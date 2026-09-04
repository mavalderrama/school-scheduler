"""Repositorio: todas las queries viven aquí. Nada de SQL fuera de este módulo.

Convención con el ORM async de Django:

- Lecturas y escrituras simples: métodos async del ORM (`aget`, `acreate`, `async for`),
  con `select_related` antes de tocar una FK desde código async.
- Escrituras multi-sentencia (p. ej. `apply_source_entries`): función **sync** con
  `transaction.atomic()` envuelta en `sync_to_async` (las transacciones no son async).
- Nunca `DJANGO_ALLOW_ASYNC_UNSAFE`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections, transaction
from django.db.models import Count, F, Q, Sum

from app.db.models import (
    AgendaEntry,
    CalendarException,
    Child,
    ConversationMessage,
    Credential,
    Family,
    LLMCacheEntry,
    LLMCall,
    Membership,
    MembershipRole,
    NotificationKind,
    NotificationLog,
    Reminder,
    ScheduleSlot,
    ScheduleTemplate,
    School,
    Source,
    SourceKind,
    SourceStatus,
    User,
    UserRole,
)
from app.llm.schemas import ChatTurn, ExtractedEntry, LLMUsage, ScheduleDraft, SlotDraft

# --- Arranque y conexiones ---------------------------------------------------------


def _ensure_connection() -> None:
    connection.ensure_connection()


async def check_connection() -> None:
    """Falla rápido en arranque si Postgres no responde.

    Ojo con el envoltorio: `connection` es un proxy thread-local, así que pasar
    `sync_to_async(connection.ensure_connection)` resolvería el método **en el hilo del
    event loop** y lo ejecutaría en el hilo de trabajo, con lo que Django aborta con
    "DatabaseWrapper objects created in a thread can only be used in that same thread".
    Por eso el acceso va dentro de una función que se resuelve ya en el hilo correcto.
    """
    await sync_to_async(_ensure_connection)()


def _close_old_connections() -> None:
    close_old_connections()


async def close_old() -> None:
    """Cierra conexiones obsoletas del hilo actual (por update y por job)."""
    await sync_to_async(_close_old_connections)()


def _close_all_connections() -> None:
    connections.close_all()


async def close_all() -> None:
    await sync_to_async(_close_all_connections)()


async def ensure_superuser(username: str, password: str, email: str = "") -> bool:
    """Crea el superusuario del admin si no existe. No reescribe la contraseña si ya existe."""
    user_model = get_user_model()
    if await user_model.objects.filter(username=username).aexists():
        return False
    await user_model.objects.acreate_superuser(username, email or None, password)
    return True


# --- Inquilino: quién es quién ------------------------------------------------------------
#
# El chat determina el niño, así que casi todo el ámbito se resuelve desde el `chat_id` que
# ya traen los handlers. Nada de esto adivina: si el chat no está vinculado, no hay niño y
# quien llame decide qué hacer.


async def child_for_chat(chat_id: int) -> Child | None:
    """El niño de este chat, o None si el chat no está vinculado a ninguno."""
    return (
        await Child.objects.select_related("family", "school")
        .filter(chat_id=chat_id, is_active=True)
        .afirst()
    )


async def get_child(child_id: int) -> Child | None:
    return await Child.objects.select_related("family", "school").filter(pk=child_id).afirst()


async def get_family(family_id: int) -> Family | None:
    return await Family.objects.filter(pk=family_id, is_active=True).afirst()


async def update_family(family_id: int, **fields: Any) -> None:
    await Family.objects.filter(pk=family_id).aupdate(**fields)


async def credentials_of(family_id: int) -> list[Credential]:
    """Claves de una familia. El secreto viaja cifrado; lo descifra quien lo necesite."""
    qs = Credential.objects.filter(family_id=family_id).order_by("provider")
    return [row async for row in qs]


async def upsert_credential(family_id: int, provider: str, **fields: Any) -> Credential:
    row, _ = await Credential.objects.aupdate_or_create(
        family_id=family_id, provider=provider, defaults=fields
    )
    return row


async def calls_this_month(family_id: int, since: datetime) -> int:
    """Llamadas reales al LLM de una familia (los aciertos de caché no cuentan)."""
    return (
        await LLMCall.objects.filter(family_id=family_id, created_at__gte=since)
        .exclude(provider="cache")
        .acount()
    )


async def children_of(family_id: int) -> list[Child]:
    qs = (
        Child.objects.select_related("school")
        .filter(family_id=family_id, is_active=True)
        .order_by("name")
    )
    return [child async for child in qs]


async def active_children() -> list[Child]:
    """Todos los niños activos con chat. Lo usan los barridos programados."""
    qs = (
        Child.objects.select_related("family", "school")
        .filter(is_active=True, chat_id__isnull=False)
        .order_by("id")
    )
    return [child async for child in qs]


async def families_of(telegram_user_id: int) -> list[Family]:
    qs = Family.objects.filter(memberships__user_id=telegram_user_id, is_active=True).order_by("id")
    return [family async for family in qs]


async def is_member(telegram_user_id: int, family_id: int) -> bool:
    return await Membership.objects.filter(user_id=telegram_user_id, family_id=family_id).aexists()


async def create_family(name: str, *, owner: User | None = None) -> Family:
    family = await Family.objects.acreate(name=name)
    if owner is not None:
        await Membership.objects.acreate(family=family, user=owner, role=MembershipRole.OWNER)
    return family


async def create_school(family_id: int, name: str, **fields: Any) -> School:
    return await School.objects.acreate(family_id=family_id, name=name, **fields)


async def create_child(family_id: int, school_id: int, name: str, **fields: Any) -> Child:
    return await Child.objects.acreate(
        family_id=family_id, school_id=school_id, name=name, **fields
    )


# --- Usuarios y fuentes -----------------------------------------------------------------


async def upsert_user(telegram_user_id: int, display_name: str) -> User:
    """Crea el padre/madre si no existe; actualiza el nombre. Nunca toca el rol."""
    user, _ = await User.objects.aupdate_or_create(
        telegram_user_id=telegram_user_id,
        defaults={"display_name": display_name},
        create_defaults={"display_name": display_name, "role": UserRole.PARENT},
    )
    return user


async def get_user(telegram_user_id: int) -> User | None:
    return await User.objects.filter(telegram_user_id=telegram_user_id).afirst()


async def create_source(
    kind: SourceKind,
    *,
    child_id: int,
    telegram_file_id: str | None = None,
    submitted_by: User | None = None,
    chat_id: int | None = None,
    caption: str | None = None,
) -> Source:
    return await Source.objects.acreate(
        child_id=child_id,
        kind=kind,
        telegram_file_id=telegram_file_id,
        submitted_by=submitted_by,
        chat_id=chat_id,
        caption=caption,
    )


async def photos_awaiting_extraction(
    older_than: datetime, *, give_up_before: datetime, limit: int = 3
) -> list[Source]:
    """Fotos descargadas que nunca llegaron a leerse (típicamente por cuota agotada).

    `pending` + `local_path` + sin `raw_llm_output` es exactamente ese estado: una foto ya
    extraída y a la espera de confirmación sí tiene `raw_llm_output`.
    """
    qs = (
        Source.objects.filter(
            kind=SourceKind.PHOTO,
            status=SourceStatus.PENDING,
            raw_llm_output__isnull=True,
            local_path__isnull=False,
            created_at__lte=older_than,
            created_at__gt=give_up_before,
        )
        .select_related("submitted_by")
        .order_by("id")[:limit]
    )
    return [source async for source in qs]


async def abandon_stale_photos(give_up_before: datetime) -> list[Source]:
    """Fotos que llevan demasiado sin poder leerse: se marcan `failed` y se avisa una vez."""
    qs = Source.objects.filter(
        kind=SourceKind.PHOTO,
        status=SourceStatus.PENDING,
        raw_llm_output__isnull=True,
        local_path__isnull=False,
        created_at__lte=give_up_before,
    ).order_by("id")
    stale = [source async for source in qs]
    if stale:
        await Source.objects.filter(pk__in=[s.pk for s in stale]).aupdate(
            status=SourceStatus.FAILED
        )
    return stale


async def photos_to_purge(before: datetime, limit: int = 200) -> list[Source]:
    """Fotos ya resueltas y antiguas: se borra el archivo, la fila se conserva."""
    qs = Source.objects.filter(
        kind=SourceKind.PHOTO,
        local_path__isnull=False,
        created_at__lt=before,
        status__in=[SourceStatus.CONFIRMED, SourceStatus.REJECTED, SourceStatus.FAILED],
    ).order_by("id")[:limit]
    return [source async for source in qs]


async def clear_local_path(source_id: int) -> None:
    await Source.objects.filter(pk=source_id).aupdate(local_path=None)


async def recent_sources(family_id: int, limit: int = 3) -> list[Source]:
    qs = (
        Source.objects.select_related("submitted_by")
        .filter(child__family_id=family_id)
        .order_by("-id")[:limit]
    )
    return [source async for source in qs]


async def count_awaiting_extraction(family_id: int) -> int:
    return await Source.objects.filter(
        child__family_id=family_id,
        kind=SourceKind.PHOTO,
        status=SourceStatus.PENDING,
        raw_llm_output__isnull=True,
        local_path__isnull=False,
    ).acount()


async def update_source(source_id: int, **fields: Any) -> None:
    await Source.objects.filter(pk=source_id).aupdate(**fields)


async def get_source(source_id: int) -> Source | None:
    return await Source.objects.select_related("submitted_by").filter(pk=source_id).afirst()


async def set_source_status(source_id: int, status: SourceStatus) -> None:
    await Source.objects.filter(pk=source_id).aupdate(status=status)


# --- Entradas de agenda -----------------------------------------------------------------


def _apply_source_entries(source_id: int, entries: list[ExtractedEntry]) -> tuple[int, int]:
    """Merge por fecha (sección 5 del plan), todo en una transacción.

    Para cada fecha cubierta desactiva las entradas activas previas **de ese niño**,
    marcándolas como reemplazadas por esta source, inserta las nuevas y confirma la source.
    Devuelve (insertadas, reemplazadas).

    El filtro por niño no es opcional: sin él, confirmar una foto desactivaría las entradas
    de todas las demás familias en esas fechas.
    """
    with transaction.atomic():
        source = Source.objects.select_for_update().get(pk=source_id)
        dates = {entry.entry_date for entry in entries}
        superseded = (
            AgendaEntry.objects.filter(
                child_id=source.child_id, entry_date__in=dates, is_active=True
            )
            .exclude(source_id=source_id)
            .update(is_active=False, superseded_by=source)
        )
        created = AgendaEntry.objects.bulk_create(
            [
                AgendaEntry(
                    child_id=source.child_id,
                    entry_date=entry.entry_date,
                    kind=entry.kind,
                    text=entry.text,
                    source=source,
                )
                for entry in entries
            ]
        )
        source.status = SourceStatus.CONFIRMED
        source.save(update_fields=["status"])
        return len(created), superseded


async def apply_source_entries(source_id: int, entries: list[ExtractedEntry]) -> tuple[int, int]:
    return await sync_to_async(_apply_source_entries)(source_id, entries)


async def active_entries(child_id: int, date_from: date, date_to: date) -> list[AgendaEntry]:
    """Entradas vigentes de un niño en [date_from, date_to], por fecha, tipo e id."""
    qs = (
        AgendaEntry.objects.filter(
            child_id=child_id, is_active=True, entry_date__gte=date_from, entry_date__lte=date_to
        )
        .select_related("source")
        .order_by("entry_date", "kind", "id")
    )
    return [entry async for entry in qs]


async def entries_for_source(source_id: int) -> list[AgendaEntry]:
    qs = AgendaEntry.objects.filter(source_id=source_id).order_by("entry_date", "id")
    return [entry async for entry in qs]


async def active_dates(child_id: int, date_from: date, date_to: date) -> set[date]:
    """Fechas de [date_from, date_to] con al menos una entrada vigente de ese niño."""
    qs = (
        AgendaEntry.objects.filter(
            child_id=child_id, is_active=True, entry_date__gte=date_from, entry_date__lte=date_to
        )
        .values_list("entry_date", flat=True)
        .distinct()
    )
    return {day async for day in qs}


def _add_single_entry(source_id: int, entry: ExtractedEntry) -> AgendaEntry:
    """Alta por texto: inserta UNA entrada sin tocar las demás de esa fecha.

    A diferencia de una foto (que reemplaza el día entero), agregar por texto es aditivo.
    """
    with transaction.atomic():
        source = Source.objects.select_for_update().get(pk=source_id)
        created = AgendaEntry.objects.create(
            child_id=source.child_id,
            entry_date=entry.entry_date,
            kind=entry.kind,
            text=entry.text,
            source=source,
        )
        source.status = SourceStatus.CONFIRMED
        source.save(update_fields=["status"])
        return created


async def add_single_entry(source_id: int, entry: ExtractedEntry) -> AgendaEntry:
    return await sync_to_async(_add_single_entry)(source_id, entry)


def _deactivate_entry(entry_id: int, source_id: int, child_id: int) -> bool:
    """Baja por texto: desactiva solo esa entrada, referenciando la source que la quitó."""
    with transaction.atomic():
        source = Source.objects.select_for_update().get(pk=source_id)
        updated = AgendaEntry.objects.filter(pk=entry_id, child_id=child_id, is_active=True).update(
            is_active=False, superseded_by=source
        )
        source.status = SourceStatus.CONFIRMED
        source.save(update_fields=["status"])
        return bool(updated)


async def deactivate_entry(entry_id: int, source_id: int, *, child_id: int) -> bool:
    return await sync_to_async(_deactivate_entry)(entry_id, source_id, child_id)


async def find_active_entries(
    child_id: int, date_from: date, date_to: date, hint: str | None = None
) -> list[AgendaEntry]:
    """Candidatas a borrar: vigentes del niño en el rango, filtradas por texto si hay pista."""
    qs = AgendaEntry.objects.filter(
        child_id=child_id, is_active=True, entry_date__gte=date_from, entry_date__lte=date_to
    )
    if hint:
        # Palabras de 4+ letras de la pista; ILIKE por cada una (OR).
        words = [w for w in hint.split() if len(w) >= 4]
        if words:
            matches = Q()
            for word in words:
                matches |= Q(text__icontains=word)
            qs = qs.filter(matches)
    return [entry async for entry in qs.order_by("entry_date", "kind", "id")]


async def get_entry(entry_id: int, *, child_id: int) -> AgendaEntry | None:
    """Una entrada **de ese niño**. El id llega de un botón de Telegram, así que no basta
    con buscar por clave primaria: hay que comprobar de quién es."""
    return await AgendaEntry.objects.filter(pk=entry_id, child_id=child_id).afirst()


# --- Historial de conversación ------------------------------------------------------------


async def save_message(chat_id: int, telegram_user_id: int | None, role: str, content: str) -> None:
    await ConversationMessage.objects.acreate(
        chat_id=chat_id, telegram_user_id=telegram_user_id, role=role, content=content
    )


async def recent_history(chat_id: int, limit: int = 6) -> list[ChatTurn]:
    """Últimos turnos del chat, del más antiguo al más reciente (como los lee el prompt)."""
    qs = ConversationMessage.objects.filter(chat_id=chat_id).order_by("-created_at", "-id")[:limit]
    rows = [row async for row in qs]
    return [ChatTurn(role=row.role, content=row.content) for row in reversed(rows)]


# --- Notificaciones ---------------------------------------------------------------------


async def notification_sent_ok(
    kinds: Sequence[NotificationKind],
    target_date: date,
    chat_id: int,
    child_id: int | None = None,
    reminder_id: int | None = None,
) -> bool:
    """True si ya hubo un envío correcto de alguno de esos tipos para esa fecha y chat."""
    qs = NotificationLog.objects.filter(
        kind__in=list(kinds), target_date=target_date, chat_id=chat_id, ok=True
    )
    # `child_id=None` es un valor legítimo (avisos que no son de un niño concreto), y el
    # unique parcial usa `nulls_distinct=False`, así que se filtra explícitamente. Lo mismo
    # con el recordatorio: sin este filtro, el aviso diario vería los de los recordatorios.
    qs = qs.filter(child_id__isnull=True) if child_id is None else qs.filter(child_id=child_id)
    qs = (
        qs.filter(reminder_id__isnull=True)
        if reminder_id is None
        else qs.filter(reminder_id=reminder_id)
    )
    return await qs.aexists()


async def log_notification(
    kind: NotificationKind,
    target_date: date,
    chat_id: int,
    *,
    ok: bool,
    error: str | None,
    child_id: int | None = None,
    reminder_id: int | None = None,
) -> None:
    await NotificationLog.objects.acreate(
        kind=kind,
        target_date=target_date,
        chat_id=chat_id,
        child_id=child_id,
        reminder_id=reminder_id,
        ok=ok,
        error=error,
    )


async def last_notification(child_id: int | None = None) -> NotificationLog | None:
    qs = NotificationLog.objects.all()
    if child_id is not None:
        qs = qs.filter(child_id=child_id)
    return await qs.order_by("-sent_at", "-id").afirst()


async def notifications(kind: NotificationKind | None = None) -> list[NotificationLog]:
    qs = NotificationLog.objects.order_by("id")
    if kind is not None:
        qs = qs.filter(kind=kind)
    return [row async for row in qs]


# --- Recordatorios ------------------------------------------------------------------------


async def create_reminder(
    *,
    child_id: int,
    chat_id: int,
    text: str,
    time_of_day: time,
    repeat: str,
    weekdays: str = "",
    on_date: date | None = None,
    only_school_days: bool = False,
    next_fire_at: datetime | None,
    created_by_id: int | None = None,
) -> Reminder:
    return await Reminder.objects.acreate(
        child_id=child_id,
        chat_id=chat_id,
        text=text,
        time_of_day=time_of_day,
        repeat=repeat,
        weekdays=weekdays,
        on_date=on_date,
        only_school_days=only_school_days,
        next_fire_at=next_fire_at,
        created_by_id=created_by_id,
    )


async def reminders_of(child_id: int) -> list[Reminder]:
    """Los recordatorios vigentes de ese niño, en el orden en que se van a disparar."""
    qs = Reminder.objects.filter(child_id=child_id, is_active=True).order_by(
        F("next_fire_at").asc(nulls_last=True), "id"
    )
    return [row async for row in qs]


async def find_active_reminders(child_id: int, hint: str | None = None) -> list[Reminder]:
    """Candidatos a borrar. Mismo criterio que en las entradas: palabras de 4+ letras."""
    qs = Reminder.objects.filter(child_id=child_id, is_active=True)
    if hint:
        words = [word for word in hint.split() if len(word) >= 4]
        if words:
            match = Q()
            for word in words:
                match |= Q(text__icontains=word)
            qs = qs.filter(match)
    return [row async for row in qs.order_by(F("next_fire_at").asc(nulls_last=True), "id")]


async def get_reminder(reminder_id: int, *, child_id: int) -> Reminder | None:
    """Un recordatorio **de ese niño**: el id llega de un botón, hay que comprobar de quién es."""
    return await Reminder.objects.filter(pk=reminder_id, child_id=child_id).afirst()


async def deactivate_reminder(reminder_id: int, *, child_id: int) -> bool:
    """Lo apaga. No se borra la fila: deja de estar vigente y de tener próxima vez."""
    updated = await Reminder.objects.filter(
        pk=reminder_id, child_id=child_id, is_active=True
    ).aupdate(is_active=False, next_fire_at=None)
    return bool(updated)


async def due_reminders(now: datetime, *, limit: int = 50) -> list[Reminder]:
    """Los que ya tocan, de todos los niños: es el barrido programado, no una consulta de nadie."""
    qs = (
        Reminder.objects.select_related("child__school", "child__family")
        .filter(is_active=True, next_fire_at__isnull=False, next_fire_at__lte=now)
        .order_by("next_fire_at", "id")[:limit]
    )
    return [row async for row in qs]


async def claim_reminder(
    reminder_id: int, *, expected: datetime, next_fire_at: datetime | None, fired_at: datetime
) -> bool:
    """Reserva la ocurrencia antes de enviarla. False si otro barrido se adelantó.

    La condición es el propio `next_fire_at` que se leyó: si ya cambió, esta ejecución no es
    la dueña del envío. Evita el duplicado sin bloquear la fila.
    """
    updated = await Reminder.objects.filter(pk=reminder_id, next_fire_at=expected).aupdate(
        next_fire_at=next_fire_at,
        last_fired_at=fired_at,
        is_active=next_fire_at is not None,
    )
    return bool(updated)


# --- Consumo de LLM ---------------------------------------------------------------------


async def log_llm_call(
    *,
    task: str,
    provider: str,
    ok: bool,
    error: str | None,
    usage: LLMUsage | None,
    duration_ms: int,
    model: str | None = None,
    prompt: str | None = None,
    response: dict[str, Any] | None = None,
    family_id: int | None = None,
) -> None:
    await LLMCall.objects.acreate(
        family_id=family_id,
        provider=provider,
        task=task,
        model=usage.model if usage else model,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        cache_read_tokens=usage.cache_read_tokens if usage else None,
        cache_write_tokens=usage.cache_write_tokens if usage else None,
        cost_usd=Decimal(str(usage.cost_usd)) if usage and usage.cost_usd is not None else None,
        duration_ms=usage.duration_ms if usage else duration_ms,
        ok=ok,
        error=error,
        prompt=prompt,
        response=response,
    )


# --- Caché de respuestas del LLM -----------------------------------------------------------


async def get_cache_entry(key: str, *, now: datetime) -> LLMCacheEntry | None:
    """Entrada vigente (no expirada) para la clave."""
    return await LLMCacheEntry.objects.filter(key=key, expires_at__gt=now).afirst()


async def upsert_cache_entry(
    key: str,
    *,
    task: str,
    prompt_version: str,
    provider: str,
    model: str | None,
    response: dict[str, Any],
    expires_at: datetime,
) -> None:
    await LLMCacheEntry.objects.aupdate_or_create(
        key=key,
        defaults={
            "task": task,
            "prompt_version": prompt_version,
            "provider": provider,
            "model": model,
            "response": response,
            "expires_at": expires_at,
        },
        create_defaults={
            "task": task,
            "prompt_version": prompt_version,
            "provider": provider,
            "model": model,
            "response": response,
            "expires_at": expires_at,
            "hits": 0,
        },
    )


async def touch_cache_entry(key: str, *, when: datetime) -> None:
    await LLMCacheEntry.objects.filter(key=key).aupdate(hits=F("hits") + 1, last_hit_at=when)


async def delete_cache_entry(key: str) -> bool:
    deleted, _ = await LLMCacheEntry.objects.filter(key=key).adelete()
    return bool(deleted)


async def purge_expired_cache(now: datetime) -> int:
    deleted, _ = await LLMCacheEntry.objects.filter(expires_at__lte=now).adelete()
    return int(deleted)


async def cache_entries() -> list[LLMCacheEntry]:
    return [entry async for entry in LLMCacheEntry.objects.order_by("id")]


async def llm_usage_by_provider(family_id: int, since: datetime) -> list[dict[str, Any]]:
    """Consumo agregado por proveedor desde `since`, para /estado."""
    qs = (
        LLMCall.objects.filter(family_id=family_id, created_at__gte=since)
        .values("provider")
        .annotate(
            calls=Count("id"),
            errors=Count("id", filter=Q(ok=False)),
            input_tokens=Sum("input_tokens"),
            output_tokens=Sum("output_tokens"),
            cache_read_tokens=Sum("cache_read_tokens"),
            cost_usd=Sum("cost_usd"),
        )
        .order_by("provider")
    )
    return [dict(row) async for row in qs]


async def last_call_by_provider(family_id: int) -> dict[str, LLMCall]:
    """Última llamada de cada proveedor: salud observada sin gastar un token."""
    latest: dict[str, LLMCall] = {}
    qs = LLMCall.objects.filter(family_id=family_id).order_by("-id")[:200]
    async for call in qs:
        latest.setdefault(call.provider, call)
    return latest


async def cache_stats() -> tuple[int, int]:
    """(entradas vigentes, aciertos acumulados)."""
    entries = await LLMCacheEntry.objects.acount()
    total = await LLMCacheEntry.objects.aaggregate(hits=Sum("hits"))
    return entries, int(total["hits"] or 0)


async def llm_calls(task: str | None = None) -> list[LLMCall]:
    qs = LLMCall.objects.order_by("id")
    if task is not None:
        qs = qs.filter(task=task)
    return [call async for call in qs]


# --- Diagnóstico --------------------------------------------------------------------------


def _vector_extension_installed() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        return cursor.fetchone() is not None


async def vector_extension_installed() -> bool:
    return await sync_to_async(_vector_extension_installed)()


def _table_constraints(table: str) -> dict[str, Any]:
    with connection.cursor() as cursor:
        return dict(connection.introspection.get_constraints(cursor, table))


async def table_constraints(table: str) -> dict[str, Any]:
    """Índices y constraints de una tabla, por nombre (para tests y /estado)."""
    return await sync_to_async(_table_constraints)(table)


# --- Horario rotativo y calendario ------------------------------------------------------


def _apply_schedule(
    source_id: int,
    draft: ScheduleDraft,
    *,
    valid_from: date,
    replace_ids: Sequence[int] = (),
) -> int:
    """Guarda un horario nuevo. **Solo** reemplaza los que se le indiquen explícitamente.

    Varios horarios pueden convivir: la rotación académica y el programa de la jornada
    extendida son cosas distintas y el mismo día tiene las dos. Reemplazar de más fue un
    bug real: guardar el segundo horario borraba el primero sin preguntar.

    Lo reemplazado no se borra: queda `is_active=False` con `superseded_by` a esta source
    y `valid_to` cerrado el día antes (nunca antes de su propio `valid_from`).
    """
    if draft.anchor_monday is None:
        raise ValueError("el horario no tiene lunes ancla")
    with transaction.atomic():
        source = Source.objects.select_for_update().get(pk=source_id)
        if replace_ids:
            # `pk__in` viene de un botón: se acota al niño de la source para que un
            # callback manipulado no pueda reemplazar el horario de otra familia.
            for old in ScheduleTemplate.objects.select_for_update().filter(
                pk__in=replace_ids, child_id=source.child_id, is_active=True
            ):
                old.is_active = False
                old.superseded_by = source
                old.valid_to = max(valid_from - timedelta(days=1), old.valid_from)
                old.save(update_fields=["is_active", "superseded_by", "valid_to"])
        labels = _week_labels(draft)
        template = ScheduleTemplate.objects.create(
            child_id=source.child_id,
            name=draft.name or "Horario",
            anchor_monday=draft.anchor_monday,
            cycle_weeks=draft.cycle_weeks,
            valid_from=valid_from,
            source=source,
        )
        ScheduleSlot.objects.bulk_create(
            [
                ScheduleSlot(
                    schedule=template,
                    week_index=labels[slot.week_label],
                    week_label=slot.week_label,
                    weekday=slot.weekday,
                    rotation=slot.rotation,
                    subject=slot.subject,
                )
                for slot in _unique_slots(draft)
            ]
        )
        source.status = SourceStatus.CONFIRMED
        source.save(update_fields=["status"])
        return template.pk


async def apply_schedule(
    source_id: int,
    draft: ScheduleDraft,
    *,
    valid_from: date,
    replace_ids: Sequence[int] = (),
) -> int:
    return await sync_to_async(_apply_schedule)(
        source_id, draft, valid_from=valid_from, replace_ids=replace_ids
    )


def _week_labels(draft: ScheduleDraft) -> dict[str, int]:
    """Etiqueta de semana -> índice del ciclo, en el orden en que aparecen en la foto."""
    labels: dict[str, int] = {}
    for slot in draft.slots:
        if slot.week_label not in labels:
            labels[slot.week_label] = len(labels)
    return labels


def _unique_slots(draft: ScheduleDraft) -> list[SlotDraft]:
    """Una franja por (semana, día): el unique de la tabla no admite repetidos."""
    seen: dict[tuple[str, int], SlotDraft] = {}
    for slot in draft.slots:
        seen.setdefault((slot.week_label, slot.weekday), slot)
    return list(seen.values())


async def active_schedules(child_id: int, day: date | None = None) -> list[ScheduleTemplate]:
    """Plantillas vigentes de ese niño. Pueden convivir varias (rotación + jornada extendida)."""
    qs = ScheduleTemplate.objects.filter(child_id=child_id, is_active=True)
    if day is not None:
        qs = qs.filter(valid_from__lte=day).filter(Q(valid_to__isnull=True) | Q(valid_to__gte=day))
    return [t async for t in qs.order_by("id")]


async def active_schedule(child_id: int, day: date | None = None) -> ScheduleTemplate | None:
    """La primera plantilla vigente. Usar `active_schedules` salvo que baste con saber si hay."""
    schedules = await active_schedules(child_id, day)
    return schedules[0] if schedules else None


async def slots_for_schedules(schedule_ids: Sequence[int]) -> dict[int, list[ScheduleSlot]]:
    """Franjas de varios horarios en una sola consulta, agrupadas por horario."""
    grouped: dict[int, list[ScheduleSlot]] = {sid: [] for sid in schedule_ids}
    qs = ScheduleSlot.objects.filter(schedule_id__in=schedule_ids).order_by(
        "schedule_id", "week_index", "weekday", "id"
    )
    async for slot in qs:
        grouped[slot.schedule_id].append(slot)
    return grouped


async def schedule_slots(schedule_id: int) -> list[ScheduleSlot]:
    qs = ScheduleSlot.objects.filter(schedule_id=schedule_id).order_by(
        "week_index", "weekday", "id"
    )
    return [slot async for slot in qs]


async def calendar_exceptions(school_id: int) -> dict[date, tuple[str, str]]:
    """Día -> (tipo, motivo) de un colegio. Los hermanos del mismo colegio lo comparten."""
    qs = CalendarException.objects.filter(school_id=school_id).order_by("day")
    return {row.day: (row.kind, row.label) async for row in qs}


async def add_calendar_exception(
    school_id: int, day: date, kind: str, label: str
) -> CalendarException:
    row, _ = await CalendarException.objects.aupdate_or_create(
        school_id=school_id, day=day, defaults={"kind": kind, "label": label}
    )
    return row


async def purge_llm_traces(before: datetime) -> int:
    """Vacía prompt y respuesta de las llamadas viejas; la fila y las métricas se quedan.

    Mismo criterio que con las fotos: se borra el material pesado, no la auditoría.
    """
    return await LLMCall.objects.filter(created_at__lt=before, prompt__isnull=False).aupdate(
        prompt=None, response=None
    )

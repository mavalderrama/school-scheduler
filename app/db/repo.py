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
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections, transaction
from django.db.models import Count, F, Q, Sum

from app.db.models import (
    AgendaEntry,
    ConversationMessage,
    LLMCacheEntry,
    LLMCall,
    NotificationKind,
    NotificationLog,
    Source,
    SourceKind,
    SourceStatus,
    User,
    UserRole,
)
from app.llm.schemas import ChatTurn, ExtractedEntry, LLMUsage

# --- Arranque y conexiones ---------------------------------------------------------


async def check_connection() -> None:
    """Falla rápido en arranque si Postgres no responde."""
    await sync_to_async(connection.ensure_connection)()


async def close_old() -> None:
    """Cierra conexiones obsoletas del hilo actual (por update y por job)."""
    await sync_to_async(close_old_connections)()


async def close_all() -> None:
    await sync_to_async(connections.close_all)()


async def ensure_superuser(username: str, password: str, email: str = "") -> bool:
    """Crea el superusuario del admin si no existe. No reescribe la contraseña si ya existe."""
    user_model = get_user_model()
    if await user_model.objects.filter(username=username).aexists():
        return False
    await user_model.objects.acreate_superuser(username, email or None, password)
    return True


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
    telegram_file_id: str | None = None,
    submitted_by: User | None = None,
    chat_id: int | None = None,
) -> Source:
    return await Source.objects.acreate(
        kind=kind,
        telegram_file_id=telegram_file_id,
        submitted_by=submitted_by,
        chat_id=chat_id,
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


async def recent_sources(limit: int = 3) -> list[Source]:
    qs = Source.objects.select_related("submitted_by").order_by("-id")[:limit]
    return [source async for source in qs]


async def count_awaiting_extraction() -> int:
    return await Source.objects.filter(
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

    Para cada fecha cubierta desactiva las entradas activas previas marcándolas como
    reemplazadas por esta source, inserta las nuevas y confirma la source.
    Devuelve (insertadas, reemplazadas).
    """
    with transaction.atomic():
        source = Source.objects.select_for_update().get(pk=source_id)
        dates = {entry.entry_date for entry in entries}
        superseded = (
            AgendaEntry.objects.filter(entry_date__in=dates, is_active=True)
            .exclude(source_id=source_id)
            .update(is_active=False, superseded_by=source)
        )
        created = AgendaEntry.objects.bulk_create(
            [
                AgendaEntry(
                    entry_date=entry.entry_date, kind=entry.kind, text=entry.text, source=source
                )
                for entry in entries
            ]
        )
        source.status = SourceStatus.CONFIRMED
        source.save(update_fields=["status"])
        return len(created), superseded


async def apply_source_entries(source_id: int, entries: list[ExtractedEntry]) -> tuple[int, int]:
    return await sync_to_async(_apply_source_entries)(source_id, entries)


async def active_entries(date_from: date, date_to: date) -> list[AgendaEntry]:
    """Entradas vigentes en [date_from, date_to], ordenadas por fecha, tipo e id."""
    qs = (
        AgendaEntry.objects.filter(
            is_active=True, entry_date__gte=date_from, entry_date__lte=date_to
        )
        .select_related("source")
        .order_by("entry_date", "kind", "id")
    )
    return [entry async for entry in qs]


async def entries_for_source(source_id: int) -> list[AgendaEntry]:
    qs = AgendaEntry.objects.filter(source_id=source_id).order_by("entry_date", "id")
    return [entry async for entry in qs]


async def active_dates(date_from: date, date_to: date) -> set[date]:
    """Fechas de [date_from, date_to] con al menos una entrada vigente."""
    qs = (
        AgendaEntry.objects.filter(
            is_active=True, entry_date__gte=date_from, entry_date__lte=date_to
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
            entry_date=entry.entry_date, kind=entry.kind, text=entry.text, source=source
        )
        source.status = SourceStatus.CONFIRMED
        source.save(update_fields=["status"])
        return created


async def add_single_entry(source_id: int, entry: ExtractedEntry) -> AgendaEntry:
    return await sync_to_async(_add_single_entry)(source_id, entry)


def _deactivate_entry(entry_id: int, source_id: int) -> bool:
    """Baja por texto: desactiva solo esa entrada, referenciando la source que la quitó."""
    with transaction.atomic():
        source = Source.objects.select_for_update().get(pk=source_id)
        updated = AgendaEntry.objects.filter(pk=entry_id, is_active=True).update(
            is_active=False, superseded_by=source
        )
        source.status = SourceStatus.CONFIRMED
        source.save(update_fields=["status"])
        return bool(updated)


async def deactivate_entry(entry_id: int, source_id: int) -> bool:
    return await sync_to_async(_deactivate_entry)(entry_id, source_id)


async def find_active_entries(
    date_from: date, date_to: date, hint: str | None = None
) -> list[AgendaEntry]:
    """Candidatas a borrar: vigentes en el rango, filtradas por texto si hay pista."""
    qs = AgendaEntry.objects.filter(
        is_active=True, entry_date__gte=date_from, entry_date__lte=date_to
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


async def get_entry(entry_id: int) -> AgendaEntry | None:
    return await AgendaEntry.objects.filter(pk=entry_id).afirst()


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
    kinds: Sequence[NotificationKind], target_date: date, chat_id: int
) -> bool:
    """True si ya hubo un envío correcto de alguno de esos tipos para esa fecha y chat."""
    return await NotificationLog.objects.filter(
        kind__in=list(kinds), target_date=target_date, chat_id=chat_id, ok=True
    ).aexists()


async def log_notification(
    kind: NotificationKind, target_date: date, chat_id: int, *, ok: bool, error: str | None
) -> None:
    await NotificationLog.objects.acreate(
        kind=kind, target_date=target_date, chat_id=chat_id, ok=ok, error=error
    )


async def last_notification() -> NotificationLog | None:
    return await NotificationLog.objects.order_by("-sent_at", "-id").afirst()


async def notifications(kind: NotificationKind | None = None) -> list[NotificationLog]:
    qs = NotificationLog.objects.order_by("id")
    if kind is not None:
        qs = qs.filter(kind=kind)
    return [row async for row in qs]


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
) -> None:
    await LLMCall.objects.acreate(
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


async def llm_usage_by_provider(since: datetime) -> list[dict[str, Any]]:
    """Consumo agregado por proveedor desde `since`, para /estado."""
    qs = (
        LLMCall.objects.filter(created_at__gte=since)
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


async def last_call_by_provider() -> dict[str, LLMCall]:
    """Última llamada de cada proveedor: salud observada sin gastar un token."""
    latest: dict[str, LLMCall] = {}
    qs = LLMCall.objects.order_by("-id")[:200]
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

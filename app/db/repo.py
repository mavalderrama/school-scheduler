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
from datetime import date
from decimal import Decimal
from typing import Any

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections, transaction

from app.db.models import (
    AgendaEntry,
    LLMCall,
    NotificationKind,
    NotificationLog,
    Source,
    SourceKind,
    SourceStatus,
    User,
    UserRole,
)
from app.llm.schemas import ExtractedEntry, LLMUsage

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


async def create_source(
    kind: SourceKind,
    *,
    telegram_file_id: str | None = None,
    submitted_by: User | None = None,
) -> Source:
    return await Source.objects.acreate(
        kind=kind, telegram_file_id=telegram_file_id, submitted_by=submitted_by
    )


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
) -> None:
    await LLMCall.objects.acreate(
        provider=provider,
        task=task,
        model=usage.model if usage else None,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        cost_usd=Decimal(str(usage.cost_usd)) if usage and usage.cost_usd is not None else None,
        duration_ms=usage.duration_ms if usage else duration_ms,
        ok=ok,
        error=error,
    )


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

"""Smoke test del esquema contra Postgres real (make test). Requiere el marker django_db.

`transaction=True` es obligatorio en tests `async def`: el ORM async corre en otro hilo (y otra
conexión) que la transacción que pytest-django abriría por defecto.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import date
from pathlib import Path

import pytest
from django.db import IntegrityError

from app.db import repo
from app.db.models import (
    AgendaEntry,
    EntryKind,
    NotificationKind,
    NotificationLog,
    ScheduleSlot,
    ScheduleTemplate,
    Source,
    SourceKind,
    SourceStatus,
)
from tests.conftest import TENANT

pytestmark = pytest.mark.django_db(transaction=True)


async def test_create_source_and_entry_roundtrip() -> None:
    source = await Source.objects.acreate(kind=SourceKind.MANUAL, child_id=TENANT.child_id)
    assert source.status == SourceStatus.PENDING
    assert source.created_at is not None  # db_default=Now() vuelve por RETURNING

    entry = await AgendaEntry.objects.acreate(
        child_id=TENANT.child_id,
        entry_date=date(2026, 9, 3),
        kind=EntryKind.BRING,
        text="sudadera",
        source=source,
    )
    fetched = await AgendaEntry.objects.select_related("source").aget(pk=entry.pk)
    assert fetched.source.pk == source.pk
    assert fetched.is_active is True
    assert fetched.superseded_by_id is None


async def test_vector_extension_is_installed() -> None:
    assert await repo.vector_extension_installed() is True


async def test_partial_indexes_and_constraints_exist() -> None:
    entries = await repo.table_constraints("agenda_entries")
    assert entries["agenda_entry_date_active_idx"]["index"] is True
    # El niño va primero: el índice tiene que servir para acotar por familia.
    assert entries["agenda_entry_date_active_idx"]["columns"] == ["child_id", "entry_date"]
    assert "agenda_entries_kind_check" in entries

    notifications = await repo.table_constraints("notifications_log")
    assert notifications["notif_log_ok_unique"]["unique"] is True

    messages = await repo.table_constraints("conversation_messages")
    assert messages["conv_msg_chat_created_idx"]["columns"] == ["chat_id", "created_at"]
    assert messages["conv_msg_chat_created_idx"]["orders"] == ["ASC", "DESC"]


async def test_kind_check_constraint_rejects_unknown_values() -> None:
    source = await Source.objects.acreate(kind=SourceKind.PHOTO, child_id=TENANT.child_id)
    with pytest.raises(IntegrityError):
        await AgendaEntry.objects.acreate(
            child_id=TENANT.child_id,
            entry_date=date(2026, 9, 3),
            kind="bogus",
            text="x",
            source=source,
        )


async def test_notification_log_is_idempotent_only_for_ok_sends() -> None:
    target = date(2026, 9, 3)
    await NotificationLog.objects.acreate(
        kind=NotificationKind.DAILY, target_date=target, chat_id=-100, ok=False, error="boom"
    )
    await NotificationLog.objects.acreate(
        kind=NotificationKind.DAILY, target_date=target, chat_id=-100, ok=False, error="boom"
    )
    await NotificationLog.objects.acreate(
        kind=NotificationKind.DAILY, target_date=target, chat_id=-100, ok=True
    )
    with pytest.raises(IntegrityError):
        await NotificationLog.objects.acreate(
            kind=NotificationKind.DAILY, target_date=target, chat_id=-100, ok=True
        )


async def test_ensure_superuser_is_idempotent() -> None:
    assert await repo.ensure_superuser("admin", "secret", "a@b.c") is True
    assert await repo.ensure_superuser("admin", "other", "a@b.c") is False


def test_check_connection_from_a_fresh_process() -> None:
    """Regresión del arranque: proceso nuevo, sin conexión previa, llamada desde el loop.

    `sync_to_async(connection.ensure_connection)` resolvía el método en el hilo del event
    loop y lo ejecutaba en el de trabajo; Django aborta con "DatabaseWrapper objects
    created in a thread can only be used in that same thread" y el bot no arrancaba.

    Hay que hacerlo en un proceso aparte: dentro de pytest el wrapper ya tiene conexión,
    así que `ensure_connection` sale antes de llegar a `connect()`, que es donde Django
    valida el hilo, y el fallo no aparece.
    """
    script = textwrap.dedent(
        """
        import asyncio, os, sys
        os.environ["DJANGO_SETTINGS_MODULE"] = "tests.django_settings_test"
        from app.django_bootstrap import setup_django
        setup_django()
        from app.db import repo
        asyncio.run(repo.check_connection())
        print("ok")
        """
    )
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=root, timeout=120
    )
    assert result.returncode == 0, result.stderr[-1000:]
    assert "ok" in result.stdout


async def test_connection_helpers_are_callable_from_the_loop() -> None:
    """`close_old` y `close_all` se llaman desde el loop (middleware, jobs, apagado)."""
    await repo.close_old()
    await repo.close_all()
    await repo.check_connection()


async def test_schedule_tables_and_constraints_exist() -> None:
    schedules = await repo.table_constraints("schedules")
    assert "schedules_cycle_check" in schedules

    slots = await repo.table_constraints("schedule_slots")
    assert slots["schedule_slot_unique"]["unique"] is True
    assert slots["schedule_slot_unique"]["columns"] == ["schedule_id", "week_index", "weekday"]
    assert "schedule_slot_weekday_check" in slots

    exceptions = await repo.table_constraints("calendar_exceptions")
    assert "calendar_exceptions_kind_check" in exceptions


async def test_a_slot_cannot_repeat_a_weekday_within_a_week() -> None:
    """El unique protege el cálculo: dos materias el mismo día del ciclo sería ambiguo."""
    source = await Source.objects.acreate(kind=SourceKind.PHOTO, child_id=TENANT.child_id)
    template = await ScheduleTemplate.objects.acreate(
        child_id=TENANT.child_id,
        name="H",
        anchor_monday=date(2026, 8, 31),
        valid_from=date(2026, 8, 31),
        source=source,
    )
    await ScheduleSlot.objects.acreate(
        schedule=template, week_index=0, week_label="A", weekday=1, subject="Artes"
    )
    with pytest.raises(IntegrityError):
        await ScheduleSlot.objects.acreate(
            schedule=template, week_index=0, week_label="A", weekday=1, subject="Otra"
        )


async def test_a_calendar_exception_is_unique_per_day() -> None:
    await repo.add_calendar_exception(
        TENANT.school_id, date(2026, 10, 5), "school_closed", "Semana de receso"
    )
    await repo.add_calendar_exception(
        TENANT.school_id, date(2026, 10, 5), "school_closed", "Receso (corregido)"
    )
    exceptions = await repo.calendar_exceptions(TENANT.school_id)
    assert exceptions[date(2026, 10, 5)] == ("school_closed", "Receso (corregido)")

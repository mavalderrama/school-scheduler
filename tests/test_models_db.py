"""Smoke test del esquema contra Postgres real (make test). Requiere el marker django_db.

`transaction=True` es obligatorio en tests `async def`: el ORM async corre en otro hilo (y otra
conexión) que la transacción que pytest-django abriría por defecto.
"""

from __future__ import annotations

from datetime import date

import pytest
from django.db import IntegrityError

from app.db import repo
from app.db.models import (
    AgendaEntry,
    EntryKind,
    NotificationKind,
    NotificationLog,
    Source,
    SourceKind,
    SourceStatus,
)

pytestmark = pytest.mark.django_db(transaction=True)


async def test_create_source_and_entry_roundtrip() -> None:
    source = await Source.objects.acreate(kind=SourceKind.MANUAL)
    assert source.status == SourceStatus.PENDING
    assert source.created_at is not None  # db_default=Now() vuelve por RETURNING

    entry = await AgendaEntry.objects.acreate(
        entry_date=date(2026, 9, 3), kind=EntryKind.BRING, text="sudadera", source=source
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
    assert entries["agenda_entry_date_active_idx"]["columns"] == ["entry_date"]
    assert "agenda_entries_kind_check" in entries

    notifications = await repo.table_constraints("notifications_log")
    assert notifications["notif_log_ok_unique"]["unique"] is True

    messages = await repo.table_constraints("conversation_messages")
    assert messages["conv_msg_chat_created_idx"]["columns"] == ["chat_id", "created_at"]
    assert messages["conv_msg_chat_created_idx"]["orders"] == ["ASC", "DESC"]


async def test_kind_check_constraint_rejects_unknown_values() -> None:
    source = await Source.objects.acreate(kind=SourceKind.PHOTO)
    with pytest.raises(IntegrityError):
        await AgendaEntry.objects.acreate(
            entry_date=date(2026, 9, 3), kind="bogus", text="x", source=source
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

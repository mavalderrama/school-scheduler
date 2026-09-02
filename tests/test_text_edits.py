"""Altas y bajas por texto: aditivas, versionadas y solo tras confirmar."""

from __future__ import annotations

from datetime import date

import pytest

from app.bot import actions
from app.db import repo
from app.db.models import SourceKind, SourceStatus
from app.llm.schemas import ExtractedEntry, ExtractionResult
from app.services import agenda
from app.services.confirm import PendingEdit

pytestmark = pytest.mark.django_db(transaction=True)

TUE, WED = date(2026, 9, 8), date(2026, 9, 9)


async def seed(*entries: tuple[date, str, str]) -> int:
    source = await repo.create_source(SourceKind.PHOTO)
    await agenda.apply_source(
        source.pk,
        ExtractionResult(
            entries=[
                ExtractedEntry(entry_date=d, kind=k, text=t, confidence="high")
                for d, k, t in entries
            ],
            doubts=[],
            detected_language="es",
        ),
    )
    return source.pk


async def texts(day: date) -> list[str]:
    return [e.text for e in await repo.active_entries(day, day)]


async def test_add_entry_is_additive() -> None:
    """Agregar por texto NO reemplaza el día, a diferencia de una foto nueva."""
    await seed((TUE, "bring", "sudadera"), (TUE, "homework", "pág. 12"))
    entry = await agenda.add_entry(TUE, "bring", "disfraz")
    assert entry.is_active is True
    assert await texts(TUE) == ["sudadera", "disfraz", "pág. 12"]

    source = await repo.get_source(entry.source_id)
    assert source is not None
    assert source.kind == SourceKind.TEXT_CORRECTION
    assert source.status == SourceStatus.CONFIRMED


async def test_add_entry_records_the_author() -> None:
    user = await repo.upsert_user(111, "Mamá")
    entry = await agenda.add_entry(TUE, "note", "algo", user.telegram_user_id)
    source = await repo.get_source(entry.source_id)
    assert source is not None and source.submitted_by is not None
    assert source.submitted_by.telegram_user_id == 111


async def test_remove_entry_deactivates_only_that_one() -> None:
    await seed((TUE, "bring", "sudadera"), (TUE, "homework", "pág. 12"))
    target = (await repo.active_entries(TUE, TUE))[0]

    assert await agenda.remove_entry(target.pk) is True
    assert await texts(TUE) == ["pág. 12"]

    gone = await repo.get_entry(target.pk)
    assert gone is not None
    assert gone.is_active is False
    assert gone.superseded_by_id is not None  # queda versionada, no borrada


async def test_remove_entry_twice_is_harmless() -> None:
    await seed((TUE, "note", "una"))
    target = (await repo.active_entries(TUE, TUE))[0]
    assert await agenda.remove_entry(target.pk) is True
    assert await agenda.remove_entry(target.pk) is False


async def test_apply_edit_add_and_remove() -> None:
    add = PendingEdit(
        edit_id=1, chat_id=1, action="add", entry_date=WED, kind="bring", text="botella"
    )
    assert "Agregado" in await actions.apply_edit(add, None)
    assert await texts(WED) == ["botella"]

    entry = (await repo.active_entries(WED, WED))[0]
    remove = PendingEdit(edit_id=2, chat_id=1, action="remove", entry_date=WED, entry_id=entry.pk)
    assert "Quitado" in await actions.apply_edit(remove, None)
    assert await texts(WED) == []


async def test_apply_edit_on_a_vanished_entry() -> None:
    edit = PendingEdit(edit_id=3, chat_id=1, action="remove", entry_date=WED, entry_id=999_999)
    assert "ya no está vigente" in await actions.apply_edit(edit, None)


async def test_apply_edit_remove_without_target() -> None:
    edit = PendingEdit(edit_id=4, chat_id=1, action="remove", entry_date=WED)
    assert "No sé cuál quitar" in await actions.apply_edit(edit, None)


async def test_conversation_history_roundtrip() -> None:
    for i in range(8):
        await repo.save_message(1, 111, "user", f"mensaje {i}")
        await repo.save_message(1, None, "assistant", f"respuesta {i}")
    await repo.save_message(2, 111, "user", "otro chat")

    history = await repo.recent_history(1, limit=4)
    assert [t.content for t in history] == [
        "mensaje 6",
        "respuesta 6",
        "mensaje 7",
        "respuesta 7",
    ]
    assert [t.role for t in history] == ["user", "assistant", "user", "assistant"]
    assert [t.content for t in await repo.recent_history(2)] == ["otro chat"]

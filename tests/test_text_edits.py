"""Altas y bajas por texto: aditivas, versionadas y solo tras confirmar."""

from __future__ import annotations

from datetime import date

import pytest

from app.db import repo
from app.db.models import SourceKind, SourceStatus
from app.graph import nodes
from app.llm.schemas import ExtractedEntry, ExtractionResult
from app.services import agenda, scope
from app.services.scope import Scope
from tests.conftest import TENANT

pytestmark = pytest.mark.django_db(transaction=True)


async def a_scope() -> Scope:
    """El ámbito de la familia por defecto de los tests."""
    found = await scope.for_child(TENANT.child_id)
    assert found is not None
    return found


TUE, WED = date(2026, 9, 8), date(2026, 9, 9)


async def seed(*entries: tuple[date, str, str]) -> int:
    source = await repo.create_source(SourceKind.PHOTO, child_id=TENANT.child_id)
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
    return [e.text for e in await repo.active_entries(TENANT.child_id, day, day)]


async def test_add_entry_is_additive() -> None:
    """Agregar por texto NO reemplaza el día, a diferencia de una foto nueva."""
    await seed((TUE, "bring", "sudadera"), (TUE, "homework", "pág. 12"))
    entry = await agenda.add_entry(await a_scope(), TUE, "bring", "disfraz")
    assert entry.is_active is True
    assert await texts(TUE) == ["sudadera", "disfraz", "pág. 12"]

    source = await repo.get_source(entry.source_id)
    assert source is not None
    assert source.kind == SourceKind.TEXT_CORRECTION
    assert source.status == SourceStatus.CONFIRMED


async def test_add_entry_records_the_author() -> None:
    user = await repo.upsert_user(111, "Mamá")
    entry = await agenda.add_entry(await a_scope(), TUE, "note", "algo", user.telegram_user_id)
    source = await repo.get_source(entry.source_id)
    assert source is not None and source.submitted_by is not None
    assert source.submitted_by.telegram_user_id == 111


async def test_remove_entry_deactivates_only_that_one() -> None:
    await seed((TUE, "bring", "sudadera"), (TUE, "homework", "pág. 12"))
    target = (await repo.active_entries(TENANT.child_id, TUE, TUE))[0]

    assert await agenda.remove_entry(await a_scope(), target.pk) is True
    assert await texts(TUE) == ["pág. 12"]

    gone = await repo.get_entry(target.pk, child_id=TENANT.child_id)
    assert gone is not None
    assert gone.is_active is False
    assert gone.superseded_by_id is not None  # queda versionada, no borrada


async def test_remove_entry_twice_is_harmless() -> None:
    await seed((TUE, "note", "una"))
    target = (await repo.active_entries(TENANT.child_id, TUE, TUE))[0]
    assert await agenda.remove_entry(await a_scope(), target.pk) is True
    assert await agenda.remove_entry(await a_scope(), target.pk) is False


async def apply_edit(edit: dict[str, object], entry_id: int | None = None) -> str:
    """Ejecuta el nodo del grafo que aplica un alta o una baja ya confirmada."""
    state = {
        "edit": edit,
        "user_id": None,
        "child_id": TENANT.child_id,
        "decision": {"entry_id": entry_id},
    }
    result = await nodes.apply_edit(state, None)  # type: ignore[arg-type]
    return str(result["reply"])


async def test_apply_edit_add_and_remove() -> None:
    add = {
        "edit_id": 1,
        "chat_id": 1,
        "action": "add",
        "entry_date": WED.isoformat(),
        "kind": "bring",
        "text": "botella",
    }
    assert "Agregado" in await apply_edit(add)
    assert await texts(WED) == ["botella"]

    entry = (await repo.active_entries(TENANT.child_id, WED, WED))[0]
    remove = {"edit_id": 2, "chat_id": 1, "action": "remove", "entry_id": entry.pk}
    assert "Quitado" in await apply_edit(remove)
    assert await texts(WED) == []


async def test_apply_edit_on_a_vanished_entry() -> None:
    edit = {"edit_id": 3, "chat_id": 1, "action": "remove", "entry_id": 999_999}
    assert "ya no está vigente" in await apply_edit(edit)


async def test_apply_edit_remove_without_target() -> None:
    edit = {"edit_id": 4, "chat_id": 1, "action": "remove"}
    assert "No sé cuál quitar" in await apply_edit(edit)


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

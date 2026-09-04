"""Altas y bajas por texto: aditivas, versionadas y solo tras confirmar."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.db import repo
from app.db.models import SourceKind, SourceStatus
from app.graph import nodes
from app.llm.schemas import ExtractedEntry, ExtractionResult, ScheduleDraft, SlotDraft
from app.services import agenda, reminders, scope
from app.services import schedule as schedule_service
from app.services.scope import Scope
from tests.conftest import TENANT, make_child

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
        "decision": {"target_id": entry_id},
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


# --- Recordatorios (Fase 10) ----------------------------------------------------------------


def reminder_edit(**over: object) -> dict[str, object]:
    edit: dict[str, object] = {
        "edit_id": 10,
        "chat_id": -4242,
        "action": "add_reminder",
        "text": "el disfraz",
        "time_of_day": "07:00",
        "repeat": "daily",
        "weekdays": "",
        "on_date": None,
        "only_school_days": False,
    }
    edit.update(over)
    return edit


async def test_confirming_a_reminder_saves_it_with_its_first_time() -> None:
    reply = await apply_edit(reminder_edit())

    assert "Te aviso" in reply and "07:00" in reply
    saved = await repo.reminders_of(TENANT.child_id)
    assert len(saved) == 1
    assert saved[0].text == "el disfraz"
    # Va al chat donde se pidió, no al del niño.
    assert saved[0].chat_id == -4242
    assert saved[0].next_fire_at is not None


async def test_a_one_off_whose_hour_already_passed_is_refused() -> None:
    """Antes que guardar algo que no sonaría nunca, se dice."""
    yesterday = date.today() - timedelta(days=1)
    reply = await apply_edit(
        reminder_edit(repeat="once", on_date=yesterday.isoformat(), time_of_day="07:00")
    )

    assert "nunca" in reply
    assert await repo.reminders_of(TENANT.child_id) == []


async def test_removing_a_reminder_switches_it_off() -> None:
    await apply_edit(reminder_edit())
    saved = (await repo.reminders_of(TENANT.child_id))[0]

    reply = await apply_edit(
        {"edit_id": 11, "chat_id": 1, "action": "remove_reminder", "reminder_id": saved.pk}
    )

    assert "ya no te aviso" in reply
    assert await repo.reminders_of(TENANT.child_id) == []


async def test_removing_a_reminder_that_is_gone() -> None:
    edit = {"edit_id": 12, "chat_id": 1, "action": "remove_reminder", "reminder_id": 999_999}
    assert "ya no está activo" in await apply_edit(edit)


async def test_removing_a_reminder_of_another_family_does_nothing() -> None:
    other = await make_child("Otra", chat_id=-777020)
    theirs = await repo.create_reminder(
        child_id=other.pk,
        chat_id=-777020,
        text="suyo",
        time_of_day=time(7, 0),
        repeat="daily",
        next_fire_at=datetime.now(UTC) + timedelta(hours=1),
    )

    edit = {"edit_id": 13, "chat_id": 1, "action": "remove_reminder", "reminder_id": theirs.pk}
    assert "ya no está activo" in await apply_edit(edit)

    assert [r.text for r in await repo.reminders_of(other.pk)] == ["suyo"]


async def test_the_cap_per_child_is_enforced() -> None:
    for i in range(reminders.MAX_PER_CHILD):
        await repo.create_reminder(
            child_id=TENANT.child_id,
            chat_id=1,
            text=f"n{i}",
            time_of_day=time(7, 0),
            repeat="daily",
            next_fire_at=datetime.now(UTC) + timedelta(hours=1),
        )

    assert "muchos recordatorios" in await apply_edit(reminder_edit())
    assert len(await repo.reminders_of(TENANT.child_id)) == reminders.MAX_PER_CHILD


async def test_an_unknown_action_is_answered_not_guessed() -> None:
    """Con cuatro acciones, el `else` de antes habría borrado una entrada de agenda."""
    assert "No sé qué hacer" in await apply_edit({"edit_id": 14, "chat_id": 1, "action": "vete"})


# --- Fase 10.1: lo que se repite cada semana --------------------------------------------------


async def test_a_recurring_becomes_a_weekly_schedule() -> None:
    """No son N entradas con fecha: es una regla, y vive donde viven las reglas."""
    today = date.today()
    result = await agenda.add_recurring(await a_scope(), "5", "natación", today=today)

    assert result.replaced is False
    templates = await repo.active_schedules(TENANT.child_id, today)
    assert [t.name for t in templates] == ["Natación"]
    assert templates[0].cycle_weeks == 1  # semanal, no rotativo A/B
    assert templates[0].anchor_monday.isoweekday() == 1
    assert templates[0].valid_from == today

    slots = (await repo.slots_for_schedules([templates[0].pk]))[templates[0].pk]
    assert [(s.weekday, s.subject) for s in slots] == [(5, "natación")]

    # Y se ve donde se pregunta por una materia, que es lo que se buscaba.
    found = await schedule_service.find_subject(await a_scope(), "natacion", today, count=1)
    assert found and found[0].day.isoweekday() == 5


async def test_repeating_the_same_recurring_replaces_it_instead_of_duplicating() -> None:
    """Si no, el viernes saldría «Natación» dos veces y no habría forma de quitar una."""
    today = date.today()
    scope_ = await a_scope()
    first = await agenda.add_recurring(scope_, "5", "natación", today=today)
    second = await agenda.add_recurring(scope_, "24", "Natación", today=today)

    assert second.replaced is True
    templates = await repo.active_schedules(TENANT.child_id, today)
    assert [t.name for t in templates] == ["Natación"]
    # El anterior queda fuera de las vigentes, versionado y no borrado.
    assert templates[0].pk != first.schedule_id
    slots = (await repo.slots_for_schedules([templates[0].pk]))[templates[0].pk]
    assert sorted(s.weekday for s in slots) == [2, 4]


async def test_a_recurring_does_not_touch_the_academic_schedule() -> None:
    """El horario del colegio y el extra conviven: reemplazar de más ya fue un bug real."""
    today = date.today()
    source = await repo.create_source(SourceKind.PHOTO, child_id=TENANT.child_id)
    await repo.apply_schedule(
        source.pk,
        ScheduleDraft(
            name="Horario K4A",
            cycle_weeks=2,
            anchor_monday=today - timedelta(days=today.weekday()),
            slots=[SlotDraft(week_label="A", weekday=1, subject="Música")],
        ),
        valid_from=today,
    )

    await agenda.add_recurring(await a_scope(), "5", "natación", today=today)

    names = [t.name for t in await repo.active_schedules(TENANT.child_id, today)]
    assert sorted(names) == ["Horario K4A", "Natación"]

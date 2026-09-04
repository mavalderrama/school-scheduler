"""Altas y bajas por texto: aditivas, versionadas y solo tras confirmar."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.db import repo
from app.db.models import ScheduleSlot, ScheduleTemplate, SourceKind, SourceStatus
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


# --- Fase 10.2: quitar una regla y cambiar una franja ------------------------------------------


K4A_SLOTS = (("A", 2, "Música"), ("A", 4, "Artes"), ("B", 2, "Deporte"))


async def seed_schedule(
    name: str = "Horario K4A", cycle: int = 2, child_id: int | None = None
) -> int:
    """`child_id` se resuelve dentro: `TENANT` lo rellena un fixture, no el import."""
    today = date.today()
    source = await repo.create_source(
        SourceKind.PHOTO, child_id=child_id if child_id is not None else TENANT.child_id
    )
    return await repo.apply_schedule(
        source.pk,
        ScheduleDraft(
            name=name,
            cycle_weeks=cycle,
            anchor_monday=today - timedelta(days=today.weekday()),
            slots=[
                SlotDraft(week_label=w, weekday=d, rotation="7", subject=subj)
                for w, d, subj in K4A_SLOTS
            ],
        ),
        valid_from=today,
    )


async def slot_named(schedule_id: int, subject: str) -> int:
    slots = await repo.schedule_slots(schedule_id)
    return next(s.pk for s in slots if s.subject == subject)


async def test_removing_a_schedule_versions_it_instead_of_deleting() -> None:
    schedule_id = await seed_schedule()
    edit = {"edit_id": 20, "chat_id": 1, "action": "remove_recurring", "schedule_id": schedule_id}

    assert "Quitado el horario" in await apply_edit(edit)

    assert await repo.active_schedules(TENANT.child_id, date.today()) == []
    # Las franjas siguen ahí: el pasado no se reescribe.
    assert len(await repo.schedule_slots(schedule_id)) == len(K4A_SLOTS)


async def test_removing_a_schedule_twice_is_harmless() -> None:
    schedule_id = await seed_schedule()
    edit = {"edit_id": 21, "chat_id": 1, "action": "remove_recurring", "schedule_id": schedule_id}
    await apply_edit(edit)
    assert "ya no está vigente" in await apply_edit(edit)


async def test_removing_a_schedule_of_another_family_does_nothing() -> None:
    other = await make_child("Otra", chat_id=-777030)
    theirs = await seed_schedule(name="Suyo", child_id=other.pk)

    edit = {"edit_id": 22, "chat_id": 1, "action": "remove_recurring", "schedule_id": theirs}
    assert "ya no está vigente" in await apply_edit(edit)

    assert [t.name for t in await repo.active_schedules(other.pk, date.today())] == ["Suyo"]


async def test_changing_a_slot_clones_the_template_with_everything_else_intact() -> None:
    """No es un UPDATE: nace una plantilla nueva y la vieja queda cerrada."""
    schedule_id = await seed_schedule()
    target = await slot_named(schedule_id, "Deporte")
    await ScheduleSlot.objects.filter(pk=target).aupdate(note="traer toalla")

    reply = await apply_edit(
        {"edit_id": 23, "chat_id": 1, "action": "edit_slot", "slot_id": target, "text": "evento"}
    )
    assert "Cambiado" in reply and "Deporte" in reply and "evento" in reply

    active = await repo.active_schedules(TENANT.child_id, date.today())
    assert len(active) == 1 and active[0].pk != schedule_id
    assert (active[0].name, active[0].cycle_weeks) == ("Horario K4A", 2)

    slots = await repo.schedule_slots(active[0].pk)
    assert sorted((s.week_label, s.weekday, s.subject) for s in slots) == [
        ("A", 2, "Música"),
        ("A", 4, "Artes"),
        ("B", 2, "evento"),
    ]
    # Se copia la fila entera: `rotation` y `note` no caben en un ScheduleDraft.
    changed = next(s for s in slots if s.weekday == 2 and s.week_label == "B")
    assert (changed.rotation, changed.note) == ("7", "traer toalla")


async def test_changing_a_slot_leaves_the_old_template_closed_but_readable() -> None:
    schedule_id = await seed_schedule()
    await apply_edit(
        {
            "edit_id": 24,
            "chat_id": 1,
            "action": "edit_slot",
            "slot_id": await slot_named(schedule_id, "Música"),
            "text": "evento",
        }
    )
    old = await ScheduleTemplate.objects.aget(pk=schedule_id)
    assert old.is_active is False
    assert old.superseded_by_id is not None
    assert old.valid_to is not None and old.valid_to >= old.valid_from


async def test_changing_a_slot_to_what_it_already_says_does_not_version() -> None:
    schedule_id = await seed_schedule()
    reply = await apply_edit(
        {
            "edit_id": 25,
            "chat_id": 1,
            "action": "edit_slot",
            "slot_id": await slot_named(schedule_id, "Música"),
            "text": "música",
        }
    )
    assert "Ya dice eso" in reply
    assert [t.pk for t in await repo.active_schedules(TENANT.child_id, date.today())] == [
        schedule_id
    ]


async def test_changing_a_slot_of_another_family_does_nothing() -> None:
    other = await make_child("Otra", chat_id=-777031)
    theirs = await seed_schedule(name="Suyo", child_id=other.pk)
    target = await slot_named(theirs, "Música")

    edit = {"edit_id": 26, "chat_id": 1, "action": "edit_slot", "slot_id": target, "text": "otra"}
    assert "ya no está vigente" in await apply_edit(edit)

    slots = await repo.schedule_slots(theirs)
    assert sorted(s.subject for s in slots) == ["Artes", "Deporte", "Música"]


async def test_choosing_a_candidate_carries_the_id_from_the_button() -> None:
    """Con varias franjas el `edit` no lleva id: lo pone el botón, y hay que hacerle caso."""
    schedule_id = await seed_schedule()
    chosen = await slot_named(schedule_id, "Deporte")

    reply = await apply_edit(
        {"edit_id": 27, "chat_id": 1, "action": "edit_slot", "text": "evento"}, chosen
    )
    assert "Cambiado" in reply and "Deporte" in reply

    active = await repo.active_schedules(TENANT.child_id, date.today())
    slots = await repo.schedule_slots(active[0].pk)
    assert next(s.subject for s in slots if s.week_label == "B") == "evento"


async def test_choosing_which_schedule_to_remove_comes_from_the_button() -> None:
    keep = await seed_schedule("Horario K4A")
    drop = await seed_schedule("Natación", cycle=1)

    reply = await apply_edit({"edit_id": 28, "chat_id": 1, "action": "remove_recurring"}, drop)
    assert "Natación" in reply

    assert [t.pk for t in await repo.active_schedules(TENANT.child_id, date.today())] == [keep]


async def test_confirming_a_recurring_also_removes_the_entries_it_covers() -> None:
    """Lo que se enseñó en la pregunta es lo que se ejecuta: ni más ni menos."""
    today = date.today()
    friday = today + timedelta(days=(4 - today.weekday()) % 7)
    await seed((friday, "event", "natación"), (friday, "bring", "toalla"))
    active = await repo.active_entries(TENANT.child_id, friday, friday)
    covered = next(e for e in active if e.text == "natación")

    reply = await apply_edit(
        {
            "edit_id": 30,
            "chat_id": 1,
            "action": "add_recurring",
            "weekdays": "5",
            "text": "natación",
            "drop_ids": [covered.pk],
        }
    )

    assert "Quité 1 entrada suelta" in reply
    left = await repo.active_entries(TENANT.child_id, friday, friday)
    # La toalla no se toca: solo se va lo que la regla cubre.
    assert [e.text for e in left] == ["toalla"]
    gone = await repo.get_entry(covered.pk, child_id=TENANT.child_id)
    assert gone is not None and gone.is_active is False and gone.superseded_by_id is not None
    assert [t.name for t in await repo.active_schedules(TENANT.child_id, today)] == ["Natación"]


async def test_a_recurring_without_duplicates_removes_nothing() -> None:
    reply = await apply_edit(
        {
            "edit_id": 31,
            "chat_id": 1,
            "action": "add_recurring",
            "weekdays": "5",
            "text": "natación",
        }
    )
    assert "Quité" not in reply

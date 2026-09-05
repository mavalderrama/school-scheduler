"""Texto libre: clasificación con caché y despacho a la lógica determinista."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time

import pytest

from app.config import Settings
from app.db import repo
from app.db.models import SourceKind
from app.llm.provider import LLMUnavailableError
from app.llm.schemas import (
    ChatTurn,
    ExtractedEntry,
    ExtractionResult,
    Intent,
    ScheduleDraft,
    SlotDraft,
)
from app.services import agenda, chat, scope
from app.services.scope import Scope
from tests.conftest import TENANT
from tests.test_ingest import providers
from tests.test_provider import FakeProvider

pytestmark = pytest.mark.django_db(transaction=True)


async def a_scope() -> Scope:
    """El ámbito de la familia por defecto de los tests."""
    found = await scope.for_child(TENANT.child_id)
    assert found is not None
    return found


MON, TUE, WED = date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9)
SAT, SUN = date(2026, 9, 12), date(2026, 9, 13)


class IntentProvider(FakeProvider):
    """Proveedor falso que devuelve una intención fija."""

    def __init__(self, name: str, intent: Intent, **kwargs: object) -> None:
        super().__init__(name, **kwargs)  # type: ignore[arg-type]
        self.intent = intent
        self.prompts: list[str] = []

    async def classify_intent(
        self, text: str, history: list[ChatTurn], today: date, has_pending: bool
    ) -> Intent:
        await self._maybe_fail()
        self.prompts.append(text)
        return self.intent


async def seed(*entries: tuple[date, str, str]) -> None:
    source = await repo.create_source(SourceKind.MANUAL, child_id=TENANT.child_id)
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


# --- Rango de la semana (puro) ------------------------------------------------------------


def test_week_range_from_midweek_and_weekend() -> None:
    assert chat.week_range(MON) == (MON, date(2026, 9, 13))
    assert chat.week_range(WED) == (WED, date(2026, 9, 13))
    # Sábado y domingo miran ya a la semana siguiente.
    assert chat.week_range(SAT) == (date(2026, 9, 14), date(2026, 9, 20))
    assert chat.week_range(SUN) == (date(2026, 9, 14), date(2026, 9, 20))


# --- Clasificación ---------------------------------------------------------------------------


async def test_classify_logs_the_call_and_caches_the_result(settings: Settings) -> None:
    fake = IntentProvider("claude_sdk", Intent(action="help"))
    chain = providers(fake)

    first = await chat.classify(
        "¿qué sabes hacer?", [], has_pending=False, settings=settings, providers=chain
    )
    second = await chat.classify(
        "¿qué sabes hacer?", [], has_pending=False, settings=settings, providers=chain
    )

    assert (first.action, second.action) == ("help", "help")
    assert fake.calls == 1  # la segunda sale de la caché
    assert [c.provider for c in await repo.llm_calls("intent")] == ["claude_sdk", "cache"]


async def test_classify_key_depends_on_history_and_pending(settings: Settings) -> None:
    fake = IntentProvider("a", Intent(action="help"))
    chain = providers(fake)
    await chat.classify("hola", [], has_pending=False, settings=settings, providers=chain)
    await chat.classify(
        "hola",
        [ChatTurn(role="user", content="algo")],
        has_pending=False,
        settings=settings,
        providers=chain,
    )
    await chat.classify("hola", [], has_pending=True, settings=settings, providers=chain)
    assert fake.calls == 3  # ninguna reutiliza la anterior


async def test_classify_propagates_llm_error_and_logs_attempts(settings: Settings) -> None:
    fake = IntentProvider("a", Intent(action="help"), fail=LLMUnavailableError("caído"))
    with pytest.raises(LLMUnavailableError):
        await chat.classify(
            "hola", [], has_pending=False, settings=settings, providers=providers(fake)
        )
    assert [(c.provider, c.ok) for c in await repo.llm_calls("intent")] == [("a", False)]


# --- Despacho ---------------------------------------------------------------------------------


async def test_query_single_day(settings: Settings) -> None:
    await seed((TUE, "bring", "sudadera"), (TUE, "homework", "pág. 12"), (WED, "note", "otra"))
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="query_range", date_from=TUE, date_to=TUE),
        today=MON,
        chat_id=1,
    )
    assert "martes 8 de septiembre" in reply.text
    assert "🎒 Llevar: sudadera" in reply.text
    assert "📝 Tarea: pág. 12" in reply.text
    assert "otra" not in reply.text
    assert reply.edit is None


async def test_query_range_groups_by_day(settings: Settings) -> None:
    await seed((TUE, "bring", "sudadera"), (WED, "event", "izada"))
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="query_range", date_from=MON, date_to=WED),
        today=MON,
        chat_id=1,
    )
    assert reply.text.index("martes 8") < reply.text.index("miércoles 9")


async def test_query_empty_range() -> None:
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="query_range", date_from=TUE, date_to=TUE),
        today=MON,
        chat_id=1,
    )
    assert "No tengo nada" in reply.text


async def test_query_without_dates_uses_today() -> None:
    await seed((MON, "note", "hoy toca"))
    reply = await chat.dispatch(await a_scope(), Intent(action="query_range"), today=MON, chat_id=1)
    assert "hoy toca" in reply.text


async def test_add_entry_asks_for_confirmation() -> None:
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="add_entry", date_from=TUE, kind="bring", text="disfraz"),
        today=MON,
        chat_id=1,
    )
    assert reply.edit is not None
    assert (reply.edit["action"], reply.edit["entry_date"], reply.edit["text"]) == (
        "add",
        TUE.isoformat(),
        "disfraz",
    )
    assert "¿Agrego" in reply.text and "disfraz" in reply.text
    # Todavía no ha tocado la DB: nada se guarda sin confirmar.
    assert await repo.active_entries(TENANT.child_id, TUE, TUE) == []


async def test_add_entry_without_data_asks_again() -> None:
    reply = await chat.dispatch(
        await a_scope(), Intent(action="add_entry", text="disfraz"), today=MON, chat_id=1
    )
    assert reply.edit is None
    assert "¿Para qué día" in reply.text


async def test_remove_single_candidate_asks_for_confirmation() -> None:
    await seed((WED, "event", "salida al parque"))
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="remove_entry", date_from=WED, target_entry_hint="salida"),
        today=MON,
        chat_id=1,
    )
    assert reply.edit is not None and reply.edit["action"] == "remove"
    assert reply.candidates is None
    assert "¿Quito" in reply.text
    assert (await repo.active_entries(TENANT.child_id, WED, WED))[0].is_active is True


async def test_remove_several_candidates_offers_a_choice() -> None:
    await seed((WED, "bring", "sudadera"), (WED, "bring", "botella"), (WED, "note", "x"))
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="remove_entry", date_from=WED),
        today=MON,
        chat_id=1,
    )
    assert reply.candidates is not None and len(reply.candidates) == 3
    assert all(len(label) <= 60 for _, label in reply.candidates)
    assert "¿Cuál quito?" in reply.text


async def test_remove_hint_without_matches_falls_back_to_the_whole_day() -> None:
    await seed((WED, "bring", "sudadera"))
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="remove_entry", date_from=WED, target_entry_hint="paraguas rojo"),
        today=MON,
        chat_id=1,
    )
    assert reply.edit is not None and reply.edit.get("entry_id") is not None


async def test_remove_with_nothing_there() -> None:
    reply = await chat.dispatch(
        await a_scope(), Intent(action="remove_entry", date_from=WED), today=MON, chat_id=1
    )
    assert "No encontré nada" in reply.text
    assert reply.edit is None


async def test_help_and_unknown() -> None:
    assert (
        "agenda escolar"
        in (await chat.dispatch(await a_scope(), Intent(action="help"), today=MON, chat_id=1)).text
    )
    assert (
        "No te entendí"
        in (
            await chat.dispatch(await a_scope(), Intent(action="unknown"), today=MON, chat_id=1)
        ).text
    )


# --- Fase 6: consultas que usan el horario ---------------------------------------------------


async def seed_schedule(anchor: date = date(2026, 8, 31)) -> None:
    from app.llm.schemas import ScheduleDraft, SlotDraft

    source = await repo.create_source(SourceKind.PHOTO, chat_id=1, child_id=TENANT.child_id)
    await agenda.apply_source(
        source.pk,
        ExtractionResult(
            entries=[],
            doubts=[],
            detected_language="es",
            doc_type="schedule",
            schedule=ScheduleDraft(
                name="Horario K4A",
                cycle_weeks=2,
                anchor_monday=anchor,
                slots=[
                    SlotDraft(week_label="A", weekday=1, rotation="1", subject="Artes plásticas"),
                    SlotDraft(week_label="B", weekday=4, rotation="9", subject="Natación"),
                ],
            ),
        ),
        today=anchor,
    )


async def test_query_subject_answers_when_a_class_happens() -> None:
    await seed_schedule()
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="query_subject", subject="natación"),
        today=MON,
        chat_id=1,
    )
    assert "Natación" in reply.text
    assert "jueves 10 de septiembre" in reply.text
    assert "rot. 9" in reply.text


async def test_query_subject_ignores_accents() -> None:
    await seed_schedule()
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="query_subject", subject="NATACION"),
        today=MON,
        chat_id=1,
    )
    assert "Natación" in reply.text


async def test_query_subject_without_a_schedule_says_so() -> None:
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="query_subject", subject="natación"),
        today=MON,
        chat_id=1,
    )
    assert "no tengo ningún horario" in reply.text.lower()


async def test_query_subject_for_something_not_in_the_schedule() -> None:
    await seed_schedule()
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="query_subject", subject="ajedrez"),
        today=MON,
        chat_id=1,
    )
    assert "No encuentro" in reply.text and "/horario" in reply.text


async def test_query_subject_without_a_subject_asks_for_one() -> None:
    reply = await chat.dispatch(
        await a_scope(), Intent(action="query_subject"), today=MON, chat_id=1
    )
    assert "¿De qué materia?" in reply.text


async def test_a_single_day_query_includes_the_class() -> None:
    """«¿qué hay el jueves?» tiene que decir también qué clase toca."""
    await seed_schedule()
    await seed((date(2026, 9, 10), "bring", "gorro de piscina"))
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="query_range", date_from=date(2026, 9, 10), date_to=date(2026, 9, 10)),
        today=MON,
        chat_id=1,
    )
    assert "Natación" in reply.text
    assert "gorro de piscina" in reply.text
    assert reply.text.index("Natación") < reply.text.index("gorro")


async def test_a_day_with_only_a_class_is_not_empty() -> None:
    await seed_schedule()
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="query_range", date_from=date(2026, 9, 10), date_to=date(2026, 9, 10)),
        today=MON,
        chat_id=1,
    )
    assert "Natación" in reply.text
    assert "No tengo nada" not in reply.text


# --- Fase 10: recordatorios ------------------------------------------------------------------


async def test_add_reminder_asks_for_confirmation_and_saves_nothing() -> None:
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="add_reminder", text="el disfraz", time_of_day="07:00", repeat="daily"),
        today=MON,
        chat_id=-4242,
    )

    assert reply.edit is not None
    assert reply.edit["action"] == "add_reminder"
    assert reply.edit["time_of_day"] == "07:00"
    assert "07:00" in reply.text and "el disfraz" in reply.text
    # Nada en la DB hasta el ✅.
    assert await repo.reminders_of(TENANT.child_id) == []


async def test_without_an_hour_it_asks_instead_of_guessing() -> None:
    """El prompt tiene prohibido inventarse una hora ambigua; aquí se pregunta."""
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="add_reminder", text="el disfraz"),
        today=MON,
        chat_id=1,
    )

    assert reply.edit is None
    assert "hora" in reply.text.lower()


async def test_weekly_without_days_asks_which_ones() -> None:
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="add_reminder", text="natación", time_of_day="07:00", repeat="weekly"),
        today=MON,
        chat_id=1,
    )
    assert reply.edit is None
    assert "días" in reply.text


async def test_a_weekly_reminder_carries_its_days() -> None:
    reply = await chat.dispatch(
        await a_scope(),
        Intent(
            action="add_reminder",
            text="natación",
            time_of_day="17:30",
            repeat="weekly",
            weekdays=[3, 1],
        ),
        today=MON,
        chat_id=1,
    )
    assert reply.edit is not None and reply.edit["weekdays"] == "13"
    assert "lunes y miércoles" in reply.text


async def test_listing_reminders_needs_no_confirmation() -> None:
    await repo.create_reminder(
        child_id=TENANT.child_id,
        chat_id=1,
        text="revisar la agenda",
        time_of_day=dt_time(7, 0),
        repeat="daily",
        next_fire_at=datetime.now(UTC) + timedelta(hours=1),
    )

    reply = await chat.dispatch(
        await a_scope(), Intent(action="list_reminders"), today=MON, chat_id=1
    )

    assert reply.edit is None and reply.candidates is None
    assert "revisar la agenda" in reply.text


async def test_listing_with_nothing_says_so() -> None:
    reply = await chat.dispatch(
        await a_scope(), Intent(action="list_reminders"), today=MON, chat_id=1
    )
    assert "No tienes recordatorios" in reply.text


async def test_removing_with_several_candidates_offers_a_choice() -> None:
    for text in ("natación del martes", "natación del jueves"):
        await repo.create_reminder(
            child_id=TENANT.child_id,
            chat_id=1,
            text=text,
            time_of_day=dt_time(7, 0),
            repeat="daily",
            next_fire_at=datetime.now(UTC) + timedelta(hours=1),
        )

    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="remove_reminder", target_entry_hint="natación"),
        today=MON,
        chat_id=1,
    )

    assert reply.candidates is not None and len(reply.candidates) == 2
    assert all(len(label) <= 60 for _, label in reply.candidates)
    assert reply.edit is not None and "reminder_id" not in reply.edit


async def test_removing_a_single_candidate_asks_yes_or_no() -> None:
    saved = await repo.create_reminder(
        child_id=TENANT.child_id,
        chat_id=1,
        text="el disfraz",
        time_of_day=dt_time(7, 0),
        repeat="daily",
        next_fire_at=datetime.now(UTC) + timedelta(hours=1),
    )

    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="remove_reminder", target_entry_hint="disfraz"),
        today=MON,
        chat_id=1,
    )

    assert reply.candidates is None
    assert reply.edit is not None and reply.edit["reminder_id"] == saved.pk
    assert "¿Quito" in reply.text


# --- Fase 10.1: lo que se repite cada semana --------------------------------------------------


async def test_a_weekly_activity_is_understood_instead_of_unknown() -> None:
    """«Todos los viernes tiene natación» no cabía en ninguna acción y moría en «unknown»."""
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="add_recurring", text="natación", weekdays=[5]),
        today=MON,
        chat_id=-4242,
    )

    assert reply.edit is not None
    assert reply.edit["action"] == "add_recurring"
    assert reply.edit["weekdays"] == "5"
    assert "natación" in reply.text and "viernes" in reply.text
    # Nada guardado hasta el ✅.
    assert await repo.active_schedules(TENANT.child_id) == []


async def test_a_recurring_without_days_asks_which_ones() -> None:
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="add_recurring", text="natación"),
        today=MON,
        chat_id=1,
    )
    assert reply.edit is None
    assert "días" in reply.text


async def test_a_recurring_without_text_asks_what() -> None:
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="add_recurring", weekdays=[5]),
        today=MON,
        chat_id=1,
    )
    assert reply.edit is None
    assert "apunto" in reply.text.lower()


# --- Fase 10.2: quitar un horario y cambiar una franja, hablando ------------------------------


async def seed_ab_schedule(name: str = "Horario K4A", cycle: int = 2) -> int:
    """Un horario A/B vigente desde hoy, con dos materias el martes."""
    today = date.today()
    source = await repo.create_source(SourceKind.PHOTO, child_id=TENANT.child_id)
    return await repo.apply_schedule(
        source.pk,
        ScheduleDraft(
            name=name,
            cycle_weeks=cycle,
            anchor_monday=today - timedelta(days=today.weekday()),
            slots=[
                SlotDraft(week_label="A", weekday=2, subject="Música"),
                SlotDraft(week_label="B", weekday=2, subject="Deporte"),
            ],
        ),
        valid_from=today,
    )


async def test_removing_the_only_schedule_asks_for_confirmation() -> None:
    schedule_id = await seed_ab_schedule()
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="remove_recurring", target_entry_hint="el horario"),
        today=date.today(),
        chat_id=1,
    )

    assert reply.edit is not None
    assert reply.edit["action"] == "remove_recurring"
    assert reply.edit["schedule_id"] == schedule_id
    # Sigue vigente: nada se toca hasta el ✅.
    assert len(await repo.active_schedules(TENANT.child_id, date.today())) == 1


async def test_removing_with_several_schedules_offers_the_names() -> None:
    await seed_ab_schedule("Horario K4A")
    await seed_ab_schedule("Natación", cycle=1)

    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="remove_recurring", target_entry_hint="lo que sea"),
        today=date.today(),
        chat_id=1,
    )
    assert reply.candidates is not None
    assert sorted(label for _, label in reply.candidates) == ["Horario K4A", "Natación"]


async def test_removing_by_name_goes_straight_to_the_right_one() -> None:
    await seed_ab_schedule("Horario K4A")
    swim = await seed_ab_schedule("Natación", cycle=1)

    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="remove_recurring", target_entry_hint="natación"),
        today=date.today(),
        chat_id=1,
    )
    assert reply.edit is not None and reply.edit["schedule_id"] == swim
    assert reply.candidates is None


async def test_without_any_schedule_there_is_nothing_to_remove() -> None:
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="remove_recurring", target_entry_hint="natación"),
        today=date.today(),
        chat_id=1,
    )
    assert reply.edit is None
    assert "horario cargado" in reply.text


async def test_changing_a_slot_picks_the_week_that_was_named() -> None:
    """«El martes de la Semana B»: el día solo no basta, hay dos martes en el ciclo."""
    schedule_id = await seed_ab_schedule()
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="edit_slot", weekdays=[2], week_label="B", text="evento"),
        today=date.today(),
        chat_id=1,
    )

    assert reply.edit is not None and reply.candidates is None
    assert reply.edit["action"] == "edit_slot" and reply.edit["text"] == "evento"
    assert "Deporte" in reply.text and "Semana B" in reply.text

    slots = await repo.schedule_slots(schedule_id)
    assert reply.edit["slot_id"] == next(s.pk for s in slots if s.subject == "Deporte")


async def test_changing_a_slot_without_the_week_offers_both() -> None:
    await seed_ab_schedule()
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="edit_slot", weekdays=[2], text="evento"),
        today=date.today(),
        chat_id=1,
    )
    assert reply.candidates is not None and len(reply.candidates) == 2
    assert all("martes" in label for _, label in reply.candidates)


async def test_changing_a_slot_needs_a_day_and_a_subject() -> None:
    await seed_ab_schedule()
    no_day = await chat.dispatch(
        await a_scope(),
        Intent(action="edit_slot", text="evento"),
        today=date.today(),
        chat_id=1,
    )
    assert no_day.edit is None and "día" in no_day.text

    no_subject = await chat.dispatch(
        await a_scope(),
        Intent(action="edit_slot", weekdays=[2]),
        today=date.today(),
        chat_id=1,
    )
    assert no_subject.edit is None and "materia" in no_subject.text


async def test_a_day_with_no_slot_is_said_not_invented() -> None:
    await seed_ab_schedule()
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="edit_slot", weekdays=[5], text="evento"),
        today=date.today(),
        chat_id=1,
    )
    assert reply.edit is None
    assert "No encontré esa franja" in reply.text


# --- Fase 10.3: «los viernes» no es una fecha --------------------------------------------------


def test_recurring_days_reads_the_article_not_the_verb() -> None:
    """El plural es la señal, y se comprueba en Python: el modelo lo resolvió como fecha."""
    assert chat.recurring_days("Agrega que los viernes tiene natación") == [5]
    assert chat.recurring_days("todos los martes y los jueves hay refuerzo") == [2, 4]
    assert chat.recurring_days("cada miércoles lleva flauta") == [3]
    # Singular: un día concreto, no una regla.
    assert chat.recurring_days("agrega que el martes lleva disfraz") == []
    assert chat.recurring_days("¿qué hay el viernes?") == []
    # Sin día no hay regla que construir, por mucho marcador que haya.
    assert chat.recurring_days("esto es recurrente") == []


def test_recurring_days_understands_saying_it_afterwards() -> None:
    assert chat.recurring_days("El evento de natación del viernes es recurrente") == [5]
    assert chat.recurring_days("lo del jueves se repite todas las semanas") == [4]


async def test_a_plural_weekday_becomes_a_rule_even_if_the_model_said_add_entry() -> None:
    """El bug de producción: «los viernes» se guardó como una entrada del viernes 4."""
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="add_entry", date_from=date(2026, 9, 4), kind="event", text="natación"),
        today=date(2026, 9, 4),
        chat_id=1,
        text="Agrega que los viernes tiene natación",
    )

    assert reply.edit is not None
    assert reply.edit["action"] == "add_recurring"
    assert reply.edit["weekdays"] == "5"
    assert reply.edit["text"] == "natación"


async def test_a_singular_weekday_is_still_a_single_entry() -> None:
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="add_entry", date_from=TUE, kind="bring", text="disfraz"),
        today=MON,
        chat_id=1,
        text="agrega que el martes lleva disfraz",
    )
    assert reply.edit is not None and reply.edit["action"] == "add"


async def test_saying_it_is_recurrent_converts_the_entry_the_model_could_not_place() -> None:
    """«El evento de natación del viernes es recurrente» acababa en «no te entendí»."""
    today = date.today()
    friday = today + timedelta(days=(4 - today.weekday()) % 7)
    await seed((friday, "event", "natación"))

    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="unknown"),
        today=today,
        chat_id=1,
        text="El evento de natación del viernes es recurrente",
    )

    assert reply.edit is not None
    assert (reply.edit["action"], reply.edit["weekdays"]) == ("add_recurring", "5")
    assert reply.edit["text"] == "natación"
    # Y la entrada suelta se ofrece quitar en la misma pregunta, no a escondidas.
    entry = (await repo.active_entries(TENANT.child_id, friday, friday))[0]
    assert reply.edit["drop_ids"] == [entry.pk]
    assert "quito estas" in reply.text.lower()


async def test_without_a_matching_entry_it_still_says_it_did_not_understand() -> None:
    """Preferible a inventarse de qué hablaba: no hay nada con qué construir la regla."""
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="unknown"),
        today=date.today(),
        chat_id=1,
        text="lo del viernes es recurrente",
    )
    assert reply.edit is None
    assert "No te entendí" in reply.text


# --- Fase 10.4: un rango también mira el horario ----------------------------------------------


async def test_a_week_query_includes_the_schedule() -> None:
    """El bug: recién guardado «natación los viernes», «¿qué hay la próxima semana?» decía
    que no había nada. `/semana` sí lo enseñaba: el mismo dato dependía de por dónde se
    preguntara."""
    today = date.today()
    monday = today + timedelta(days=7 - today.weekday())
    await agenda.add_recurring(await a_scope(), "5", "natación", today=today)

    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="query_range", date_from=monday, date_to=monday + timedelta(days=6)),
        today=today,
        chat_id=1,
    )

    friday = monday + timedelta(days=4)
    assert "natación" in reply.text
    assert f"viernes {friday.day}" in reply.text
    assert "No tengo nada entre" not in reply.text
    # El fin de semana no se reporta: que no haya clase el sábado no es noticia.
    assert "sábado" not in reply.text and "domingo" not in reply.text


async def test_a_range_still_lists_the_entries_next_to_the_schedule() -> None:
    today = date.today()
    monday = today + timedelta(days=7 - today.weekday())
    await agenda.add_recurring(await a_scope(), "5", "natación", today=today)
    await seed((monday, "bring", "sudadera"))

    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="query_range", date_from=monday, date_to=monday + timedelta(days=6)),
        today=today,
        chat_id=1,
    )
    assert "natación" in reply.text and "sudadera" in reply.text


async def test_a_range_without_any_schedule_answers_as_before() -> None:
    today = date.today()
    monday = today + timedelta(days=7 - today.weekday())
    reply = await chat.dispatch(
        await a_scope(),
        Intent(action="query_range", date_from=monday, date_to=monday + timedelta(days=6)),
        today=today,
        chat_id=1,
    )
    assert "No tengo nada entre" in reply.text

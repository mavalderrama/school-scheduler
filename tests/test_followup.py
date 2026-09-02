"""Fase 6: el bot pregunta lo que falta antes de guardar, y guarda el horario versionado.

El caso que motivó la fase: una foto del horario rotativo no trae la fecha en que empezó
el ciclo, así que sin preguntar es inservible. Antes el bot mostraba las dudas y ya.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.config import Settings
from app.db import repo
from app.db.models import ScheduleTemplate, SourceKind, SourceStatus
from app.llm.provider import LLMUnavailableError
from app.llm.schemas import ExtractionResult, QAPair, ScheduleDraft, SlotDraft
from app.services import agenda, ingest, schedule
from tests.test_ingest import providers
from tests.test_provider import FakeProvider

pytestmark = pytest.mark.django_db(transaction=True)

ANCHOR = date(2026, 8, 31)
K4A = [
    ("A", 1, "1", "Artes plásticas"),
    ("A", 2, "2", "Expresión corporal"),
    ("A", 3, "3", "Deporte 1"),
    ("A", 4, "4", "Música"),
    ("A", 5, "5", "Deporte 3"),
    ("B", 1, "6", "Deporte 2"),
    ("B", 2, "7", "Motricidad y creatividad"),
    ("B", 3, "8", "Tecnología"),
    ("B", 4, "9", "Natación"),
    ("B", 5, "Cultural", "Encuentro de expedición"),
]


def draft(*, anchor: date | None = None) -> ScheduleDraft:
    return ScheduleDraft(
        name="Horario K4A",
        cycle_weeks=2,
        anchor_monday=anchor,
        slots=[
            SlotDraft(week_label=label, weekday=day, rotation=rot, subject=subject)
            for label, day, rot, subject in K4A
        ],
    )


def extraction(
    *, anchor: date | None = None, questions: list[str] | None = None
) -> ExtractionResult:
    return ExtractionResult(
        entries=[],
        doubts=[],
        detected_language="es",
        doc_type="schedule",
        schedule=draft(anchor=anchor),
        questions=questions or [],
    )


# --- Qué falta lo decide Python ---------------------------------------------------------------


def test_a_schedule_without_an_anchor_is_not_savable() -> None:
    missing = ingest.missing_essentials(extraction())
    assert len(missing) == 1
    assert "Semana A" in missing[0]


def test_a_schedule_with_an_anchor_needs_nothing() -> None:
    assert ingest.missing_essentials(extraction(anchor=ANCHOR)) == []


def test_an_anchor_that_is_not_a_monday_is_rejected() -> None:
    """El modelo puede devolver el martes que citó el usuario; eso no es un ancla válida."""
    missing = ingest.missing_essentials(extraction(anchor=date(2026, 9, 1)))
    assert len(missing) == 1 and "no es lunes" in missing[0]


def test_a_schedule_with_no_rows_asks_for_a_better_photo() -> None:
    empty = ExtractionResult(
        entries=[],
        doubts=[],
        detected_language="es",
        doc_type="schedule",
        schedule=ScheduleDraft(slots=[]),
    )
    assert "foto" in ingest.missing_essentials(empty)[0]


def test_a_plain_agenda_photo_is_never_interrogated() -> None:
    """Las fotos de agenda diaria siguen exactamente como en la Fase 1."""
    plain = ExtractionResult(entries=[], doubts=["borroso"], detected_language="es")
    assert ingest.missing_essentials(plain) == []


def test_essentials_come_first_and_model_questions_are_appended() -> None:
    questions = ingest.pending_questions(extraction(questions=["¿Es de este año?"]))
    assert "Semana A" in questions[0]  # lo imprescindible primero
    assert questions[1] == "¿Es de este año?"


def test_duplicate_questions_are_not_asked_twice() -> None:
    dup = extraction(anchor=ANCHOR, questions=["¿Es de este año?", "  ¿ES DE ESTE AÑO?  "])
    assert ingest.pending_questions(dup) == ["¿Es de este año?"]


# --- El interrogatorio ------------------------------------------------------------------------


async def test_the_answer_completes_the_extraction(settings: Settings) -> None:
    """Refinar devuelve la extracción con el ancla puesta y ya no falta nada."""
    source = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
    provider = FakeProvider("claude_sdk")
    provider.refined = extraction(anchor=ANCHOR)

    refined = await ingest.refine_extraction(
        source.pk,
        extraction(),
        [QAPair(question="¿Qué lunes empezó la Semana A?", answer="el martes 1 de septiembre")],
        settings,
        providers(provider),
    )

    assert refined.schedule is not None
    assert refined.schedule.anchor_monday == ANCHOR
    assert ingest.missing_essentials(refined) == []
    # La respuesta llegó al proveedor tal cual, como dato.
    assert provider.refinements[0][0].answer == "el martes 1 de septiembre"
    # Y quedó registrada la llamada.
    assert [c.provider for c in await repo.llm_calls("refine")] == ["claude_sdk"]


async def test_refining_twice_the_same_thing_uses_the_cache(settings: Settings) -> None:
    source = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
    provider = FakeProvider("claude_sdk")
    provider.refined = extraction(anchor=ANCHOR)
    pairs = [QAPair(question="¿Qué lunes?", answer="el 31 de agosto")]

    await ingest.refine_extraction(source.pk, extraction(), pairs, settings, providers(provider))
    await ingest.refine_extraction(source.pk, extraction(), pairs, settings, providers(provider))

    assert provider.calls == 1
    assert [c.provider for c in await repo.llm_calls("refine")] == ["claude_sdk", "cache"]


async def test_a_failed_refine_is_logged_and_raises(settings: Settings) -> None:
    source = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
    provider = FakeProvider("a", fail=LLMUnavailableError("caído"))
    with pytest.raises(LLMUnavailableError):
        await ingest.refine_extraction(
            source.pk,
            extraction(),
            [QAPair(question="q", answer="r")],
            settings,
            providers(provider),
        )
    assert [(c.provider, c.ok) for c in await repo.llm_calls("refine")] == [("a", False)]


# --- Guardar el horario -----------------------------------------------------------------------


async def test_applying_a_schedule_stores_the_slots_and_confirms_the_source() -> None:
    source = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
    result = await agenda.apply_source(source.pk, extraction(anchor=ANCHOR), today=date(2026, 9, 2))

    assert result.schedule_id is not None
    assert result.slots == 10
    assert result.dates == []  # un horario no cubre fechas concretas

    refreshed = await repo.get_source(source.pk)
    assert refreshed is not None and refreshed.status == SourceStatus.CONFIRMED

    template = await repo.active_schedule(date(2026, 9, 2))
    assert template is not None
    assert (template.name, template.cycle_weeks, template.anchor_monday) == (
        "Horario K4A",
        2,
        ANCHOR,
    )
    slots = await repo.schedule_slots(template.pk)
    assert len(slots) == 10
    assert {s.week_label for s in slots} == {"A", "B"}
    # «Cultural» se guardó como texto, no como número.
    assert any(s.rotation == "Cultural" for s in slots)


async def test_applying_a_schedule_without_an_anchor_refuses() -> None:
    """Defensa en profundidad: aunque el interrogatorio falle, la DB no acepta esto."""
    source = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
    with pytest.raises(ValueError, match="ancla"):
        await agenda.apply_source(source.pk, extraction(), today=date(2026, 9, 2))


async def test_a_second_schedule_supersedes_the_first_without_deleting() -> None:
    first = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
    await agenda.apply_source(first.pk, extraction(anchor=ANCHOR), today=date(2026, 9, 2))

    second = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
    await agenda.apply_source(second.pk, extraction(anchor=ANCHOR), today=date(2026, 10, 5))

    templates = [t async for t in ScheduleTemplate.objects.all().order_by("id")]
    assert len(templates) == 2  # nada se borra
    assert templates[0].is_active is False
    assert templates[0].superseded_by_id == second.pk
    assert templates[0].valid_to == date(2026, 10, 4)  # se cierra el día antes
    assert templates[1].is_active is True

    # La plantilla vigente hoy es la nueva.
    current = await repo.active_schedule(date(2026, 10, 5))
    assert current is not None and current.pk == templates[1].pk


async def test_duplicate_rows_in_the_photo_do_not_break_the_unique() -> None:
    """El modelo puede repetir una fila de la tabla; se queda la primera."""
    source = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
    noisy = extraction(anchor=ANCHOR)
    assert noisy.schedule is not None
    noisy.schedule.slots.append(
        SlotDraft(week_label="A", weekday=1, rotation="1", subject="Artes plásticas (repetida)")
    )
    result = await agenda.apply_source(source.pk, noisy, today=date(2026, 9, 2))
    assert result.slots == 10
    template = await repo.active_schedule(date(2026, 9, 2))
    assert template is not None
    slots = await repo.schedule_slots(template.pk)
    assert [s.subject for s in slots if s.week_index == 0 and s.weekday == 1] == ["Artes plásticas"]


# --- De punta a punta: la foto real -----------------------------------------------------------


async def test_the_stored_schedule_answers_what_is_tomorrow() -> None:
    """Lo que el usuario pidió: guardar la tabla y que el bot sepa qué toca."""
    source = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
    await agenda.apply_source(source.pk, extraction(anchor=ANCHOR), today=date(2026, 9, 2))

    wednesday = await schedule.resolve(date(2026, 9, 2))
    thursday = await schedule.resolve(date(2026, 9, 3))
    natacion = await schedule.find_subject("natacion", date(2026, 9, 2), count=1)

    assert wednesday is not None and wednesday.subject == "Deporte 1"
    assert thursday is not None and thursday.subject == "Música"
    assert [s.day for s in natacion] == [date(2026, 9, 10)]


async def test_without_a_schedule_resolve_says_nothing_rather_than_guessing() -> None:
    assert await schedule.resolve(date(2026, 9, 2)) is None
    assert await schedule.find_subject("natación", date(2026, 9, 2)) == []

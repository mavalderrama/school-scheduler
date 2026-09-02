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
from app.llm.provider import LLMQuotaError, LLMUnavailableError
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


async def test_two_schedules_coexist_by_default() -> None:
    """Bug real: el segundo horario borraba al primero sin preguntar.

    La rotación académica y el programa de la jornada extendida son cosas distintas y el
    mismo día tiene las dos, así que por defecto se añade, no se reemplaza.
    """
    first = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
    await agenda.apply_source(first.pk, extraction(anchor=ANCHOR), today=date(2026, 9, 2))

    second = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
    pac = extraction(anchor=ANCHOR)
    assert pac.schedule is not None
    pac.schedule.name = "PAC - jornada extendida"
    await agenda.apply_source(second.pk, pac, today=date(2026, 10, 5))

    active = await repo.active_schedules(date(2026, 10, 5))
    assert len(active) == 2
    assert {t.name for t in active} == {"Horario K4A", "PAC - jornada extendida"}


async def test_replacing_is_explicit_and_supersedes_without_deleting() -> None:
    first = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
    await agenda.apply_source(first.pk, extraction(anchor=ANCHOR), today=date(2026, 9, 2))
    old = await repo.active_schedule(date(2026, 9, 2))
    assert old is not None

    second = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
    await agenda.apply_source(
        second.pk, extraction(anchor=ANCHOR), today=date(2026, 10, 5), replace_ids=[old.pk]
    )

    templates = [t async for t in ScheduleTemplate.objects.all().order_by("id")]
    assert len(templates) == 2  # nada se borra
    assert templates[0].is_active is False
    assert templates[0].superseded_by_id == second.pk
    assert templates[0].valid_to == date(2026, 10, 4)  # se cierra el día antes
    assert templates[1].is_active is True
    assert [t.pk for t in await repo.active_schedules(date(2026, 10, 5))] == [templates[1].pk]


async def test_replacing_the_same_day_never_closes_before_it_opened() -> None:
    """Regresión: reemplazar el mismo día dejaba `valid_to` anterior a `valid_from`."""
    first = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
    await agenda.apply_source(first.pk, extraction(anchor=ANCHOR), today=date(2026, 9, 2))
    old = await repo.active_schedule(date(2026, 9, 2))
    assert old is not None

    second = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
    await agenda.apply_source(
        second.pk, extraction(anchor=ANCHOR), today=date(2026, 9, 2), replace_ids=[old.pk]
    )

    superseded = await ScheduleTemplate.objects.aget(pk=old.pk)
    assert superseded.valid_to == superseded.valid_from  # nunca antes de empezar


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


# --- Convivencia de horarios y pie de foto ---------------------------------------------------


def pac_draft() -> ExtractionResult:
    """El PAC real: ciclo de 1 semana, sin viernes, jornada extendida."""
    return ExtractionResult(
        entries=[],
        doubts=[],
        detected_language="es",
        doc_type="schedule",
        schedule=ScheduleDraft(
            name="PAC - jornada extendida",
            cycle_weeks=1,
            anchor_monday=ANCHOR,
            slots=[
                SlotDraft(week_label="A", weekday=1, subject="Jornada extendida de fútbol"),
                SlotDraft(week_label="A", weekday=2, subject="Jornada extendida de natación"),
                SlotDraft(week_label="A", weekday=3, subject="Jornada extendida de fútbol"),
                SlotDraft(week_label="A", weekday=4, subject="Jornada extendida de natación"),
            ],
        ),
    )


async def test_a_day_shows_every_active_schedule() -> None:
    """El caso que falló: con la rotación y el PAC cargados, el jueves tiene las dos."""
    for ext in (extraction(anchor=ANCHOR), pac_draft()):
        src = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
        await agenda.apply_source(src.pk, ext, today=date(2026, 9, 2))

    slots = await schedule.resolve_day(date(2026, 9, 3))  # jueves, Semana A
    assert {s.subject for s in slots} == {"Música", "Jornada extendida de natación"}
    assert {s.schedule_name for s in slots} == {"Horario K4A", "PAC - jornada extendida"}


async def test_a_one_week_cycle_never_alternates() -> None:
    """El PAC se repite igual todas las semanas: cycle_weeks=1."""
    src = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
    await agenda.apply_source(src.pk, pac_draft(), today=date(2026, 9, 2))
    for day in (date(2026, 9, 3), date(2026, 9, 10), date(2026, 9, 17)):
        slots = await schedule.resolve_day(day)
        assert [s.subject for s in slots] == ["Jornada extendida de natación"]


async def test_a_day_the_pac_does_not_cover_only_shows_the_other() -> None:
    """El PAC no tiene viernes: ese día solo aparece la rotación académica."""
    for ext in (extraction(anchor=ANCHOR), pac_draft()):
        src = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
        await agenda.apply_source(src.pk, ext, today=date(2026, 9, 2))
    slots = await schedule.resolve_day(date(2026, 9, 4))  # viernes, Semana A
    assert [s.subject for s in slots] == ["Deporte 3"]


async def test_a_holiday_is_reported_once_not_once_per_schedule() -> None:
    for ext in (extraction(anchor=ANCHOR), pac_draft()):
        src = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
        await agenda.apply_source(src.pk, ext, today=date(2026, 9, 2))
    slots = await schedule.resolve_day(date(2026, 10, 12))  # Día de la Raza
    assert len(slots) == 1
    assert slots[0].subject is None and slots[0].skipped_reason == "Día de la Raza"


async def test_find_subject_looks_in_every_schedule() -> None:
    for ext in (extraction(anchor=ANCHOR), pac_draft()):
        src = await repo.create_source(SourceKind.PHOTO, chat_id=-100)
        await agenda.apply_source(src.pk, ext, today=date(2026, 9, 2))
    found = await schedule.find_subject("natación", date(2026, 9, 2), count=3)
    # El PAC tiene natación los martes y jueves; la rotación, los jueves de Semana B.
    assert [s.day for s in found] == [date(2026, 9, 3), date(2026, 9, 8), date(2026, 9, 10)]


async def test_the_photo_caption_reaches_the_model(settings: Settings) -> None:
    """Bug real: lo que el usuario escribía junto a la foto se descartaba sin usarlo."""
    from tests.test_ingest import fake_download

    provider = FakeProvider("claude_sdk", result=pac_draft())
    await ingest.ingest_photo(
        file_id="f",
        user_id=111,
        display_name="Alejandro",
        chat_id=-100,
        download=fake_download,
        settings=settings,
        providers=providers(provider),
        note="Debes marcar este como PAC horario extendido",
    )
    assert provider.notes == ["Debes marcar este como PAC horario extendido"]


async def test_the_caption_is_part_of_the_cache_key(settings: Settings) -> None:
    """La misma foto con otra indicación es otra lectura, no un acierto de caché."""
    from tests.test_ingest import fake_download

    provider = FakeProvider("claude_sdk", result=pac_draft())
    for note in ("es el horario del PAC", "es el horario del PAC", "en realidad es la agenda"):
        await ingest.ingest_photo(
            file_id="f",
            user_id=111,
            display_name="Alejandro",
            chat_id=-100,
            download=fake_download,
            settings=settings,
            providers=providers(provider),
            note=note,
        )
    assert provider.calls == 2  # la segunda repetición sí sale de la caché


async def test_the_caption_survives_a_restart(settings: Settings) -> None:
    """Una foto reintentada tras quedarse sin cuota tiene que releerse con su indicación."""
    from tests.test_ingest import fake_download

    provider = FakeProvider("claude_sdk", fail=LLMQuotaError("límite"))
    with pytest.raises(ingest.IngestError) as info:
        await ingest.ingest_photo(
            file_id="f",
            user_id=111,
            display_name="Alejandro",
            chat_id=-100,
            download=fake_download,
            settings=settings,
            providers=providers(provider),
            note="Debes marcar este como PAC horario extendido",
        )
    source = await repo.get_source(info.value.source_id)
    assert source is not None
    assert source.caption == "Debes marcar este como PAC horario extendido"

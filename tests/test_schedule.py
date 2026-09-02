"""Aritmética del horario rotativo A/B, contra el horario real K4A.

Sin DB: los modelos se instancian sin guardar. La tabla de la foto es la fuente de verdad
de estos tests, así que un cambio en la fórmula del ciclo se nota aquí y no en producción.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.db.models import CalendarKind, HolidayPolicy, ScheduleSlot, ScheduleTemplate
from app.services import schedule

# Lunes ancla de la Semana A: el usuario dijo que la Semana A empezó el martes 2026-09-01.
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


def make_template(**overrides: object) -> ScheduleTemplate:
    values: dict[str, object] = {
        "name": "Horario K4A",
        "anchor_monday": ANCHOR,
        "cycle_weeks": 2,
        "valid_from": ANCHOR,
        "valid_to": None,
        "holiday_policy": HolidayPolicy.SKIP_DAY,
    }
    values.update(overrides)
    return ScheduleTemplate(**values)


def make_slots() -> list[ScheduleSlot]:
    labels = {"A": 0, "B": 1}
    return [
        ScheduleSlot(
            week_index=labels[label],
            week_label=label,
            weekday=weekday,
            rotation=rotation,
            subject=subject,
        )
        for label, weekday, rotation, subject in K4A
    ]


@pytest.fixture
def template() -> ScheduleTemplate:
    return make_template()


@pytest.fixture
def slots() -> list[ScheduleSlot]:
    return make_slots()


# --- El ciclo -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "label", "subject"),
    [
        (date(2026, 8, 31), "A", "Artes plásticas"),  # el lunes ancla
        (date(2026, 9, 1), "A", "Expresión corporal"),  # el martes que citó el usuario
        (date(2026, 9, 2), "A", "Deporte 1"),
        (date(2026, 9, 4), "A", "Deporte 3"),
        (date(2026, 9, 7), "B", "Deporte 2"),  # segunda semana del ciclo
        (date(2026, 9, 10), "B", "Natación"),
        (date(2026, 9, 11), "B", "Encuentro de expedición"),
        (date(2026, 9, 14), "A", "Artes plásticas"),  # el ciclo vuelve a empezar
        (date(2026, 12, 7), "A", "Artes plásticas"),  # 14 semanas después sigue cuadrando
    ],
)
def test_the_cycle_reproduces_the_photo(
    template: ScheduleTemplate, slots: list[ScheduleSlot], day: date, label: str, subject: str
) -> None:
    result = schedule.slot_for(day, template, slots, exceptions={})
    assert (result.week_label, result.subject) == (label, subject)
    assert result.has_class


def test_rotation_is_text_not_a_number(
    template: ScheduleTemplate, slots: list[ScheduleSlot]
) -> None:
    """La última franja se llama «Cultural»: la columna no puede ser un entero."""
    result = schedule.slot_for(date(2026, 9, 11), template, slots, exceptions={})
    assert result.rotation == "Cultural"


def test_weekends_have_no_class(template: ScheduleTemplate, slots: list[ScheduleSlot]) -> None:
    result = schedule.slot_for(date(2026, 9, 5), template, slots, exceptions={})
    assert not result.has_class
    assert result.skipped_reason == "fin de semana"


def test_week_index_survives_the_year_boundary(template: ScheduleTemplate) -> None:
    """Contar semanas desde el ancla no depende del número de semana ISO del año."""
    assert schedule.week_index(date(2026, 12, 28), template) == 1
    assert schedule.week_index(date(2027, 1, 4), template) == 0
    assert schedule.week_index(date(2027, 1, 11), template) == 1


def test_before_the_anchor_the_cycle_still_alternates() -> None:
    """El módulo en Python nunca es negativo, así que una fecha anterior no rompe."""
    template = make_template(valid_from=date(2026, 1, 1))
    assert schedule.week_index(date(2026, 8, 24), template) == 1
    assert schedule.week_index(date(2026, 8, 17), template) == 0
    assert schedule.week_index(date(2026, 8, 10), template) == 1


# --- Vigencia -------------------------------------------------------------------------------


def test_outside_the_school_year_there_is_no_schedule(slots: list[ScheduleSlot]) -> None:
    template = make_template(valid_from=date(2026, 8, 31), valid_to=date(2026, 12, 4))
    after = schedule.slot_for(date(2026, 12, 14), template, slots, exceptions={})
    before = schedule.slot_for(date(2026, 8, 24), template, slots, exceptions={})
    assert after.subject is None and "fuera del periodo" in (after.skipped_reason or "")
    assert before.subject is None and "fuera del periodo" in (before.skipped_reason or "")


def test_without_a_template_it_says_so(slots: list[ScheduleSlot]) -> None:
    result = schedule.slot_for(date(2026, 9, 2), None, slots, exceptions={})
    assert result.skipped_reason == "no hay horario cargado"


# --- Festivos: solo se cancela ese día ------------------------------------------------------


def test_a_holiday_cancels_only_that_day_and_does_not_shift_the_cycle(
    template: ScheduleTemplate, slots: list[ScheduleSlot]
) -> None:
    """Decisión del usuario: la semana sigue siendo la del calendario.

    El 12 de octubre de 2026 es lunes festivo (Día de la Raza). Esa semana es A, así que
    se pierde Artes plásticas, pero el martes 13 sigue siendo la rotación 2 y no la 1.
    """
    monday = schedule.slot_for(date(2026, 10, 12), template, slots, exceptions={})
    assert monday.subject is None
    assert monday.skipped_reason == "Día de la Raza"
    assert monday.week_label == "A"  # la etiqueta no se pierde aunque no haya clase

    tuesday = schedule.slot_for(date(2026, 10, 13), template, slots, exceptions={})
    assert (tuesday.week_label, tuesday.rotation, tuesday.subject) == (
        "A",
        "2",
        "Expresión corporal",
    )


def test_a_school_closure_from_the_admin_cancels_the_day(
    template: ScheduleTemplate, slots: list[ScheduleSlot]
) -> None:
    exceptions = {date(2026, 9, 10): (CalendarKind.SCHOOL_CLOSED.value, "Jornada pedagógica")}
    result = schedule.slot_for(date(2026, 9, 10), template, slots, exceptions=exceptions)
    assert result.subject is None
    assert result.skipped_reason == "Jornada pedagógica"


def test_class_day_overrides_a_national_holiday(
    template: ScheduleTemplate, slots: list[ScheduleSlot]
) -> None:
    """Si el colegio sí trabaja un festivo, la tabla manda sobre la librería."""
    exceptions = {date(2026, 10, 12): (CalendarKind.CLASS_DAY.value, "El colegio sí trabaja")}
    result = schedule.slot_for(date(2026, 10, 12), template, slots, exceptions=exceptions)
    assert result.subject == "Artes plásticas"


# --- «¿Cuándo hay natación?» ----------------------------------------------------------------


def test_next_occurrences_skips_non_school_days(
    template: ScheduleTemplate, slots: list[ScheduleSlot]
) -> None:
    exceptions = {date(2026, 9, 10): (CalendarKind.SCHOOL_CLOSED.value, "Jornada pedagógica")}
    found = schedule.next_occurrences(
        "natación", date(2026, 9, 2), template, slots, exceptions=exceptions, count=2
    )
    # El jueves 10 se cae por la jornada pedagógica: las siguientes son 24 de sep y 8 de oct.
    assert [r.day for r in found] == [date(2026, 9, 24), date(2026, 10, 8)]
    assert all(r.subject == "Natación" for r in found)


def test_next_occurrences_ignores_accents_and_case(
    template: ScheduleTemplate, slots: list[ScheduleSlot]
) -> None:
    """Quien pregunta escribe «natacion», no «Natación»."""
    found = schedule.next_occurrences(
        "NATACION", date(2026, 9, 2), template, slots, exceptions={}, count=1
    )
    assert [r.day for r in found] == [date(2026, 9, 10)]


def test_next_occurrences_of_something_that_does_not_exist(
    template: ScheduleTemplate, slots: list[ScheduleSlot]
) -> None:
    assert (
        schedule.next_occurrences("ajedrez", date(2026, 9, 2), template, slots, exceptions={}) == []
    )


# --- Semana completa ------------------------------------------------------------------------


def test_week_plan_covers_monday_to_friday(
    template: ScheduleTemplate, slots: list[ScheduleSlot]
) -> None:
    plan = schedule.week_plan(date(2026, 9, 7), template, slots, exceptions={})
    assert [r.subject for r in plan] == [
        "Deporte 2",
        "Motricidad y creatividad",
        "Tecnología",
        "Natación",
        "Encuentro de expedición",
    ]


def test_subjects_lists_each_one_once(slots: list[ScheduleSlot]) -> None:
    assert len(schedule.subjects(slots)) == 10
    assert "Natación" in schedule.subjects(slots)

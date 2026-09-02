"""Calendario escolar: festivos nacionales + excepciones del colegio.

Las fechas van fijas a propósito. Si una actualización de `holidays` cambiara de criterio,
tiene que romper aquí en `make check` y no un lunes por la mañana en producción.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.db.models import CalendarKind
from app.services import schoolcal

RECESO = {
    date(2026, 10, 5): (CalendarKind.SCHOOL_CLOSED.value, "Semana de receso"),
    date(2026, 10, 6): (CalendarKind.SCHOOL_CLOSED.value, "Semana de receso"),
    date(2026, 10, 7): (CalendarKind.SCHOOL_CLOSED.value, "Semana de receso"),
    date(2026, 10, 8): (CalendarKind.SCHOOL_CLOSED.value, "Semana de receso"),
    date(2026, 10, 9): (CalendarKind.SCHOOL_CLOSED.value, "Semana de receso"),
}


@pytest.mark.parametrize(
    ("day", "name"),
    [
        (date(2026, 1, 1), "Año Nuevo"),
        (date(2026, 5, 1), "Día del Trabajo"),
        (date(2026, 8, 7), "Batalla de Boyacá"),
        (date(2026, 12, 25), "Navidad"),
    ],
)
def test_fixed_national_holidays(day: date, name: str) -> None:
    assert schoolcal.holiday_name(day) == name


def test_ley_emiliani_moves_holidays_to_monday() -> None:
    """El 12 de octubre cae lunes en 2026, pero Reyes se corre del 6 al 12 de enero."""
    assert schoolcal.holiday_name(date(2026, 1, 6)) is None  # el día real no es festivo
    moved = schoolcal.holiday_name(date(2026, 1, 12))
    assert moved is not None and "Reyes" in moved
    assert date(2026, 1, 12).weekday() == 0  # se corrió al lunes


def test_a_normal_weekday_is_a_school_day() -> None:
    assert schoolcal.is_school_day(date(2026, 9, 2), exceptions={})


def test_weekends_are_not_school_days() -> None:
    info = schoolcal.day_info(date(2026, 9, 5), exceptions={})
    assert not info.is_school_day and info.reason == "fin de semana"


def test_a_national_holiday_is_not_a_school_day() -> None:
    info = schoolcal.day_info(date(2026, 8, 7), exceptions={})
    assert not info.is_school_day and info.reason == "Batalla de Boyacá"


def test_the_admin_table_can_close_the_school() -> None:
    info = schoolcal.day_info(date(2026, 10, 6), exceptions=RECESO)
    assert not info.is_school_day and info.reason == "Semana de receso"


def test_class_day_wins_over_a_national_holiday() -> None:
    """La excepción a la excepción: el colegio sí trabaja ese festivo."""
    exceptions = {date(2026, 8, 7): (CalendarKind.CLASS_DAY.value, "El colegio sí trabaja")}
    assert schoolcal.is_school_day(date(2026, 8, 7), exceptions=exceptions)


def test_school_days_skips_the_whole_recess_week() -> None:
    days = schoolcal.school_days(date(2026, 10, 5), date(2026, 10, 16), exceptions=RECESO)
    assert days == [
        date(2026, 10, 13),
        date(2026, 10, 14),
        date(2026, 10, 15),
        date(2026, 10, 16),
    ]
    # El lunes 12 es el Día de la Raza, así que tampoco aparece.
    assert date(2026, 10, 12) not in days


def test_next_school_day_jumps_the_long_weekend() -> None:
    # Viernes 6 de agosto de 2026 -> el 7 es festivo, así que toca el lunes 10.
    assert schoolcal.next_school_day(date(2026, 8, 7), exceptions={}) == date(2026, 8, 10)


def test_next_non_school_day_ignores_weekends() -> None:
    info = schoolcal.next_non_school_day(date(2026, 9, 2), exceptions={})
    assert info is not None
    assert info.day == date(2026, 10, 12) and info.reason == "Día de la Raza"


def test_an_unsupported_country_degrades_to_no_holidays() -> None:
    assert schoolcal.holiday_name(date(2026, 1, 1), country="ZZ") is None

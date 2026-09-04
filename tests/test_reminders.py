"""Cuándo suena un recordatorio. Todo puro: sin DB, sin LLM y sin la hora de la máquina."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.db.models import CalendarKind, RepeatKind
from app.services import reminders

BOG = ZoneInfo("America/Bogota")
MADRID = ZoneInfo("Europe/Madrid")

# 2026-09-07 es lunes; 09-12 sábado; 10-12 festivo en Colombia (Día de la Raza, Ley Emiliani).
MON = date(2026, 9, 7)
SEVEN = time(7, 0)


def at(day: date, moment: time = SEVEN, tz: ZoneInfo = BOG) -> datetime:
    return datetime.combine(day, moment, tzinfo=tz)


def when(
    *,
    repeat: str,
    after: datetime,
    weekdays: str = "",
    on_date: date | None = None,
    only_school_days: bool = False,
    moment: time = SEVEN,
    tz: ZoneInfo = BOG,
    exceptions: dict[date, tuple[str, str]] | None = None,
) -> datetime | None:
    return reminders.next_occurrence(
        repeat=repeat,
        weekdays=weekdays,
        time_of_day=moment,
        on_date=on_date,
        only_school_days=only_school_days,
        after=after,
        tz=tz,
        exceptions=exceptions or {},
        country="CO",
    )


# --- Una vez -------------------------------------------------------------------------------


def test_once_is_its_day_and_then_never_again() -> None:
    assert when(repeat=RepeatKind.ONCE, on_date=MON, after=at(MON, time(6, 0))) == at(MON)
    # Ya sonó: no hay siguiente, y ese None es lo que apaga la fila.
    assert when(repeat=RepeatKind.ONCE, on_date=MON, after=at(MON)) is None


def test_once_without_a_date_never_fires() -> None:
    assert when(repeat=RepeatKind.ONCE, on_date=None, after=at(MON, time(6, 0))) is None


# --- Diario --------------------------------------------------------------------------------


def test_daily_fires_today_when_the_hour_has_not_passed() -> None:
    assert when(repeat=RepeatKind.DAILY, after=at(MON, time(6, 0))) == at(MON)


def test_daily_rolls_over_to_tomorrow_once_the_hour_passed() -> None:
    assert when(repeat=RepeatKind.DAILY, after=at(MON, time(10, 0))) == at(MON + timedelta(days=1))


def test_the_result_is_strictly_after_so_a_fired_one_does_not_repeat() -> None:
    """Si devolviera la misma ocurrencia, el barrido la mandaría en bucle."""
    assert when(repeat=RepeatKind.DAILY, after=at(MON)) == at(MON + timedelta(days=1))


def test_daily_does_not_care_about_holidays_unless_asked() -> None:
    """«todos los días» es todos los días: el 12 de octubre es festivo y suena igual."""
    eve = at(date(2026, 10, 11), time(23, 0))
    assert when(repeat=RepeatKind.DAILY, after=eve) == at(date(2026, 10, 12))


# --- Días de la semana ---------------------------------------------------------------------


def test_weekly_picks_the_next_listed_day() -> None:
    # Lunes y miércoles; el lunes a las 10 ya pasó la hora, así que toca el miércoles.
    got = when(repeat=RepeatKind.WEEKLY, weekdays="13", after=at(MON, time(10, 0)))
    assert got == at(date(2026, 9, 9))


def test_weekly_wraps_around_to_next_week() -> None:
    got = when(repeat=RepeatKind.WEEKLY, weekdays="1", after=at(MON, time(10, 0)))
    assert got == at(MON + timedelta(days=7))


def test_weekly_without_days_never_fires() -> None:
    assert when(repeat=RepeatKind.WEEKLY, weekdays="", after=at(MON, time(6, 0))) is None


# --- Solo días de colegio ------------------------------------------------------------------


def test_school_days_skip_the_weekend() -> None:
    friday = date(2026, 9, 11)
    got = when(repeat=RepeatKind.DAILY, only_school_days=True, after=at(friday, time(10, 0)))
    assert got == at(date(2026, 9, 14))  # el lunes, no el sábado


def test_school_days_skip_a_national_holiday() -> None:
    eve = at(date(2026, 10, 11), time(23, 0))
    got = when(repeat=RepeatKind.DAILY, only_school_days=True, after=eve)
    assert got == at(date(2026, 10, 13))  # el 12 es festivo


def test_school_days_skip_a_closure_of_this_school() -> None:
    closed = {MON: (str(CalendarKind.SCHOOL_CLOSED), "Receso")}
    got = when(
        repeat=RepeatKind.DAILY,
        only_school_days=True,
        after=at(MON, time(6, 0)),
        exceptions=closed,
    )
    assert got == at(MON + timedelta(days=1))


def test_a_rule_that_can_never_happen_gives_up() -> None:
    """Solo domingos y solo días de colegio: no existe. Se rinde en vez de buscar sin fin."""
    got = when(
        repeat=RepeatKind.WEEKLY, weekdays="7", only_school_days=True, after=at(MON, time(6, 0))
    )
    assert got is None


# --- Horario de verano ---------------------------------------------------------------------


def test_a_local_hour_that_does_not_exist_moves_forward() -> None:
    """En Madrid el 29/3/2026 el reloj salta de 2:00 a 3:00: las 2:30 no existen ese día."""
    got = when(
        repeat=RepeatKind.DAILY,
        moment=time(2, 30),
        tz=MADRID,
        after=datetime(2026, 3, 28, 12, 0, tzinfo=MADRID),
    )
    assert got is not None
    assert (got.date(), got.hour, got.minute) == (date(2026, 3, 29), 3, 30)


def test_a_local_hour_that_happens_twice_takes_the_first() -> None:
    got = when(
        repeat=RepeatKind.DAILY,
        moment=time(2, 30),
        tz=MADRID,
        after=datetime(2026, 10, 24, 12, 0, tzinfo=MADRID),
    )
    assert got is not None
    assert got.utcoffset() == timedelta(hours=2)  # el primer paso, antes de atrasar


# --- Qué hacer con una ocurrencia vencida --------------------------------------------------


def test_within_the_grace_period_it_is_sent() -> None:
    fire = at(MON)
    assert reminders.due_action(RepeatKind.DAILY, fire, fire + timedelta(minutes=2)) == "send"


def test_a_repeating_one_that_is_hours_late_is_skipped() -> None:
    """El caso «el bot estuvo caído toda la mañana»: el de mañana es el bueno."""
    fire = at(MON)
    assert reminders.due_action(RepeatKind.DAILY, fire, fire + timedelta(hours=6)) == "skip"


def test_a_one_off_that_is_late_is_still_sent() -> None:
    """Esa promesa se hizo una sola vez: perderla es peor que llegar tarde."""
    fire = at(MON)
    assert reminders.due_action(RepeatKind.ONCE, fire, fire + timedelta(hours=6)) == "late"


# --- Días de la semana como texto ----------------------------------------------------------


def test_weekdays_round_trip_sorted_and_deduplicated() -> None:
    assert reminders.format_weekdays([3, 1, 1]) == "13"
    assert reminders.parse_weekdays("13") == [1, 3]
    assert reminders.format_weekdays([]) == ""


def test_draft_from_edit_reads_the_json_of_the_graph() -> None:
    draft = reminders.draft_from_edit(
        {
            "text": "disfraz",
            "time_of_day": "07:00",
            "repeat": "once",
            "weekdays": "",
            "on_date": "2026-09-10",
            "only_school_days": False,
        }
    )
    assert (draft.text, draft.time_of_day, draft.on_date) == ("disfraz", SEVEN, date(2026, 9, 10))

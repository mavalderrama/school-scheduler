"""Calendario escolar: qué días hay clase. Sin LLM y sin red.

Dos fuentes que se combinan por precedencia:

- Los festivos nacionales los calcula `holidays` en ejecución. Incluye la Ley Emiliani
  (los festivos que se corren al lunes siguiente), que es justo la parte que a mano se
  hace mal, y no hay que mantener una tabla año tras año.
- `calendar_exceptions` guarda lo que ninguna librería puede saber: la semana de receso,
  las jornadas pedagógicas, el día de la familia. Y `class_day` es la excepción a la
  excepción: un festivo nacional en el que este colegio sí tiene clase.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache

import holidays

from app.db.models import CalendarKind

WEEKEND = (5, 6)  # weekday(): 5 sábado, 6 domingo


@dataclass(frozen=True, slots=True)
class DayInfo:
    """Un día del calendario y por qué es (o no es) lectivo."""

    day: date
    is_school_day: bool
    reason: str | None = None  # motivo solo cuando NO hay clase


@lru_cache(maxsize=32)
def _holidays_for(country: str, year: int) -> dict[date, str]:
    """Festivos nacionales del año, en español. Cacheado: la librería no toca la red."""
    try:
        found = holidays.country_holidays(country, years=[year], language="es")
    except NotImplementedError:  # pragma: no cover - país no soportado
        return {}
    return {day: str(name) for day, name in found.items()}


def holiday_name(day: date, *, country: str = "CO") -> str | None:
    """Nombre del festivo nacional, o None si es un día corriente."""
    return _holidays_for(country, day.year).get(day)


def day_info(day: date, *, exceptions: dict[date, tuple[str, str]], country: str = "CO") -> DayInfo:
    """Precedencia: class_day > excepción del colegio > festivo nacional > fin de semana.

    `exceptions` mapea día -> (kind, label), tal como lo devuelve `repo.calendar_exceptions`.
    """
    override = exceptions.get(day)
    if override is not None:
        kind, label = override
        if kind == CalendarKind.CLASS_DAY:
            # Gana sobre todo, incluido un festivo nacional y el fin de semana.
            return DayInfo(day, True)
        return DayInfo(day, False, label)

    national = holiday_name(day, country=country)
    if national is not None:
        return DayInfo(day, False, national)

    if day.weekday() in WEEKEND:
        return DayInfo(day, False, "fin de semana")

    return DayInfo(day, True)


def is_school_day(
    day: date, *, exceptions: dict[date, tuple[str, str]], country: str = "CO"
) -> bool:
    return day_info(day, exceptions=exceptions, country=country).is_school_day


def school_days(
    date_from: date,
    date_to: date,
    *,
    exceptions: dict[date, tuple[str, str]],
    country: str = "CO",
) -> list[date]:
    """Días lectivos del rango, inclusive por los dos extremos."""
    return [
        day
        for day in _iter_days(date_from, date_to)
        if is_school_day(day, exceptions=exceptions, country=country)
    ]


def next_school_day(
    since: date, *, exceptions: dict[date, tuple[str, str]], country: str = "CO", limit: int = 60
) -> date | None:
    """Primer día lectivo desde `since` (incluido). None si no hay ninguno en `limit` días."""
    for offset in range(limit):
        day = since + timedelta(days=offset)
        if is_school_day(day, exceptions=exceptions, country=country):
            return day
    return None


def next_non_school_day(
    since: date, *, exceptions: dict[date, tuple[str, str]], country: str = "CO", limit: int = 120
) -> DayInfo | None:
    """Próximo día entre semana SIN clase, para avisarlo en /estado."""
    for offset in range(limit):
        day = since + timedelta(days=offset)
        if day.weekday() in WEEKEND:
            continue
        info = day_info(day, exceptions=exceptions, country=country)
        if not info.is_school_day:
            return info
    return None


def _iter_days(date_from: date, date_to: date) -> Iterable[date]:
    day = date_from
    while day <= date_to:
        yield day
        day += timedelta(days=1)

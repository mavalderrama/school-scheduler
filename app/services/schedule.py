"""Horario rotativo A/B: qué materia toca cada día. Determinista, sin LLM.

La aritmética es toda la fase: la semana del ciclo sale de contar semanas completas desde
un lunes ancla. Un festivo **no desplaza el ciclo**: la semana sigue siendo la que le toca por
calendario y esa rotación simplemente no se dicta esa vuelta.

Comprobado contra el horario K4A: ancla lunes 2026-08-31, ciclo de 2 semanas; el miércoles
2026-09-02 es Semana A (Deporte 1) y el jueves 2026-09-10 es Semana B (Natación).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from app.db import repo
from app.db.models import ScheduleSlot, ScheduleTemplate
from app.services import schoolcal
from app.services.scope import Scope

MAX_HORIZON_DAYS = 400


@dataclass(frozen=True, slots=True)
class SlotResult:
    """Qué toca un día concreto. `subject` es None si no hay clase o no hay horario."""

    day: date
    week_label: str | None = None
    rotation: str | None = None
    subject: str | None = None
    skipped_reason: str | None = None
    schedule_name: str | None = None

    @property
    def has_class(self) -> bool:
        return self.subject is not None


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_index(day: date, template: ScheduleTemplate) -> int:
    """Índice de la semana dentro del ciclo (0 = la del lunes ancla)."""
    weeks = (monday_of(day) - monday_of(template.anchor_monday)).days // 7
    return weeks % template.cycle_weeks


def covers(day: date, template: ScheduleTemplate) -> bool:
    """¿El horario está vigente ese día? Fuera del año escolar no se extrapola."""
    if day < template.valid_from:
        return False
    return template.valid_to is None or day <= template.valid_to


def slot_for(
    day: date,
    template: ScheduleTemplate | None,
    slots: Sequence[ScheduleSlot],
    *,
    exceptions: dict[date, tuple[str, str]],
    country: str = "CO",
) -> SlotResult:
    """La franja de un día, o el motivo por el que no hay ninguna."""
    if template is None:
        return SlotResult(day, skipped_reason="no hay horario cargado")
    name = template.name
    if not covers(day, template):
        return SlotResult(day, skipped_reason="fuera del periodo del horario", schedule_name=name)

    info = schoolcal.day_info(day, exceptions=exceptions, country=country)
    index = week_index(day, template)
    # Un horario que se repite igual cada semana no tiene «Semana A»: sin etiqueta, para
    # que ninguna vista acabe enseñando el vocabulario A/B de otro horario distinto.
    label = _label_for(index, slots) if template.cycle_weeks > 1 else None

    if not info.is_school_day:
        # La etiqueta de la semana no se toca: solo se pierde la franja de ese día.
        return SlotResult(day, week_label=label, skipped_reason=info.reason, schedule_name=name)

    match = next(
        (s for s in slots if s.week_index == index and s.weekday == day.isoweekday()), None
    )
    if match is None:
        return SlotResult(
            day, week_label=label, skipped_reason="sin franja para ese día", schedule_name=name
        )
    return SlotResult(
        day,
        week_label=match.week_label if template.cycle_weeks > 1 else None,
        rotation=match.rotation,
        subject=match.subject,
        schedule_name=name,
    )


def week_plan(
    monday: date,
    template: ScheduleTemplate | None,
    slots: Sequence[ScheduleSlot],
    *,
    exceptions: dict[date, tuple[str, str]],
    country: str = "CO",
    days: int = 5,
) -> list[SlotResult]:
    """Los días hábiles de la semana que empieza en `monday`, con festivos marcados."""
    return [
        slot_for(
            monday + timedelta(days=offset),
            template,
            slots,
            exceptions=exceptions,
            country=country,
        )
        for offset in range(days)
    ]


def next_occurrences(
    subject: str,
    since: date,
    template: ScheduleTemplate | None,
    slots: Sequence[ScheduleSlot],
    *,
    exceptions: dict[date, tuple[str, str]],
    country: str = "CO",
    count: int = 3,
    horizon_days: int = 120,
) -> list[SlotResult]:
    """Próximas veces que toca una materia, saltando los días sin clase.

    La comparación es laxa a propósito (sin tildes ni mayúsculas, por subcadena): quien
    pregunta escribe «natacion», no «Natación».
    """
    needle = _normalize(subject)
    if not needle or template is None:
        return []

    found: list[SlotResult] = []
    for offset in range(min(horizon_days, MAX_HORIZON_DAYS)):
        day = since + timedelta(days=offset)
        result = slot_for(day, template, slots, exceptions=exceptions, country=country)
        if result.subject and needle in _normalize(result.subject):
            found.append(result)
            if len(found) == count:
                break
    return found


def subjects(slots: Sequence[ScheduleSlot]) -> list[str]:
    """Materias distintas del horario, en el orden en que aparecen."""
    seen: dict[str, None] = {}
    for slot in slots:
        seen.setdefault(slot.subject, None)
    return list(seen)


def _label_for(index: int, slots: Sequence[ScheduleSlot]) -> str | None:
    return next((s.week_label for s in slots if s.week_index == index), None)


def same_subject(one: str, other: str) -> bool:
    """¿Son el mismo nombre? Sin tildes ni mayúsculas: «Natación» y «natacion» lo son."""
    return _normalize(one) == _normalize(other)


def _normalize(text: str) -> str:
    """Minúsculas y sin tildes, para comparar «Natación» con «natacion»."""
    table = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")
    return " ".join(text.translate(table).lower().split())


# --- Carga desde la DB ----------------------------------------------------------------------
#
# Estas envolturas leen la plantilla vigente y las excepciones del calendario para que
# notify, chat y los comandos no repitan la misma consulta. Todo lo de arriba sigue siendo
# puro y testeable sin base de datos.


@dataclass(frozen=True, slots=True)
class LoadedSchedule:
    template: ScheduleTemplate
    slots: list[ScheduleSlot]
    exceptions: dict[date, tuple[str, str]]


async def load_all(scope: Scope, day: date | None = None) -> list[LoadedSchedule]:
    """Todas las plantillas vigentes del niño con sus franjas. Una consulta de franjas."""
    templates = await repo.active_schedules(scope.child_id, day)
    if not templates:
        return []
    slots = await repo.slots_for_schedules([t.pk for t in templates])
    exceptions = await repo.calendar_exceptions(scope.school_id)
    return [
        LoadedSchedule(template=t, slots=slots.get(t.pk, []), exceptions=exceptions)
        for t in templates
    ]


async def load(scope: Scope, day: date | None = None) -> LoadedSchedule | None:
    """La primera plantilla vigente. Para cuando solo hace falta saber si hay alguna."""
    loaded = await load_all(scope, day)
    return loaded[0] if loaded else None


async def resolve_day(scope: Scope, day: date) -> list[SlotResult]:
    """Qué toca ese día, **una entrada por horario vigente**.

    Lista vacía = no hay ningún horario cargado, que no es lo mismo que no haber clase.
    Si el día entero no es lectivo se devuelve una sola entrada con el motivo: es una
    propiedad del calendario, no de cada horario, y repetirla por horario sobra.
    """
    loaded = await load_all(scope, day)
    if not loaded:
        return []

    info = schoolcal.day_info(day, exceptions=loaded[0].exceptions, country=scope.country)
    if not info.is_school_day:
        return [SlotResult(day, skipped_reason=info.reason)]

    results = [
        slot_for(day, item.template, item.slots, exceptions=item.exceptions, country=scope.country)
        for item in loaded
    ]
    return [r for r in results if r.subject is not None]


async def resolve(scope: Scope, day: date) -> SlotResult | None:
    """La primera franja del día. Se conserva para quien solo necesita una."""
    results = await resolve_day(scope, day)
    return results[0] if results else None


async def resolve_week(scope: Scope, monday: date) -> list[list[SlotResult]]:
    """Los cinco días hábiles; cada uno con lo que dice cada horario vigente."""
    return [await resolve_day(scope, monday + timedelta(days=offset)) for offset in range(5)]


async def find_subject(
    scope: Scope, subject: str, since: date, *, count: int = 3
) -> list[SlotResult]:
    """Próximas veces que toca una materia, mirando en todos los horarios vigentes."""
    loaded = await load_all(scope, since)
    if not loaded:
        return []
    found: list[SlotResult] = []
    for item in loaded:
        found.extend(
            next_occurrences(
                subject,
                since,
                item.template,
                item.slots,
                exceptions=item.exceptions,
                country=scope.country,
                count=count,
            )
        )
    found.sort(key=lambda r: (r.day, r.schedule_name or ""))
    return found[:count]

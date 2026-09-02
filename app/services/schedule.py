"""Horario rotativo A/B: qué materia toca cada día. Determinista, sin LLM.

La aritmética es toda la fase: la semana del ciclo sale de contar semanas completas desde
un lunes ancla. Con la política `skip_day` (la de este colegio) un festivo **no desplaza el
ciclo**: la semana sigue siendo la que le toca por calendario y esa rotación simplemente no
se dicta esa vuelta.

Comprobado contra el horario K4A: ancla lunes 2026-08-31, ciclo de 2 semanas; el miércoles
2026-09-02 es Semana A (Deporte 1) y el jueves 2026-09-10 es Semana B (Natación).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from app.db import repo
from app.db.models import HolidayPolicy, ScheduleSlot, ScheduleTemplate
from app.services import schoolcal

MAX_HORIZON_DAYS = 400


@dataclass(frozen=True, slots=True)
class SlotResult:
    """Qué toca un día concreto. `subject` es None si no hay clase o no hay horario."""

    day: date
    week_label: str | None = None
    rotation: str | None = None
    subject: str | None = None
    skipped_reason: str | None = None

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
    if not covers(day, template):
        return SlotResult(day, skipped_reason="fuera del periodo del horario")

    info = schoolcal.day_info(day, exceptions=exceptions, country=country)
    index = week_index(day, template)
    label = _label_for(index, slots)

    if not info.is_school_day:
        # `skip_day`: la etiqueta de la semana no se toca, solo se pierde esa rotación.
        return SlotResult(day, week_label=label, skipped_reason=info.reason)

    if template.holiday_policy == HolidayPolicy.SHIFT:  # pragma: no cover - no usado aún
        raise NotImplementedError("la política 'shift' todavía no está implementada")

    match = next(
        (s for s in slots if s.week_index == index and s.weekday == day.isoweekday()), None
    )
    if match is None:
        return SlotResult(day, week_label=label, skipped_reason="sin franja para ese día")
    return SlotResult(
        day,
        week_label=match.week_label,
        rotation=match.rotation,
        subject=match.subject,
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


async def load(day: date | None = None) -> LoadedSchedule | None:
    """Plantilla vigente + franjas + excepciones, o None si no hay horario cargado."""
    template = await repo.active_schedule(day)
    if template is None:
        return None
    return LoadedSchedule(
        template=template,
        slots=await repo.schedule_slots(template.pk),
        exceptions=await repo.calendar_exceptions(),
    )


async def resolve(day: date, *, country: str = "CO") -> SlotResult | None:
    """Qué toca ese día. **None** significa que no hay horario cargado, que no es lo mismo
    que que no haya clase: quien llama decide si vale la pena decir algo."""
    loaded = await load(day)
    if loaded is None:
        return None
    return slot_for(
        day, loaded.template, loaded.slots, exceptions=loaded.exceptions, country=country
    )


async def resolve_week(monday: date, *, country: str = "CO") -> list[SlotResult]:
    loaded = await load(monday)
    if loaded is None:
        return []
    return week_plan(
        monday, loaded.template, loaded.slots, exceptions=loaded.exceptions, country=country
    )


async def find_subject(
    subject: str, since: date, *, country: str = "CO", count: int = 3
) -> list[SlotResult]:
    loaded = await load(since)
    if loaded is None:
        return []
    return next_occurrences(
        subject,
        since,
        loaded.template,
        loaded.slots,
        exceptions=loaded.exceptions,
        country=country,
        count=count,
    )

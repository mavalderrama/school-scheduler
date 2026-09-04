"""Lógica de negocio de la agenda: confirmar o rechazar una source.

Una foto confirmada se aplica de una de dos formas según su `doc_type`: entradas por
fecha con merge del día (`agenda`) o una plantilla de horario rotativo que reemplaza a la
anterior (`schedule`). Las dos versionan en vez de borrar.

Por texto hay tres altas, y la diferencia entre ellas es qué es cada cosa: `add_entry` es
un día concreto, `add_recurring` es una regla semanal (y por eso acaba en `schedules`, no
en N entradas con fecha) y el recordatorio, que vive en `reminders`, lo maneja el grafo.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from app.db import repo
from app.db.models import AgendaEntry, SourceKind, SourceStatus
from app.llm.schemas import ExtractedEntry, ExtractionResult, ScheduleDraft, SlotDraft
from app.log import get_logger
from app.services import cache, reminders
from app.services import schedule as schedule_service
from app.services.scope import Scope

log = get_logger(__name__)


@dataclass(frozen=True)
class ApplyResult:
    source_id: int
    dates: list[date]
    inserted: int
    superseded: int
    schedule_id: int | None = None
    slots: int = 0


async def apply_source(
    source_id: int,
    extraction: ExtractionResult,
    *,
    today: date | None = None,
    replace_ids: Sequence[int] = (),
) -> ApplyResult:
    """Confirma la extracción.

    Un horario no se mezcla por fecha, pero tampoco reemplaza a los demás por su cuenta:
    solo desactiva los que se le indiquen en `replace_ids`, que decide el usuario con
    los botones.
    """
    if extraction.doc_type == "schedule" and extraction.schedule is not None:
        return await _apply_schedule(source_id, extraction, today or date.today(), replace_ids)
    inserted, superseded = await repo.apply_source_entries(source_id, extraction.entries)
    dates = sorted({entry.entry_date for entry in extraction.entries})
    log.info(
        "source_applied",
        source_id=source_id,
        dates=[d.isoformat() for d in dates],
        inserted=inserted,
        superseded=superseded,
    )
    return ApplyResult(source_id=source_id, dates=dates, inserted=inserted, superseded=superseded)


async def _apply_schedule(
    source_id: int,
    extraction: ExtractionResult,
    today: date,
    replace_ids: Sequence[int] = (),
) -> ApplyResult:
    """Guarda el horario. `valid_from` es hoy o el ancla, lo que sea más tarde.

    Se usa el ancla cuando es futura (un horario que aún no empieza) y hoy cuando el ciclo
    ya venía corriendo: así el horario nuevo no reescribe retroactivamente el pasado.
    """
    draft = extraction.schedule
    if draft is None or draft.anchor_monday is None:
        # No debería llegar aquí: el interrogatorio no deja confirmar sin ancla.
        raise ValueError("el horario no tiene lunes ancla")
    valid_from = max(today, draft.anchor_monday)
    schedule_id = await repo.apply_schedule(
        source_id, draft, valid_from=valid_from, replace_ids=replace_ids
    )
    slots = len({(s.week_label, s.weekday) for s in draft.slots})
    log.info(
        "schedule_applied",
        source_id=source_id,
        schedule_id=schedule_id,
        slots=slots,
        anchor=draft.anchor_monday.isoformat(),
        replaced=list(replace_ids),
    )
    return ApplyResult(
        source_id=source_id,
        dates=[],
        inserted=slots,
        superseded=0,
        schedule_id=schedule_id,
        slots=slots,
    )


async def add_entry(
    scope: Scope, entry_date: date, kind: str, text: str, user_id: int | None = None
) -> AgendaEntry:
    """Alta por texto: source `text_correction` + UNA entrada, sin tocar el resto del día."""
    user = await repo.get_user(user_id) if user_id is not None else None
    source = await repo.create_source(
        SourceKind.TEXT_CORRECTION, child_id=scope.child_id, submitted_by=user
    )
    entry = await repo.add_single_entry(
        source.pk,
        ExtractedEntry(entry_date=entry_date, kind=kind, text=text, confidence="high"),
    )
    log.info("entry_added", entry_id=entry.pk, source_id=source.pk, date=entry_date.isoformat())
    return entry


@dataclass(frozen=True)
class RecurringResult:
    """Lo que hizo un alta recurrente: qué horario quedó y si sustituyó a otro igual."""

    schedule_id: int
    weekdays: str
    text: str
    replaced: bool


async def add_recurring(
    scope: Scope,
    weekdays: str,
    text: str,
    *,
    today: date,
    user_id: int | None = None,
) -> RecurringResult:
    """Alta de algo que se repite cada semana: «todos los viernes hay natación».

    No es una entrada con fecha (no hay una) ni un recordatorio (no hay hora): es una
    **regla**, y una regla ya tiene dónde vivir en este proyecto: un horario de ciclo
    semanal, que convive con el académico igual que el de la jornada extendida. Así sale en
    /hoy, /manana y la notificación diaria, respeta festivos y no genera N filas que
    caduquen al agotarse un horizonte inventado.

    Repetir el mismo nombre **reemplaza** al anterior en vez de duplicar la línea del día;
    lo reemplazado queda versionado, como todo lo demás.
    """
    user = await repo.get_user(user_id) if user_id is not None else None
    name = text[:1].upper() + text[1:]
    same = [
        t.pk
        for t in await repo.active_schedules(scope.child_id, today)
        if schedule_service.same_subject(t.name, name)
    ]
    source = await repo.create_source(
        SourceKind.TEXT_CORRECTION, child_id=scope.child_id, submitted_by=user
    )
    draft = ScheduleDraft(
        name=name,
        cycle_weeks=1,
        anchor_monday=schedule_service.monday_of(today),
        slots=[
            SlotDraft(week_label="A", weekday=day, subject=text)
            for day in reminders.parse_weekdays(weekdays)
        ],
    )
    schedule_id = await repo.apply_schedule(source.pk, draft, valid_from=today, replace_ids=same)
    log.info(
        "recurring_added",
        schedule_id=schedule_id,
        source_id=source.pk,
        weekdays=weekdays,
        replaced=same,
    )
    return RecurringResult(
        schedule_id=schedule_id, weekdays=weekdays, text=text, replaced=bool(same)
    )


async def remove_entry(scope: Scope, entry_id: int, user_id: int | None = None) -> bool:
    """Baja por texto: desactiva solo esa entrada, con `superseded_by` a la nueva source."""
    user = await repo.get_user(user_id) if user_id is not None else None
    source = await repo.create_source(
        SourceKind.TEXT_CORRECTION, child_id=scope.child_id, submitted_by=user
    )
    removed = await repo.deactivate_entry(entry_id, source.pk, child_id=scope.child_id)
    log.info("entry_removed", entry_id=entry_id, source_id=source.pk, removed=removed)
    return removed


async def reject_source(source_id: int) -> None:
    """Descartar = no tocar nada; solo marca la source como rechazada.

    Además invalida la entrada de caché de esa foto: quien descarta suele hacerlo porque
    la lectura estaba mal, así que reenviarla debe volver a leerla con el LLM.
    """
    source = await repo.get_source(source_id)
    await repo.set_source_status(source_id, SourceStatus.REJECTED)
    if source is not None:
        await cache.invalidate(source.llm_cache_key)
    log.info("source_rejected", source_id=source_id)

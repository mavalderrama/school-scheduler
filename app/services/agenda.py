"""Lógica de negocio de la agenda: confirmar o rechazar una source (merge por fecha)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.db import repo
from app.db.models import SourceStatus
from app.llm.schemas import ExtractionResult
from app.log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ApplyResult:
    source_id: int
    dates: list[date]
    inserted: int
    superseded: int


async def apply_source(source_id: int, extraction: ExtractionResult) -> ApplyResult:
    """Confirma la extracción: reemplaza lo vigente en las fechas cubiertas e inserta lo nuevo."""
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


async def reject_source(source_id: int) -> None:
    """Descartar = no tocar nada; solo marca la source como rechazada."""
    await repo.set_source_status(source_id, SourceStatus.REJECTED)
    log.info("source_rejected", source_id=source_id)

"""Lógica de negocio de la agenda: confirmar o rechazar una source (merge por fecha)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.db import repo
from app.db.models import AgendaEntry, SourceKind, SourceStatus
from app.llm.schemas import ExtractedEntry, ExtractionResult
from app.log import get_logger
from app.services import cache

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


async def add_entry(
    entry_date: date, kind: str, text: str, user_id: int | None = None
) -> AgendaEntry:
    """Alta por texto: source `text_correction` + UNA entrada, sin tocar el resto del día."""
    user = await repo.get_user(user_id) if user_id is not None else None
    source = await repo.create_source(SourceKind.TEXT_CORRECTION, submitted_by=user)
    entry = await repo.add_single_entry(
        source.pk,
        ExtractedEntry(entry_date=entry_date, kind=kind, text=text, confidence="high"),
    )
    log.info("entry_added", entry_id=entry.pk, source_id=source.pk, date=entry_date.isoformat())
    return entry


async def remove_entry(entry_id: int, user_id: int | None = None) -> bool:
    """Baja por texto: desactiva solo esa entrada, con `superseded_by` a la nueva source."""
    user = await repo.get_user(user_id) if user_id is not None else None
    source = await repo.create_source(SourceKind.TEXT_CORRECTION, submitted_by=user)
    removed = await repo.deactivate_entry(entry_id, source.pk)
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

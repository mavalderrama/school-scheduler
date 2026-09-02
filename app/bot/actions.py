"""Acciones compartidas entre los botones y el texto libre.

"Sí" escrito y ✅ pulsado tienen que hacer exactamente lo mismo, así que la lógica vive
aquí y tanto `handlers/callbacks.py` como `handlers/text.py` la llaman.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from aiogram import Bot

from app.bot.handlers.photo import start_ingest
from app.bot.present import present_extraction
from app.config import Settings
from app.db import repo
from app.db.models import Source, SourceStatus
from app.llm import compose
from app.llm.provider import LLMError, LLMProviders, LLMQuotaError
from app.log import get_logger
from app.services import agenda, ingest
from app.services.confirm import PendingEdit, PendingPhoto, PendingStore

log = get_logger(__name__)


async def continue_queue(
    bot: Bot,
    chat_id: int,
    settings: Settings,
    providers: LLMProviders,
    pending: PendingStore,
) -> None:
    """Cierra la confirmación y, si hay fotos en cola, procesa la siguiente."""
    next_photo = pending.clear(chat_id)
    if next_photo is not None:
        await bot.send_message(chat_id, "Sigo con la siguiente foto de la cola.")
        await start_ingest(bot, chat_id, next_photo, settings, providers, pending)


async def confirm_photo(
    current: PendingPhoto, settings: Settings, *, replace_ids: Sequence[int] = ()
) -> str:
    today = datetime.now(settings.zoneinfo).date()
    # El nombre del reemplazado hay que leerlo antes: después queda desactivado.
    replaced = None
    if replace_ids:
        old = next((t for t in await repo.active_schedules() if t.pk in set(replace_ids)), None)
        replaced = old.name if old is not None else None

    result = await agenda.apply_source(
        current.source_id, current.extraction, today=today, replace_ids=replace_ids
    )
    draft = current.extraction.schedule
    if result.schedule_id is not None and draft is not None and draft.anchor_monday is not None:
        return compose.format_schedule_applied_multi(
            draft.name or "Horario", result.slots, draft.anchor_monday, replaced
        )
    return compose.format_applied(result.dates, result.inserted, result.superseded)


async def reject_photo(current: PendingPhoto) -> str:
    await agenda.reject_source(current.source_id)
    return "❌ Descartado. No guardé nada de esta foto."


async def apply_edit(edit: PendingEdit, user_id: int | None) -> str:
    """Ejecuta un alta o una baja ya confirmada."""
    if edit.action == "add":
        entry = await agenda.add_entry(
            edit.entry_date, edit.kind or "note", edit.text or "", user_id
        )
        return compose.format_added(entry)

    if edit.entry_id is None:
        return "No sé cuál quitar. Vuelve a pedírmelo, por favor."
    return await remove_chosen(edit.entry_id, user_id)


async def remove_chosen(entry_id: int, user_id: int | None) -> str:
    """Baja de una candidata elegida con los botones."""
    entry = await repo.get_entry(entry_id)
    if entry is None or not entry.is_active:
        return "Esa entrada ya no está vigente."
    await agenda.remove_entry(entry.pk, user_id)
    return compose.format_removed(entry)


async def resume_photo(
    bot: Bot,
    source: Source,
    settings: Settings,
    providers: LLMProviders,
    pending: PendingStore,
) -> bool:
    """Reintenta una foto que quedó sin leer (cuota agotada) y pide confirmación.

    Devuelve False sin ruido si todavía no se puede: el job lo volverá a intentar.
    """
    if source.chat_id is None or source.local_path is None:
        return False
    if pending.get(source.chat_id) is not None:
        return False  # el chat está ocupado con otra confirmación
    image_path = Path(source.local_path)
    if not await asyncio.to_thread(image_path.is_file):
        log.warning("retry_photo_missing_file", source_id=source.pk, path=source.local_path)
        await repo.update_source(source.pk, status=SourceStatus.FAILED)
        return False

    try:
        extraction, _ = await ingest.extract_photo(
            source.pk, image_path, settings, providers, source.caption
        )
    except LLMQuotaError:
        return False  # sigue sin cuota; se reintenta en la próxima pasada
    except LLMError as exc:
        log.warning("retry_photo_failed", source_id=source.pk, error=str(exc))
        return False

    await bot.send_message(source.chat_id, "⏳ Ya pude leer la foto que quedó pendiente:")
    await present_extraction(bot, source.chat_id, source.pk, extraction, pending)
    log.info("retry_photo_ok", source_id=source.pk, chat_id=source.chat_id)
    return True

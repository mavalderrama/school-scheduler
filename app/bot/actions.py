"""Acciones compartidas entre los botones y el texto libre.

"Sí" escrito y ✅ pulsado tienen que hacer exactamente lo mismo, así que la lógica vive
aquí y tanto `handlers/callbacks.py` como `handlers/text.py` la llaman.
"""

from __future__ import annotations

from aiogram import Bot

from app.bot.handlers.photo import start_ingest
from app.config import Settings
from app.db import repo
from app.llm import compose
from app.llm.provider import LLMProviders
from app.log import get_logger
from app.services import agenda
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


async def confirm_photo(current: PendingPhoto) -> str:
    result = await agenda.apply_source(current.source_id, current.extraction)
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

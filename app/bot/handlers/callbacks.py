"""Botones ✅ Confirmar / ✏️ Corregir / ❌ Descartar."""

from __future__ import annotations

from aiogram import Bot, Router
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.photo import start_ingest
from app.bot.keyboards import SourceCallback
from app.config import Settings
from app.llm.compose import format_applied, format_extraction
from app.llm.provider import LLMProviders
from app.log import get_logger
from app.services import agenda
from app.services.confirm import PendingStore

log = get_logger(__name__)
router = Router(name="callbacks")


async def _finish(
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


@router.callback_query(SourceCallback.filter())
async def on_source_action(
    query: CallbackQuery,
    callback_data: SourceCallback,
    bot: Bot,
    settings: Settings,
    providers: LLMProviders,
    pending: PendingStore,
) -> None:
    message = query.message
    if not isinstance(message, Message):
        await query.answer("Este mensaje ya no está disponible.")
        return
    chat_id = message.chat.id
    current = pending.get(chat_id)
    if current is None or current.source_id != callback_data.source_id:
        await query.answer("Esta lectura ya no está activa.")
        await message.edit_reply_markup(reply_markup=None)
        return

    if callback_data.action == "confirm":
        result = await agenda.apply_source(current.source_id, current.extraction)
        await query.answer("Guardado")
        await message.edit_text(
            format_extraction(current.extraction).rsplit("\n", 1)[0]
            + "\n\n"
            + format_applied(result.dates, result.inserted, result.superseded)
        )
        await _finish(bot, chat_id, settings, providers, pending)
        return

    if callback_data.action == "reject":
        await agenda.reject_source(current.source_id)
        await query.answer("Descartado")
        await message.edit_text("❌ Descartado. No guardé nada de esta foto.")
        await _finish(bot, chat_id, settings, providers, pending)
        return

    # correct
    current.awaiting_correction = True
    await query.answer()
    await message.edit_reply_markup(reply_markup=None)
    await bot.send_message(
        chat_id,
        "✏️ Dime qué corrijo en un mensaje (por ejemplo: «el disfraz es el jueves, no el "
        "martes» o «quita la tarea de matemáticas»).",
    )

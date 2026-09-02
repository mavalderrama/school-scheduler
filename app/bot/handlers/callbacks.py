"""Botones: ✅/✏️/❌ de una foto, ✅/❌ de un alta o baja, y elección de candidata."""

from __future__ import annotations

from aiogram import Bot, Router
from aiogram.types import CallbackQuery, Message

from app.bot import actions
from app.bot.keyboards import CandidateCallback, EditCallback, SourceCallback
from app.config import Settings
from app.llm import compose
from app.llm.provider import LLMProviders
from app.log import get_logger
from app.services.confirm import PendingEdit, PendingPhoto, PendingStore

log = get_logger(__name__)
router = Router(name="callbacks")


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
    if not isinstance(current, PendingPhoto) or current.source_id != callback_data.source_id:
        await query.answer("Esta lectura ya no está activa.")
        await message.edit_reply_markup(reply_markup=None)
        return

    if callback_data.action == "confirm":
        summary = await actions.confirm_photo(current)
        await query.answer("Guardado")
        await message.edit_text(
            compose.format_extraction(current.extraction).rsplit("\n", 1)[0] + "\n\n" + summary
        )
        await actions.continue_queue(bot, chat_id, settings, providers, pending)
        return

    if callback_data.action == "reject":
        await query.answer("Descartado")
        await message.edit_text(await actions.reject_photo(current))
        await actions.continue_queue(bot, chat_id, settings, providers, pending)
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


@router.callback_query(EditCallback.filter())
async def on_edit_action(
    query: CallbackQuery,
    callback_data: EditCallback,
    pending: PendingStore,
) -> None:
    """✅ / ❌ de un alta o una baja pedida por texto."""
    message = query.message
    if not isinstance(message, Message):
        await query.answer("Este mensaje ya no está disponible.")
        return
    chat_id = message.chat.id
    current = pending.get(chat_id)
    if not isinstance(current, PendingEdit) or current.edit_id != callback_data.edit_id:
        await query.answer("Esto ya no está activo.")
        await message.edit_reply_markup(reply_markup=None)
        return

    user_id = query.from_user.id
    if callback_data.action == "reject":
        pending.clear(chat_id)
        await query.answer("Cancelado")
        await message.edit_text("❌ Listo, no cambio nada.")
        return

    result = await actions.apply_edit(current, user_id)
    pending.clear(chat_id)
    await query.answer("Hecho")
    await message.edit_text(result)


@router.callback_query(CandidateCallback.filter())
async def on_candidate_chosen(
    query: CallbackQuery,
    callback_data: CandidateCallback,
    pending: PendingStore,
) -> None:
    """Elección entre varias entradas candidatas a borrar."""
    message = query.message
    if not isinstance(message, Message):
        await query.answer("Este mensaje ya no está disponible.")
        return
    chat_id = message.chat.id
    current = pending.get(chat_id)
    if not isinstance(current, PendingEdit) or current.edit_id != callback_data.edit_id:
        await query.answer("Esto ya no está activo.")
        await message.edit_reply_markup(reply_markup=None)
        return

    result = await actions.remove_chosen(callback_data.entry_id, query.from_user.id)
    pending.clear(chat_id)
    await query.answer("Hecho")
    await message.edit_text(result)

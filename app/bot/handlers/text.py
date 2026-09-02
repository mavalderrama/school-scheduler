"""Texto libre: corrección de lo pendiente, o intención clasificada por el LLM (flujo 7.2)."""

from __future__ import annotations

from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.types import Message

from app.bot import actions
from app.bot.keyboards import candidates_keyboard, confirmation_keyboard, edit_keyboard
from app.config import Settings
from app.db import repo
from app.llm import compose
from app.llm.provider import LLMError, LLMProviders
from app.log import get_logger
from app.services import chat, ingest
from app.services.confirm import PendingEdit, PendingPhoto, PendingStore

log = get_logger(__name__)
router = Router(name="text")


async def _reply(message: Message, chat_id: int, text: str) -> Message:
    """Responde y guarda la respuesta en el historial corto."""
    await repo.save_message(chat_id, None, "assistant", text)
    return await message.answer(text)


async def _apply_correction(
    message: Message,
    bot: Bot,
    current: PendingPhoto,
    text: str,
    settings: Settings,
    providers: LLMProviders,
) -> None:
    """Rehace la extracción pendiente con la corrección del usuario y vuelve a preguntar."""
    status = await message.reply("✏️ Aplicando la corrección...")
    try:
        corrected = await ingest.correct_extraction(
            current.source_id, current.extraction, text, settings, providers
        )
    except LLMError as exc:
        log.warning("correction_failed", source_id=current.source_id, error=str(exc))
        await status.edit_text(
            "⚠️ No pude aplicar la corrección ahora (el proveedor de IA no respondió). "
            "Inténtalo otra vez o usa ❌ para descartar."
        )
        current.awaiting_correction = False
        await bot.send_message(
            message.chat.id,
            compose.format_extraction(current.extraction),
            reply_markup=confirmation_keyboard(current.source_id),
        )
        return
    current.extraction = corrected
    current.awaiting_correction = False
    summary = await status.edit_text(
        compose.format_extraction(corrected),
        reply_markup=confirmation_keyboard(current.source_id),
    )
    current.summary_message_id = summary.message_id if isinstance(summary, Message) else None


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(
    message: Message,
    bot: Bot,
    settings: Settings,
    providers: LLMProviders,
    pending: PendingStore,
) -> None:
    chat_id = message.chat.id
    text = (message.text or "").strip()
    if not text:
        return
    user_id = message.from_user.id if message.from_user else None
    current = pending.get(chat_id)
    photo_pending = current if isinstance(current, PendingPhoto) else None

    # Tras ✏️ el siguiente mensaje es la corrección, sin pasar por el clasificador.
    if photo_pending is not None and photo_pending.awaiting_correction:
        await _apply_correction(message, bot, photo_pending, text, settings, providers)
        return

    history = await repo.recent_history(chat_id, chat.HISTORY_TURNS)
    await repo.save_message(chat_id, user_id, "user", text)

    try:
        intent = await chat.classify(
            text,
            history,
            has_pending=current is not None,
            settings=settings,
            providers=providers,
        )
    except LLMError as exc:
        log.warning("intent_failed", chat_id=chat_id, error=str(exc))
        await _reply(message, chat_id, compose.NO_LLM_TEXT)
        return

    # Respuestas a algo pendiente: "sí" y "no" valen igual que los botones.
    if photo_pending is not None:
        if intent.action == "confirm":
            await _reply(message, chat_id, await actions.confirm_photo(photo_pending))
            await actions.continue_queue(bot, chat_id, settings, providers, pending)
            return
        if intent.action == "reject":
            await _reply(message, chat_id, await actions.reject_photo(photo_pending))
            await actions.continue_queue(bot, chat_id, settings, providers, pending)
            return
        if intent.action == "correct_pending":
            await _apply_correction(message, bot, photo_pending, text, settings, providers)
            return
        await _reply(
            message,
            chat_id,
            "Tengo una foto pendiente de confirmar. Responde con ✅ / ✏️ / ❌ del mensaje anterior.",
        )
        return

    if isinstance(current, PendingEdit):
        if intent.action == "confirm":
            await _reply(message, chat_id, await actions.apply_edit(current, user_id))
            pending.clear(chat_id)
            return
        if intent.action == "reject":
            pending.clear(chat_id)
            await _reply(message, chat_id, "Listo, no cambio nada.")
            return
        # Preguntó otra cosa: la edición pendiente caduca en vez de quedar colgada.
        pending.clear(chat_id)

    today = datetime.now(settings.zoneinfo).date()
    reply = await chat.dispatch(intent, today=today, store=pending, chat_id=chat_id)
    await repo.save_message(chat_id, None, "assistant", reply.text)

    if reply.edit is not None and reply.candidates is not None:
        pending.set(reply.edit)
        await message.answer(
            reply.text, reply_markup=candidates_keyboard(reply.candidates, reply.edit.edit_id)
        )
    elif reply.edit is not None:
        pending.set(reply.edit)
        await message.answer(reply.text, reply_markup=edit_keyboard(reply.edit.edit_id))
    else:
        await message.answer(reply.text)

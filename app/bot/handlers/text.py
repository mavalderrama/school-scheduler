"""Texto libre: corrección de lo pendiente, o intención clasificada por el LLM (flujo 7.2)."""

from __future__ import annotations

from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.types import Message

from app.bot import actions
from app.bot.keyboards import (
    candidates_keyboard,
    confirmation_keyboard,
    edit_keyboard,
    question_keyboard,
)
from app.bot.present import present_extraction
from app.config import Settings
from app.db import repo
from app.llm import compose
from app.llm.provider import LLMError, LLMProviders
from app.log import get_logger
from app.services import chat, ingest
from app.services.confirm import PendingEdit, PendingPhoto, PendingQuestions, PendingStore

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
    pending: PendingStore,
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
    current.awaiting_correction = False
    await present_extraction(
        bot, current.chat_id, current.source_id, corrected, pending, edit_message=status
    )


async def _answer_question(
    message: Message,
    bot: Bot,
    current: PendingQuestions,
    text: str,
    settings: Settings,
    providers: LLMProviders,
    pending: PendingStore,
) -> None:
    """Guarda la respuesta y, cuando están todas, reinterpreta la extracción con ellas.

    Antes de nada mira si el usuario quiere dejarlo: durante el interrogatorio cualquier
    texto se tomaba como respuesta y no había forma de salir. La comprobación es en Python,
    sin LLM, para que funcione también con el proveedor caído.
    """
    if chat.is_cancel(text):
        await _reply(message, current.chat_id, await actions.reject_photo(current))
        await actions.continue_queue(bot, current.chat_id, settings, providers, pending)
        return

    current.answer(text)
    if not current.complete:
        remaining = len(current.questions) - len(current.answers) - 1
        await message.answer(compose.format_question(current.current or "", remaining=remaining))
        return

    status = await message.reply("🧠 Ya está, déjame recomponerlo...")
    try:
        refined = await ingest.refine_extraction(
            current.source_id, current.extraction, current.answers, settings, providers
        )
    except LLMError as exc:
        log.warning("refine_failed", source_id=current.source_id, error=str(exc))
        # Devolver la respuesta a la cola: si no, la pregunta se daba por contestada y el
        # usuario se quedaba atrapado respondiendo algo que nunca se procesaba.
        if current.answers:
            current.answers.pop()
        await status.edit_text(
            "⚠️ La IA no respondió, así que no pude procesarlo. Puedes contestarme otra vez "
            "o dejarlo con el botón.\n\n"
            + compose.format_question(current.current or "", remaining=0),
            reply_markup=question_keyboard(current.source_id),
        )
        return
    # Otra ronda si sigue faltando algo; `attempts` acaba cortando el bucle.
    await present_extraction(
        bot,
        current.chat_id,
        current.source_id,
        refined,
        pending,
        attempts=current.attempts + 1,
        edit_message=status,
    )


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

    # Respuesta a una pregunta del bot: es un dato, no una intención que clasificar.
    if isinstance(current, PendingQuestions):
        await _answer_question(message, bot, current, text, settings, providers, pending)
        return

    # Tras ✏️ el siguiente mensaje es la corrección, sin pasar por el clasificador.
    if photo_pending is not None and photo_pending.awaiting_correction:
        await _apply_correction(message, bot, photo_pending, text, settings, providers, pending)
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
            await _reply(message, chat_id, await actions.confirm_photo(photo_pending, settings))
            await actions.continue_queue(bot, chat_id, settings, providers, pending)
            return
        if intent.action == "reject":
            await _reply(message, chat_id, await actions.reject_photo(photo_pending))
            await actions.continue_queue(bot, chat_id, settings, providers, pending)
            return
        if intent.action == "correct_pending":
            await _apply_correction(message, bot, photo_pending, text, settings, providers, pending)
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
    reply = await chat.dispatch(
        intent,
        today=today,
        store=pending,
        chat_id=chat_id,
        country=settings.school_country,
    )
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

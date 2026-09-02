"""Texto libre. En Fase 1 solo sirve para corregir una lectura pendiente (tras ✏️)."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import Message

from app.bot.keyboards import confirmation_keyboard
from app.config import Settings
from app.llm.compose import format_extraction
from app.llm.provider import LLMError, LLMProviders
from app.log import get_logger
from app.services import ingest
from app.services.confirm import PendingStore

log = get_logger(__name__)
router = Router(name="text")


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(
    message: Message,
    bot: Bot,
    settings: Settings,
    providers: LLMProviders,
    pending: PendingStore,
) -> None:
    chat_id = message.chat.id
    current = pending.get(chat_id)
    if current is None:
        await message.reply(
            "Por ahora solo leo fotos de la agenda. Mándame una foto y te digo qué entendí. "
            "Las consultas por texto llegan en la siguiente fase."
        )
        return
    if not current.awaiting_correction:
        await message.reply(
            "Hay una lectura pendiente: responde con los botones ✅ / ✏️ / ❌ del mensaje anterior."
        )
        return

    status = await message.reply("✏️ Aplicando la corrección...")
    try:
        corrected = await ingest.correct_extraction(
            current.source_id, current.extraction, message.text or "", settings, providers
        )
    except LLMError as exc:
        log.warning("correction_failed", source_id=current.source_id, error=str(exc))
        await status.edit_text(
            "⚠️ No pude aplicar la corrección ahora (el proveedor de IA no respondió). "
            "Inténtalo otra vez o usa ❌ para descartar."
        )
        current.awaiting_correction = False
        await bot.send_message(
            chat_id,
            format_extraction(current.extraction),
            reply_markup=confirmation_keyboard(current.source_id),
        )
        return
    current.extraction = corrected
    current.awaiting_correction = False
    summary = await status.edit_text(
        format_extraction(corrected), reply_markup=confirmation_keyboard(current.source_id)
    )
    current.summary_message_id = summary.message_id if isinstance(summary, Message) else None

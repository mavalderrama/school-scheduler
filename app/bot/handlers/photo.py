"""Fotos de la agenda: una a la vez por chat; el resto espera en cola."""

from __future__ import annotations

from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.types import Message

from app.bot.present import present_extraction
from app.config import Settings
from app.llm.provider import LLMProviders
from app.log import get_logger
from app.services import ingest
from app.services.confirm import PendingStore, QueuedPhoto

log = get_logger(__name__)
router = Router(name="photo")

READING_TEXT = "📷 Leyendo la agenda... (puede tardar un par de minutos)"


async def start_ingest(
    bot: Bot,
    chat_id: int,
    photo: QueuedPhoto,
    settings: Settings,
    providers: LLMProviders,
    pending: PendingStore,
) -> None:
    """Procesa una foto y deja la confirmación pendiente en el chat."""

    async def download(file_id: str, destination: Path) -> None:
        await bot.download(file_id, destination=destination)

    await bot.send_chat_action(chat_id, "typing")
    status = await bot.send_message(chat_id, READING_TEXT)
    try:
        result = await ingest.ingest_photo(
            file_id=photo.file_id,
            user_id=photo.user_id,
            display_name=photo.display_name,
            chat_id=chat_id,
            download=download,
            settings=settings,
            providers=providers,
        )
    except ingest.IngestError as exc:
        await status.edit_text(f"⚠️ {exc.user_message}")
        return
    # Decide entre preguntar lo que falta y pedir confirmación.
    await present_extraction(
        bot, chat_id, result.source_id, result.extraction, pending, edit_message=status
    )


@router.message(F.photo)
async def on_photo(
    message: Message,
    bot: Bot,
    settings: Settings,
    providers: LLMProviders,
    pending: PendingStore,
) -> None:
    if message.from_user is None or not message.photo:
        return
    photo = QueuedPhoto(
        file_id=message.photo[-1].file_id,  # la de mayor resolución
        user_id=message.from_user.id,
        display_name=message.from_user.full_name,
    )
    chat_id = message.chat.id
    if pending.get(chat_id) is not None:
        position = pending.enqueue(chat_id, photo)
        await message.reply(
            "Tengo una lectura pendiente de confirmar. Responde primero con los botones "
            f"✅ / ✏️ / ❌ y sigo con esta foto (en cola: {position})."
        )
        return
    await start_ingest(bot, chat_id, photo, settings, providers, pending)

"""Whitelist: todo update fuera de ALLOWED_USER_IDS / ALLOWED_CHAT_IDS se ignora en silencio."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from app.log import get_logger

log = get_logger(__name__)


def _ids_from_update(update: Update) -> tuple[int | None, int | None]:
    """Devuelve (user_id, chat_id) del update, o None si no aplica."""
    message: Message | None = update.message or update.edited_message
    if message is not None:
        user_id = message.from_user.id if message.from_user else None
        return user_id, message.chat.id
    callback: CallbackQuery | None = update.callback_query
    if callback is not None:
        chat_id = None
        if isinstance(callback.message, Message):
            chat_id = callback.message.chat.id
        return callback.from_user.id, chat_id
    return None, None


class AuthMiddleware(BaseMiddleware):
    """Middleware externo sobre `dp.update`: descarta todo lo que no venga de la familia."""

    def __init__(self, allowed_user_ids: list[int], allowed_chat_ids: list[int]) -> None:
        self.allowed_users = frozenset(allowed_user_ids)
        self.allowed_chats = frozenset(allowed_chat_ids)

    def is_allowed(self, user_id: int | None, chat_id: int | None) -> bool:
        return (
            user_id is not None
            and chat_id is not None
            and user_id in self.allowed_users
            and chat_id in self.allowed_chats
        )

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)
        user_id, chat_id = _ids_from_update(event)
        if not self.is_allowed(user_id, chat_id):
            log.debug("update_ignored", user_id=user_id, chat_id=chat_id)
            return None
        return await handler(event, data)

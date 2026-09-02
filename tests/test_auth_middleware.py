"""La whitelist descarta en silencio todo lo que no venga de la familia."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.bot.middlewares.auth import AuthMiddleware

ALLOWED_USERS = [111, 222]
ALLOWED_CHATS = [-100999, 111, 222]


def _message(user_id: int, chat_id: int) -> Update:
    chat_type = "supergroup" if chat_id < 0 else "private"
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type=chat_type),
            from_user=User(id=user_id, is_bot=False, first_name="Test"),
            text="/ping",
        ),
    )


def _callback(user_id: int, chat_id: int) -> Update:
    return Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="cb",
            from_user=User(id=user_id, is_bot=False, first_name="Test"),
            chat_instance="x",
            data="confirm",
            message=Message(
                message_id=5,
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type="supergroup"),
                text="¿Confirmas?",
            ),
        ),
    )


class _Handler:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, event: Any, data: dict[str, Any]) -> str:
        self.calls += 1
        return "handled"


async def _run(update: Update) -> tuple[Any, int]:
    middleware = AuthMiddleware(ALLOWED_USERS, ALLOWED_CHATS)
    handler = _Handler()
    result = await middleware(handler, update, {})
    return result, handler.calls


async def test_allowed_user_in_group_passes() -> None:
    result, calls = await _run(_message(111, -100999))
    assert result == "handled" and calls == 1


async def test_allowed_user_in_private_chat_passes() -> None:
    result, calls = await _run(_message(222, 222))
    assert result == "handled" and calls == 1


async def test_unknown_user_in_allowed_group_is_dropped() -> None:
    result, calls = await _run(_message(999, -100999))
    assert result is None and calls == 0


async def test_allowed_user_in_unknown_chat_is_dropped() -> None:
    result, calls = await _run(_message(111, -100123))
    assert result is None and calls == 0


async def test_callback_query_is_checked_too() -> None:
    assert (await _run(_callback(111, -100999)))[1] == 1
    assert (await _run(_callback(999, -100999)))[1] == 0

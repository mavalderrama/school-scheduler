"""Fotos de la agenda: una a la vez por chat; el resto espera en cola.

La cola vive ahora en el estado del grafo, así que sobrevive a un reinicio. Antes era un
`deque` en memoria y una foto encolada no dejaba ni rastro.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import Message

from app.bot.deliver import deliver
from app.graph.runner import GraphRunner
from app.graph.state import GraphState
from app.llm import compose
from app.log import get_logger
from app.services import scope

log = get_logger(__name__)
router = Router(name="photo")

READING_TEXT = "📷 Leyendo la agenda... (puede tardar un par de minutos)"


def photo_payload(message: Message) -> dict[str, object]:
    assert message.from_user is not None and message.photo is not None
    return {
        "file_id": message.photo[-1].file_id,  # la de mayor resolución
        "user_id": message.from_user.id,
        "display_name": message.from_user.full_name,
        "caption": (message.caption or "").strip() or None,
    }


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot, runner: GraphRunner) -> None:
    if message.from_user is None or not message.photo:
        return
    chat_id = message.chat.id
    sc = await scope.for_chat(chat_id)
    if sc is None:
        await message.answer(compose.NOT_LINKED_TEXT)
        return
    payload = photo_payload(message)

    if await runner.is_waiting(chat_id):
        position = await runner.enqueue(chat_id, payload)
        await message.reply(
            "Tengo una lectura pendiente de confirmar. Responde primero con los botones "
            f"y sigo con esta foto (en cola: {position})."
        )
        return

    await bot.send_chat_action(chat_id, "typing")
    await bot.send_message(chat_id, READING_TEXT)
    state: GraphState = {
        "chat_id": chat_id,
        "child_id": sc.child_id,
        "flow": "photo",
        "photo": payload,
        "queue": [],
        "questions": [],
        "answers": [],
        "attempts": 0,
    }
    turn = await runner.start(chat_id, state)
    await deliver(bot, chat_id, turn)
    if turn.finished:
        nxt = await runner.drain(chat_id)
        if nxt is not None:
            await bot.send_message(chat_id, "Sigo con la siguiente foto de la cola.")
            await deliver(bot, chat_id, nxt)

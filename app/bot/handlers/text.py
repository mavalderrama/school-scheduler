"""Texto libre.

El orden importa y es el mismo de siempre, solo que ahora el estado lo lleva el grafo:

1. Si el grafo está esperando algo en este chat, el mensaje **es la respuesta** y no pasa
   por el clasificador: contestar una pregunta o dar una corrección son datos, no
   intenciones. Un «descarta» lo reconoce el propio grafo, sin LLM.
2. Si no espera nada, se clasifica la intención y se despacha.
"""

from __future__ import annotations

from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.types import Message

from app.bot.deliver import deliver
from app.bot.keyboards import candidates_keyboard, edit_keyboard
from app.config import Settings
from app.db import repo
from app.graph.runner import GraphRunner
from app.graph.state import GraphState
from app.llm import compose
from app.llm.provider import LLMError, LLMProviders
from app.log import get_logger
from app.services import chat, scope

log = get_logger(__name__)
router = Router(name="text")


async def _reply(message: Message, chat_id: int, text: str) -> Message:
    """Responde y guarda la respuesta en el historial corto."""
    await repo.save_message(chat_id, None, "assistant", text)
    return await message.answer(text)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(
    message: Message,
    bot: Bot,
    settings: Settings,
    providers: LLMProviders,
    runner: GraphRunner,
) -> None:
    chat_id = message.chat.id
    text = (message.text or "").strip()
    if not text:
        return
    sc = await scope.for_chat(chat_id)
    if sc is None:
        await message.answer(compose.NOT_LINKED_TEXT)
        return
    user_id = message.from_user.id if message.from_user else None

    # 1) El grafo está esperando: esto es la respuesta, no una intención que clasificar.
    if await runner.is_waiting(chat_id):
        turn = await runner.resume(chat_id, text)
        if turn is not None:
            await deliver(bot, chat_id, turn)
            if turn.finished:
                nxt = await runner.drain(chat_id)
                if nxt is not None:
                    await bot.send_message(chat_id, "Sigo con la siguiente foto de la cola.")
                    await deliver(bot, chat_id, nxt)
            return

    history = await repo.recent_history(chat_id, chat.HISTORY_TURNS)
    await repo.save_message(chat_id, user_id, "user", text)

    try:
        intent = await chat.classify(
            text,
            history,
            has_pending=False,
            settings=settings,
            providers=providers,
            family_id=sc.family_id,
        )
    except LLMError as exc:
        log.warning("intent_failed", chat_id=chat_id, error=str(exc))
        await _reply(message, chat_id, compose.NO_LLM_TEXT)
        return

    today = datetime.now(sc.zoneinfo).date()
    reply = await chat.dispatch(sc, intent, today=today, chat_id=chat_id)
    await repo.save_message(chat_id, None, "assistant", reply.text)

    if reply.edit is None:
        await message.answer(reply.text)
        return

    # Un alta o una baja abre su propio flujo en el grafo, así que también sobrevive.
    state: GraphState = {
        "chat_id": chat_id,
        "child_id": sc.child_id,
        "flow": "edit",
        "edit": reply.edit,
        "user_id": user_id,
        "queue": [],
    }
    await runner.start(chat_id, state)
    keyboard = (
        candidates_keyboard(reply.candidates, int(reply.edit["edit_id"]))
        if reply.candidates
        else edit_keyboard(int(reply.edit["edit_id"]))
    )
    await message.answer(reply.text, reply_markup=keyboard)

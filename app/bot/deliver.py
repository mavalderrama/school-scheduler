"""Traduce lo que devuelve el grafo a mensajes de Telegram.

Sustituye a `present.py`. Los nodos no mandan nada —al reanudar se volverían a ejecutar y
reenviarían el mensaje—, así que el `interrupt()` dice *qué* mostrar y esto lo envía, ya
fuera del grafo.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from app.bot.keyboards import (
    confirmation_keyboard,
    edit_keyboard,
    no_reminder_keyboard,
    question_keyboard,
    schedule_keyboard,
)
from app.graph.runner import Ask, Turn
from app.llm import compose
from app.log import get_logger

log = get_logger(__name__)


def keyboard_for(ask: Ask) -> InlineKeyboardMarkup | None:
    """El teclado que toca según lo que esté preguntando el grafo."""
    if ask.kind == "ask" and ask.source_id is not None:
        return question_keyboard(ask.source_id)
    if ask.kind == "summary" and ask.source_id is not None:
        if ask.schedules:
            return schedule_keyboard(ask.source_id, ask.schedules)
        return confirmation_keyboard(ask.source_id)
    if ask.kind == "edit" and ask.edit is not None:
        return edit_keyboard(int(ask.edit.get("edit_id", 0)))
    if ask.kind == "offer_reminder" and ask.edit is not None:
        return no_reminder_keyboard(int(ask.edit.get("edit_id", 0)))
    return None


def text_for(ask: Ask) -> str:
    if ask.kind == "correction":
        return (
            "✏️ Dime qué corrijo en un mensaje (por ejemplo: «el disfraz es el jueves, no "
            "el martes» o «quita la tarea de matemáticas»)."
        )
    return ask.text or ""


async def deliver(bot: Bot, chat_id: int, turn: Turn) -> None:
    """Manda al chat lo que haya salido del grafo: una pregunta, un resumen o el final."""
    if turn.ask is not None:
        ask = turn.ask
        if ask.kind == "summary" and ask.gave_up:
            await bot.send_message(chat_id, compose.GIVE_UP_TEXT)
        await bot.send_message(chat_id, text_for(ask), reply_markup=keyboard_for(ask))
        return
    if turn.error:
        await bot.send_message(chat_id, f"⚠️ {turn.error}")
        return
    if turn.reply:
        await bot.send_message(chat_id, turn.reply)

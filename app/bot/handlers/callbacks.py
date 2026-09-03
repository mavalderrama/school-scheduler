"""Botones: todos reanudan el grafo con la decisión que pulsó el usuario.

El grafo comprueba si el hilo está esperando algo antes de reanudar, así que un botón de
un mensaje viejo (o una doble pulsación) se ignora en vez de envenenar la conversación
siguiente.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot, Router
from aiogram.types import CallbackQuery, Message

from app.bot.deliver import deliver
from app.bot.keyboards import (
    CandidateCallback,
    EditCallback,
    ScheduleCallback,
    SourceCallback,
)
from app.graph.runner import GraphRunner
from app.log import get_logger

log = get_logger(__name__)
router = Router(name="callbacks")

STALE = "Esto ya no está activo."


async def _resume(
    query: CallbackQuery, bot: Bot, runner: GraphRunner, value: dict[str, Any], notice: str
) -> None:
    """Reanuda el grafo y manda lo que salga. Común a todos los botones."""
    message = query.message
    if not isinstance(message, Message):
        await query.answer("Este mensaje ya no está disponible.")
        return
    chat_id = message.chat.id
    turn = await runner.resume(chat_id, value)
    if turn is None:
        await query.answer(STALE)
        await message.edit_reply_markup(reply_markup=None)
        return

    await query.answer(notice)
    await message.edit_reply_markup(reply_markup=None)
    await deliver(bot, chat_id, turn)
    if turn.finished:
        nxt = await runner.drain(chat_id)
        if nxt is not None:
            await bot.send_message(chat_id, "Sigo con la siguiente foto de la cola.")
            await deliver(bot, chat_id, nxt)


@router.callback_query(SourceCallback.filter())
async def on_source_action(
    query: CallbackQuery, callback_data: SourceCallback, bot: Bot, runner: GraphRunner
) -> None:
    """✅ / ✏️ / ❌ de una foto (❌ vale también durante el interrogatorio)."""
    notices = {"confirm": "Guardado", "reject": "Descartado", "correct": "Dime"}
    await _resume(
        query,
        bot,
        runner,
        {"action": callback_data.action, "source_id": callback_data.source_id},
        notices.get(callback_data.action, "Hecho"),
    )


@router.callback_query(ScheduleCallback.filter())
async def on_schedule_action(
    query: CallbackQuery, callback_data: ScheduleCallback, bot: Bot, runner: GraphRunner
) -> None:
    """Horario nuevo con otros vigentes: añadirlo aparte o reemplazar uno concreto."""
    replace_ids = [callback_data.target] if callback_data.action == "replace" else []
    await _resume(
        query,
        bot,
        runner,
        {"action": "confirm", "replace_ids": replace_ids},
        "Guardado",
    )


@router.callback_query(EditCallback.filter())
async def on_edit_action(
    query: CallbackQuery, callback_data: EditCallback, bot: Bot, runner: GraphRunner
) -> None:
    """✅ / ❌ de un alta o una baja pedida por texto."""
    action = "confirm" if callback_data.action == "confirm" else "reject"
    await _resume(
        query,
        bot,
        runner,
        {"action": action, "edit_id": callback_data.edit_id},
        "Hecho" if action == "confirm" else "Cancelado",
    )


@router.callback_query(CandidateCallback.filter())
async def on_candidate_chosen(
    query: CallbackQuery, callback_data: CandidateCallback, bot: Bot, runner: GraphRunner
) -> None:
    """Elección entre varias entradas candidatas a borrar."""
    await _resume(
        query,
        bot,
        runner,
        {"action": "confirm", "entry_id": callback_data.entry_id},
        "Hecho",
    )

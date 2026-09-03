"""Reintento de una foto que se quedó sin leer. El resto lo lleva el grafo.

Antes este módulo tenía la lógica compartida entre botones y texto; ahora esa lógica son
nodos del grafo y los dos caminos reanudan el mismo hilo, así que no pueden divergir.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiogram import Bot

from app.bot.deliver import deliver
from app.db import repo
from app.db.models import Source, SourceStatus
from app.graph.runner import GraphRunner
from app.graph.state import GraphState
from app.log import get_logger

log = get_logger(__name__)


async def resume_photo(bot: Bot, source: Source, runner: GraphRunner) -> bool:
    """Relee una foto que quedó pendiente por cuota y pide confirmación.

    Devuelve False sin ruido si todavía no se puede: el job lo volverá a intentar.
    """
    if source.chat_id is None or source.local_path is None:
        return False
    if await runner.is_waiting(source.chat_id):
        return False  # el chat está ocupado con otra conversación
    image_path = Path(source.local_path)
    if not await asyncio.to_thread(image_path.is_file):
        log.warning("retry_photo_missing_file", source_id=source.pk, path=source.local_path)
        await repo.update_source(source.pk, status=SourceStatus.FAILED)
        return False

    state: GraphState = {
        "chat_id": source.chat_id,
        "child_id": source.child_id,
        "flow": "photo",
        "source_id": source.pk,
        "photo": {"local_path": source.local_path, "caption": source.caption},
        "queue": [],
        "questions": [],
        "answers": [],
        "attempts": 0,
    }
    turn = await runner.start(source.chat_id, state)
    if turn.error:
        log.warning("retry_photo_failed", source_id=source.pk, error=turn.error)
        return False

    await bot.send_message(source.chat_id, "⏳ Ya pude leer la foto que quedó pendiente:")
    await deliver(bot, source.chat_id, turn)
    log.info("retry_photo_ok", source_id=source.pk, chat_id=source.chat_id)
    return True

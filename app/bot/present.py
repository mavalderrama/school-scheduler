"""Qué se le muestra al chat cuando hay una extracción lista.

Vive aparte de `actions.py` para no cerrar un ciclo de imports (`actions` importa el
handler de fotos y el handler de fotos necesita esto). Es el **único** sitio que decide
entre preguntar lo que falta y pedir confirmación, así que la foto nueva, el reintento
tras cuota, la corrección y el refinado se comportan igual.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import Message

from app.bot.keyboards import confirmation_keyboard, question_keyboard, schedule_keyboard
from app.db import repo
from app.llm import compose
from app.llm.schemas import ExtractionResult
from app.log import get_logger
from app.services import ingest
from app.services.confirm import PendingPhoto, PendingQuestions, PendingStore

log = get_logger(__name__)


async def present_extraction(
    bot: Bot,
    chat_id: int,
    source_id: int,
    extraction: ExtractionResult,
    pending: PendingStore,
    *,
    attempts: int = 0,
    edit_message: Message | None = None,
) -> None:
    """Deja lo pendiente en el chat: o el interrogatorio, o el resumen con ✅/✏️/❌.

    Es el único sitio que decide entre preguntar y confirmar, así que la foto nueva, el
    reintento tras cuota, la corrección y el refinado se comportan igual.
    """
    questions = ingest.pending_questions(extraction)
    if questions and attempts < ingest.MAX_REFINE_ROUNDS:
        state = PendingQuestions(
            source_id=source_id,
            chat_id=chat_id,
            extraction=extraction,
            questions=questions,
            attempts=attempts,
        )
        pending.set(state)
        text = compose.format_question(questions[0], remaining=len(questions) - 1)
        keyboard = question_keyboard(source_id)
        if edit_message is not None:
            await edit_message.edit_text(text, reply_markup=keyboard)
        else:
            await bot.send_message(chat_id, text, reply_markup=keyboard)
        log.info("questions_asked", source_id=source_id, count=len(questions))
        return

    if questions:
        # Se agotaron las rondas: mejor decirlo que preguntar lo mismo otra vez.
        await bot.send_message(chat_id, compose.GIVE_UP_TEXT)

    text = compose.format_extraction(extraction)
    keyboard = confirmation_keyboard(source_id)
    if extraction.doc_type == "schedule":
        # Con otros horarios ya vigentes hay que preguntar: añadir aparte o reemplazar
        # uno concreto. Antes el nuevo pisaba a los anteriores sin avisar.
        existing = [(t.pk, t.name) for t in await repo.active_schedules()]
        if existing:
            names = ", ".join(f"«{n}»" for _, n in existing[:3])
            text += (
                f"\n\nYa tengo {len(existing)} horario(s) vigente(s): {names}.\n"
                "¿Lo añado aparte o reemplaza a uno?"
            )
            keyboard = schedule_keyboard(source_id, existing)
    summary = (
        await edit_message.edit_text(text, reply_markup=keyboard)
        if edit_message is not None
        else await bot.send_message(chat_id, text, reply_markup=keyboard)
    )
    pending.set(
        PendingPhoto(
            source_id=source_id,
            chat_id=chat_id,
            extraction=extraction,
            summary_message_id=summary.message_id if isinstance(summary, Message) else None,
        )
    )

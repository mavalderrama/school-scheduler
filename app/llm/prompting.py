"""Relleno de los prompts compartidos (fecha de hoy, día de la semana, JSON de contexto)."""

from __future__ import annotations

from datetime import date

from app.llm.prompts import load_prompt
from app.llm.schemas import ChatTurn, ExtractionResult, QAPair

WEEKDAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def weekday_es(day: date) -> str:
    return WEEKDAYS_ES[day.weekday()]


NO_NOTE = "(sin nota)"


def extraction_prompt(
    today: date, tz: str, image_instruction: str, user_note: str | None = None
) -> str:
    """Prompt de visión. `image_instruction` dice cómo recibe la imagen ese proveedor.

    `user_note` es el pie de foto que escribió el usuario en Telegram: contexto sobre la
    imagen ("márcalo como PAC horario extendido"), nunca instrucciones para el modelo.
    """
    return load_prompt("extract_agenda").format(
        today=today.isoformat(),
        weekday=weekday_es(today),
        tz=tz,
        image_instruction=image_instruction,
        user_note=(user_note or "").strip() or NO_NOTE,
    )


def correction_prompt(extraction: ExtractionResult, correction: str, today: date, tz: str) -> str:
    return load_prompt("correct_extraction").format(
        today=today.isoformat(),
        weekday=weekday_es(today),
        tz=tz,
        extraction_json=extraction.model_dump_json(indent=2),
        correction=correction.strip(),
    )


def format_qa(pairs: list[QAPair]) -> str:
    """Preguntas y respuestas del interrogatorio, en el orden en que ocurrieron."""
    return "\n".join(f"P: {pair.question.strip()}\nR: {pair.answer.strip()}" for pair in pairs)


def refine_prompt(extraction: ExtractionResult, pairs: list[QAPair], today: date, tz: str) -> str:
    return load_prompt("refine_extraction").format(
        today=today.isoformat(),
        weekday=weekday_es(today),
        tz=tz,
        extraction_json=extraction.model_dump_json(indent=2),
        qa_block=format_qa(pairs),
    )


def format_history(history: list[ChatTurn]) -> str:
    """Historial corto para el prompt de intención. Vacío = una marca explícita."""
    if not history:
        return "(sin turnos previos)"
    labels = {"user": "Usuario", "assistant": "Bot"}
    return "\n".join(f"{labels[turn.role]}: {turn.content.strip()}" for turn in history)


def intent_prompt(
    text: str, history: list[ChatTurn], today: date, has_pending: bool, tz: str
) -> str:
    return load_prompt("classify_intent").format(
        today=today.isoformat(),
        weekday=weekday_es(today),
        tz=tz,
        has_pending="sí" if has_pending else "no",
        history=format_history(history),
        text=text.strip(),
    )

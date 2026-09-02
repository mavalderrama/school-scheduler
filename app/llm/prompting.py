"""Relleno de los prompts compartidos (fecha de hoy, día de la semana, JSON de contexto)."""

from __future__ import annotations

from datetime import date

from app.llm.prompts import load_prompt
from app.llm.schemas import ExtractionResult

WEEKDAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def weekday_es(day: date) -> str:
    return WEEKDAYS_ES[day.weekday()]


def extraction_prompt(today: date, tz: str, image_instruction: str) -> str:
    """Prompt de visión. `image_instruction` dice cómo recibe la imagen ese proveedor."""
    return load_prompt("extract_agenda").format(
        today=today.isoformat(),
        weekday=weekday_es(today),
        tz=tz,
        image_instruction=image_instruction,
    )


def correction_prompt(extraction: ExtractionResult, correction: str, today: date, tz: str) -> str:
    return load_prompt("correct_extraction").format(
        today=today.isoformat(),
        weekday=weekday_es(today),
        tz=tz,
        extraction_json=extraction.model_dump_json(indent=2),
        correction=correction.strip(),
    )

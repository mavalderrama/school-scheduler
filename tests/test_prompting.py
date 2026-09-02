"""Relleno de los prompts compartidos. Sin LLM ni DB.

Protege dos cosas frágiles: que los `.md` no tengan llaves sueltas (`.format` lanzaría
KeyError en producción) y que la fecha de hoy siga siendo prominente tras haber movido
el bloque CONTEXTO al final para dejar lo estático primero.
"""

from __future__ import annotations

from datetime import date

from app.llm.prompting import correction_prompt, extraction_prompt, weekday_es
from app.llm.schemas import ExtractedEntry, ExtractionResult

TODAY = date(2026, 9, 2)  # miércoles
TZ = "America/Bogota"

EXTRACTION = ExtractionResult(
    entries=[
        ExtractedEntry(entry_date=date(2026, 9, 3), kind="bring", text="sudadera", confidence="low")
    ],
    doubts=["no se lee el jueves"],
    detected_language="es",
)


def test_weekday_es() -> None:
    assert weekday_es(TODAY) == "miércoles"
    assert weekday_es(date(2026, 9, 6)) == "domingo"


def test_extraction_prompt_renders_context_last() -> None:
    prompt = extraction_prompt(TODAY, TZ, "La imagen viene adjunta.")
    assert "HOY es miércoles 2026-09-02" in prompt
    assert TZ in prompt
    assert "La imagen viene adjunta." in prompt
    # Lo estático primero, el contexto volátil al final (preparado para prompt caching).
    assert prompt.index("Reglas para") < prompt.index("=== CONTEXTO ===")
    # Las dos formas de foto están descritas antes del contexto.
    assert prompt.index('doc_type: "agenda"') < prompt.index("=== CONTEXTO ===")
    assert prompt.index('doc_type: "schedule"') < prompt.index("=== CONTEXTO ===")
    assert prompt.rstrip().endswith("Responde únicamente con el JSON.")


def test_correction_prompt_embeds_extraction_and_correction() -> None:
    prompt = correction_prompt(EXTRACTION, "  el disfraz es el jueves  ", TODAY, TZ)
    assert "HOY es miércoles 2026-09-02" in prompt
    assert '"text": "sudadera"' in prompt
    assert "el disfraz es el jueves" in prompt
    assert prompt.index("Reglas:") < prompt.index("=== CONTEXTO ===")

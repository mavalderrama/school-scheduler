"""Resumen de extracción y mensaje de guardado (plantillas, sin LLM)."""

from __future__ import annotations

from datetime import date

from app.llm.compose import format_applied, format_date_es, format_extraction
from app.llm.schemas import ExtractedEntry, ExtractionResult


def test_format_date_es() -> None:
    assert format_date_es(date(2026, 9, 2)) == "miércoles 2 de septiembre"


def test_format_extraction_groups_by_date_and_escapes_html() -> None:
    extraction = ExtractionResult(
        entries=[
            ExtractedEntry(
                entry_date=date(2026, 9, 3), kind="homework", text="pág. 12", confidence="high"
            ),
            ExtractedEntry(
                entry_date=date(2026, 9, 2), kind="bring", text="<botella>", confidence="low"
            ),
            ExtractedEntry(
                entry_date=date(2026, 9, 2), kind="event", text="izada", confidence="medium"
            ),
        ],
        doubts=["no se lee el jueves"],
        detected_language="es",
    )
    text = format_extraction(extraction)
    lines = text.splitlines()
    assert lines[0].startswith("📖")
    assert lines.index("<b>miércoles 2 de septiembre</b>") < lines.index(
        "<b>jueves 3 de septiembre</b>"
    )
    assert "🎒 Llevar: &lt;botella&gt; ❓" in text
    assert "📌 Evento: izada ❔" in text
    assert "📝 Tarea: pág. 12" in text
    assert "• no se lee el jueves" in text
    assert text.endswith("¿Lo guardo?")


def test_format_extraction_without_entries() -> None:
    text = format_extraction(ExtractionResult(entries=[], doubts=[], detected_language="es"))
    assert "No encontré entradas" in text
    assert "❓" not in text


def test_format_applied() -> None:
    assert format_applied([date(2026, 9, 2)], 1, 0) == (
        "✅ Guardado: 1 entrada para miércoles 2 de septiembre."
    )
    assert format_applied([date(2026, 9, 2), date(2026, 9, 3)], 3, 2).endswith(
        "Reemplacé 2 anteriores."
    )

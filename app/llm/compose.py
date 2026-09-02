"""Datos → texto para el usuario (plantillas deterministas, sin LLM). HTML de Telegram."""

from __future__ import annotations

import html
from collections.abc import Iterable
from datetime import date

from app.llm.prompting import weekday_es
from app.llm.schemas import ExtractedEntry, ExtractionResult

MONTHS_ES = [
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
]

KIND_LABELS: dict[str, tuple[str, str]] = {
    "bring": ("🎒", "Llevar"),
    "homework": ("📝", "Tarea"),
    "event": ("📌", "Evento"),
    "note": ("📎", "Nota"),
}

CONFIDENCE_MARK = {"high": "", "medium": " ❔", "low": " ❓"}


def format_date_es(day: date) -> str:
    """'martes 2 de septiembre' (con año solo si no es el actual no se decide aquí)."""
    return f"{weekday_es(day)} {day.day} de {MONTHS_ES[day.month - 1]}"


def _entry_line(entry: ExtractedEntry) -> str:
    emoji, label = KIND_LABELS[entry.kind]
    return f"{emoji} {label}: {html.escape(entry.text)}{CONFIDENCE_MARK[entry.confidence]}"


def _group_by_date(entries: Iterable[ExtractedEntry]) -> dict[date, list[ExtractedEntry]]:
    grouped: dict[date, list[ExtractedEntry]] = {}
    for entry in sorted(entries, key=lambda e: (e.entry_date, e.kind)):
        grouped.setdefault(entry.entry_date, []).append(entry)
    return grouped


def format_extraction(extraction: ExtractionResult) -> str:
    """Resumen de lo leído, agrupado por día, con dudas y marcas de confianza."""
    lines: list[str] = []
    if extraction.entries:
        lines.append("📖 Esto es lo que entendí:")
        for day, entries in _group_by_date(extraction.entries).items():
            lines.append("")
            lines.append(f"<b>{format_date_es(day)}</b>")
            lines.extend(_entry_line(e) for e in entries)
    else:
        lines.append("📖 No encontré entradas con fecha en la foto.")
    if extraction.doubts:
        lines.append("")
        lines.append("⚠️ Dudas:")
        lines.extend(f"• {html.escape(d)}" for d in extraction.doubts)
    if any(e.confidence != "high" for e in extraction.entries):
        lines.append("")
        lines.append("<i>❔ = interpretable · ❓ = difícil de leer</i>")
    lines.append("")
    lines.append("¿Lo guardo?" if extraction.entries else "¿Descarto la foto o me corriges?")
    return "\n".join(lines)


def format_applied(dates: list[date], inserted: int, superseded: int) -> str:
    days = ", ".join(format_date_es(d) for d in dates)
    text = f"✅ Guardado: {inserted} entrada{'s' if inserted != 1 else ''}"
    if days:
        text += f" para {days}"
    if superseded:
        text += f". Reemplacé {superseded} anterior{'es' if superseded != 1 else ''}."
    else:
        text += "."
    return text

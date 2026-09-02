"""Datos → texto para el usuario (plantillas deterministas, sin LLM). HTML de Telegram."""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Protocol

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


class AgendaLike(Protocol):
    """Lo mínimo que compose necesita de una entrada guardada.

    Es un Protocol para que `app/llm/` no dependa de los modelos de Django.
    """

    entry_date: date
    kind: str
    text: str


def stored_line(entry: AgendaLike) -> str:
    emoji, label = KIND_LABELS[entry.kind]
    return f"{emoji} {label}: {html.escape(entry.text)}"


def format_agenda(entries: Sequence[AgendaLike], *, title: str, empty: str) -> str:
    """Entradas vigentes agrupadas por día. Sin LLM: plantilla determinista."""
    if not entries:
        return empty
    grouped: dict[date, list[AgendaLike]] = {}
    for entry in sorted(entries, key=lambda e: (e.entry_date, e.kind)):
        grouped.setdefault(entry.entry_date, []).append(entry)

    lines = [title]
    for day, day_entries in grouped.items():
        lines.append("")
        lines.append(f"<b>{format_date_es(day)}</b>")
        lines.extend(stored_line(e) for e in day_entries)
    return "\n".join(lines)


def describe_entry(entry: AgendaLike) -> str:
    """Una línea para confirmaciones y listas de candidatas."""
    _, label = KIND_LABELS[entry.kind]
    return f"{label.lower()} «{html.escape(entry.text)}» del {format_date_es(entry.entry_date)}"


def format_add_question(entry_date: date, kind: str, text: str) -> str:
    _, label = KIND_LABELS[kind]
    return f"¿Agrego {label.lower()} «{html.escape(text)}» para el {format_date_es(entry_date)}?"


def format_remove_question(entry: AgendaLike) -> str:
    return f"¿Quito {describe_entry(entry)}?"


def format_added(entry: AgendaLike) -> str:
    return f"✅ Agregado: {describe_entry(entry)}."


def format_removed(entry: AgendaLike) -> str:
    return f"✅ Quitado: {describe_entry(entry)}."


def format_candidates(entries: Sequence[AgendaLike]) -> str:
    lines = ["Encontré varias. ¿Cuál quito?"]
    lines.extend(f"• {describe_entry(e)}" for e in entries)
    return "\n".join(lines)


HELP_TEXT = (
    "📚 <b>Bot de la agenda escolar</b>\n"
    "\n"
    "Mándame una <b>foto</b> de la agenda y te digo qué entendí antes de guardar nada.\n"
    "\n"
    "También puedes escribirme normal:\n"
    "• «¿qué hay mañana?» o «¿qué lleva el viernes?»\n"
    "• «¿qué hay esta semana?»\n"
    "• «agrega que el martes lleva disfraz»\n"
    "• «quita lo del jueves»\n"
    "\n"
    "Comandos (funcionan aunque la IA esté caída):\n"
    "/hoy · /manana · /semana · /pendiente · /estado · /ayuda · /ping"
)

NO_LLM_TEXT = (
    "⚠️ No puedo interpretar texto ahora mismo (el proveedor de IA no responde). "
    "Usa /hoy, /manana o /semana."
)


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

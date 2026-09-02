"""Datos → texto para el usuario (plantillas deterministas, sin LLM). HTML de Telegram."""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any, Protocol

from app.llm.prompting import weekday_es
from app.llm.schemas import (
    WEEKDAY_LABELS,
    ExtractedEntry,
    ExtractionResult,
    ScheduleDraft,
    SlotDraft,
)

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


class SlotLike(Protocol):
    """Lo mínimo que compose necesita de una franja resuelta del horario.

    Son propiedades y no atributos porque `SlotResult` es un dataclass congelado: un
    Protocol con atributos exigiría que fueran asignables.
    """

    @property
    def day(self) -> date: ...

    @property
    def week_label(self) -> str | None: ...

    @property
    def rotation(self) -> str | None: ...

    @property
    def subject(self) -> str | None: ...

    @property
    def skipped_reason(self) -> str | None: ...


def slot_line(slot: SlotLike, *, with_date: bool = False) -> str:
    """Una línea de clase. Sin materia se explica por qué, en vez de callar."""
    prefix = f"{format_date_es(slot.day)}: " if with_date else ""
    if slot.subject is None:
        reason = f" ({html.escape(slot.skipped_reason)})" if slot.skipped_reason else ""
        return f"🚫 {prefix}sin clase{reason}"
    week = f" · Semana {html.escape(slot.week_label)}" if slot.week_label else ""
    rotation = f" · rot. {html.escape(slot.rotation)}" if slot.rotation else ""
    return f"🎨 {prefix}<b>{html.escape(slot.subject)}</b>{week}{rotation}"


def format_schedule_draft(draft: ScheduleDraft) -> str:
    """Resumen de un horario recién leído de una foto, para confirmarlo."""
    lines = [f"🗓️ Entendí un <b>horario rotativo</b>: {html.escape(draft.name or 'sin título')}"]
    lines.append(f"Ciclo de {draft.cycle_weeks} semanas, {len(draft.slots)} franjas.")
    by_week: dict[str, list[SlotDraft]] = {}
    for slot in sorted(draft.slots, key=lambda s: (s.week_label, s.weekday)):
        by_week.setdefault(slot.week_label, []).append(slot)
    for label, slots in by_week.items():
        lines.append("")
        lines.append(f"<b>Semana {html.escape(label)}</b>")
        for slot in slots:
            day = WEEKDAY_LABELS.get(slot.weekday, str(slot.weekday))
            rotation = f" ({html.escape(slot.rotation)})" if slot.rotation else ""
            lines.append(f"• {day}: {html.escape(slot.subject)}{rotation}")
    if draft.anchor_monday is not None:
        lines.append("")
        lines.append(
            f"La Semana {next(iter(by_week), 'A')} empezó el {format_date_es(draft.anchor_monday)}."
        )
    return "\n".join(lines)


def format_extraction(extraction: ExtractionResult) -> str:
    """Resumen de lo leído, agrupado por día, con dudas y marcas de confianza."""
    if extraction.doc_type == "schedule" and extraction.schedule is not None:
        parts = [format_schedule_draft(extraction.schedule)]
        if extraction.doubts:
            parts.append("")
            parts.append("⚠️ Dudas:")
            parts.extend(f"• {html.escape(d)}" for d in extraction.doubts)
        parts.append("")
        parts.append("¿Lo guardo?")
        return "\n".join(parts)

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
    "• «¿cuándo hay natación?»\n"
    "\n"
    "Si me mandas la <b>tabla del horario</b> (Semana A / Semana B), te pregunto cuándo "
    "empezó el ciclo y a partir de ahí te digo qué clase toca cada día.\n"
    "\n"
    "Comandos (funcionan aunque la IA esté caída):\n"
    "/hoy · /manana · /semana · /horario · /pendiente · /estado · /ayuda · /ping"
)


NO_SCHEDULE_TEXT = (
    "Todavía no tengo el horario cargado. Mándame una foto de la tabla "
    "(Semana A / Semana B) y te pregunto lo que falte."
)


def format_schedule_table(
    template_name: str,
    slots: Sequence[Any],
    *,
    current_label: str | None = None,
) -> str:
    """La tabla completa del horario para /horario. `slots` son filas de ScheduleSlot."""
    lines = [f"🗓️ <b>{html.escape(template_name)}</b>"]
    if current_label:
        lines.append(f"Esta semana es la <b>Semana {html.escape(current_label)}</b>.")
    by_week: dict[str, list[Any]] = {}
    for slot in slots:
        by_week.setdefault(slot.week_label, []).append(slot)
    for label, week_slots in by_week.items():
        lines.append("")
        marker = " ← esta semana" if label == current_label else ""
        lines.append(f"<b>Semana {html.escape(label)}</b>{marker}")
        for slot in sorted(week_slots, key=lambda s: s.weekday):
            day = WEEKDAY_LABELS.get(slot.weekday, str(slot.weekday))
            rotation = f" ({html.escape(slot.rotation)})" if slot.rotation else ""
            lines.append(f"• {day}: {html.escape(slot.subject)}{rotation}")
    return "\n".join(lines)


def format_next_occurrences(subject: str, slots: Sequence[SlotLike]) -> str:
    """Respuesta a «¿cuándo hay natación?»."""
    if not slots:
        return (
            f"No encuentro «{html.escape(subject)}» en el horario. "
            "Prueba con /horario para ver las materias que tengo."
        )
    name = slots[0].subject or subject
    lines = [f"🎨 Próximas veces que hay <b>{html.escape(name)}</b>:"]
    lines.extend(
        f"• {format_date_es(s.day)}" + (f" · rot. {html.escape(s.rotation)}" if s.rotation else "")
        for s in slots
    )
    return "\n".join(lines)


def format_question(question: str, *, remaining: int) -> str:
    """Una pregunta del interrogatorio, con cuántas quedan si hay más de una."""
    tail = (
        f"\n\n<i>({remaining} pregunta{'s' if remaining != 1 else ''} más)</i>" if remaining else ""
    )
    return f"❓ {html.escape(question)}{tail}"


GIVE_UP_TEXT = (
    "No consigo entender lo que falta. Descarto la foto con ❌ y la mandas de nuevo, "
    "o me lo cuentas de otra forma."
)


def format_schedule_applied(name: str, slots: int, anchor: date) -> str:
    return (
        f"✅ Guardado el horario <b>{html.escape(name)}</b>: {slots} franjas. "
        f"El ciclo empezó el {format_date_es(anchor)}."
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

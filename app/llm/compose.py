"""Datos → texto para el usuario (plantillas deterministas, sin LLM). HTML de Telegram."""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from datetime import date, time
from typing import Any, Protocol

from app.llm.prompting import WEEKDAYS_ES, weekday_es
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


def format_date_es(day: date, *, with_year: bool = False) -> str:
    """'martes 2 de septiembre', o con año si se pide.

    El año se omite por defecto porque casi todo lo que dice el bot es de esta semana. Pero
    donde la fecha está lejos —el vencimiento del token, que es a un año— omitirlo se lee
    como «caduca hoy», así que ahí se pide explícitamente.
    """
    text = f"{weekday_es(day)} {day.day} de {MONTHS_ES[day.month - 1]}"
    return f"{text} de {day.year}" if with_year else text


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

    @property
    def schedule_name(self) -> str | None: ...


def slot_line(slot: SlotLike, *, with_date: bool = False, with_schedule: bool = False) -> str:
    """Una línea de clase. Sin materia se explica por qué, en vez de callar."""
    prefix = f"{format_date_es(slot.day)}: " if with_date else ""
    if slot.subject is None:
        reason = f" ({html.escape(slot.skipped_reason)})" if slot.skipped_reason else ""
        return f"🚫 {prefix}sin clase{reason}"
    week = f" · Semana {html.escape(slot.week_label)}" if slot.week_label else ""
    rotation = f" · rot. {html.escape(slot.rotation)}" if slot.rotation else ""
    # El nombre del horario solo cuando hay más de uno: si no, es ruido en cada línea.
    origin = (
        f" <i>({html.escape(slot.schedule_name)})</i>"
        if with_schedule and slot.schedule_name
        else ""
    )
    return f"🎨 {prefix}<b>{html.escape(slot.subject)}</b>{week}{rotation}{origin}"


def slot_lines(slots: Sequence[SlotLike], *, with_date: bool = False) -> list[str]:
    """Una línea por horario vigente. Marca el origen solo si hay más de uno."""
    many = len({s.schedule_name for s in slots if s.schedule_name}) > 1
    return [slot_line(s, with_date=with_date, with_schedule=many) for s in slots]


def _slot_bullet(slot: SlotDraft) -> str:
    day = WEEKDAY_LABELS.get(slot.weekday, str(slot.weekday))
    rotation = f" ({html.escape(slot.rotation)})" if slot.rotation else ""
    return f"• {day}: {html.escape(slot.subject)}{rotation}"


def format_schedule_draft(draft: ScheduleDraft) -> str:
    """Resumen de un horario recién leído de una foto, para confirmarlo.

    Un ciclo de una semana no es un horario rotativo y no tiene «Semana A»: llamarlo así
    importa el vocabulario de otro horario distinto, que es justo lo que confundía.
    """
    name = html.escape(draft.name or "sin título")
    slot_count = f"{len(draft.slots)} franja{'s' if len(draft.slots) != 1 else ''}"

    if draft.cycle_weeks <= 1:
        lines = [
            f"🗓️ Entendí un <b>horario semanal</b>: {name}",
            f"Se repite igual todas las semanas, {slot_count}.",
            "",
        ]
        lines.extend(_slot_bullet(s) for s in sorted(draft.slots, key=lambda s: s.weekday))
        if draft.anchor_monday is not None:
            lines.append("")
            lines.append(f"Aplica desde el {format_date_es(draft.anchor_monday)}.")
        return "\n".join(lines)

    weeks = f"{draft.cycle_weeks} semanas" if draft.cycle_weeks != 1 else "1 semana"
    lines = [
        f"🗓️ Entendí un <b>horario rotativo</b>: {name}",
        f"Ciclo de {weeks}, {slot_count}.",
    ]
    by_week: dict[str, list[SlotDraft]] = {}
    for slot in sorted(draft.slots, key=lambda s: (s.week_label, s.weekday)):
        by_week.setdefault(slot.week_label, []).append(slot)
    for label, slots in by_week.items():
        lines.append("")
        lines.append(f"<b>Semana {html.escape(label)}</b>")
        lines.extend(_slot_bullet(s) for s in slots)
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
        # Lo que el modelo no tiene claro se enseña, no se pregunta: si algo fuera
        # imprescindible lo habría detectado `ingest.missing_essentials` antes de llegar aquí.
        notes = [*extraction.doubts, *extraction.questions]
        if notes:
            parts.append("")
            parts.append("⚠️ Dudas:")
            parts.extend(f"• {html.escape(d)}" for d in notes)
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
    "• «todos los viernes tiene natación» (se repite cada semana)\n"
    "• «quita la natación de los viernes»\n"
    "• «el martes de la Semana B cámbialo por evento»\n"
    "• «recuérdame todos los días a las 7 que revise la agenda»\n"
    "\n"
    "• «¿cuándo hay natación?»\n"
    "\n"
    "Si me mandas la <b>tabla del horario</b> (Semana A / Semana B), te pregunto cuándo "
    "empezó el ciclo y a partir de ahí te digo qué clase toca cada día.\n"
    "\n"
    "Comandos (funcionan aunque la IA esté caída):\n"
    "/hoy · /manana · /semana · /horario · /recordatorios · /pendiente · /cancelar · "
    "/estado · /ayuda · /ping"
)


NOT_LINKED_TEXT = (
    "Este chat todavía no está vinculado a ningún niño. Usa /vincular para asociarlo, o "
    "escríbeme por privado si aún no tienes familia dada de alta."
)

NO_SCHEDULE_TEXT = (
    "Todavía no tengo ningún horario cargado. Mándame una foto de la tabla "
    "(Semana A / Semana B) y te pregunto lo que falte."
)


def format_schedule_applied_multi(name: str, slots: int, anchor: date, replaced: str | None) -> str:
    """Confirmación de un horario guardado, diciendo si reemplazó a otro o se añadió."""
    what = f"reemplazando a «{html.escape(replaced)}»" if replaced else "añadido aparte"
    return (
        f"✅ Guardado el horario <b>{html.escape(name)}</b> ({what}): {slots} franjas. "
        f"El ciclo empezó el {format_date_es(anchor)}."
    )


def format_schedule_table(
    template_name: str,
    slots: Sequence[Any],
    *,
    current_label: str | None = None,
) -> str:
    """La tabla completa del horario para /horario. `slots` son filas de ScheduleSlot."""
    lines = [f"🗓️ <b>{html.escape(template_name)}</b>"]
    by_week: dict[str, list[Any]] = {}
    for slot in slots:
        by_week.setdefault(slot.week_label, []).append(slot)
    if len(by_week) <= 1:
        # Horario semanal: sin etiquetas de semana, que aquí solo confundirían.
        lines.append("Se repite igual todas las semanas.")
        lines.append("")
        for slot in sorted(next(iter(by_week.values()), []), key=lambda s: s.weekday):
            day = WEEKDAY_LABELS.get(slot.weekday, str(slot.weekday))
            rotation = f" ({html.escape(slot.rotation)})" if slot.rotation else ""
            lines.append(f"• {day}: {html.escape(slot.subject)}{rotation}")
        return "\n".join(lines)
    if current_label:
        lines.append(f"Esta semana es la <b>Semana {html.escape(current_label)}</b>.")
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
    """Una pregunta del interrogatorio, con cuántas quedan si hay más de una.

    Siempre recuerda cómo salir: mientras el bot pregunta, todo lo que escribes cuenta
    como respuesta, así que la salida tiene que estar a la vista.
    """
    tail = (
        f"\n<i>({remaining} pregunta{'s' if remaining != 1 else ''} más)</i>\n" if remaining else ""
    )
    return f"❓ {html.escape(question)}\n{tail}\n<i>Si prefieres dejarlo, dime «descarta».</i>"


REJECTED_TEXT = (
    "❌ Listo, lo descarto. No guardé nada de esta foto; mándamela otra vez cuando quieras."
)

REFINE_FAILED_TEXT = (
    "⚠️ La IA no respondió, así que no pude procesarlo. Puedes contestarme otra vez "
    "o dejarlo con el botón."
)

CORRECTION_FAILED_TEXT = (
    "⚠️ No pude aplicar la corrección ahora (el proveedor de IA no respondió). "
    "Inténtalo otra vez o usa ❌ para descartar."
)

GIVE_UP_TEXT = (
    "No consigo entender lo que falta. Descarto la foto con ❌ y la mandas de nuevo, "
    "o me lo cuentas de otra forma."
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


def format_weekday_list(weekdays: str) -> str:
    """`"25"` → «los martes y viernes». Los días vienen como dígitos ISO, igual que en la DB."""
    names = [WEEKDAYS_ES[int(day) - 1] for day in weekdays]
    if not names:
        return ""
    listed = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} y {names[-1]}"
    return f"los {listed}"


def format_recurring_question(weekdays: str, text: str, *, dropped: Sequence[str] = ()) -> str:
    """La pregunta del alta, diciendo qué entradas sueltas se van con ella.

    Que se vean **antes** del ✅ es la diferencia entre limpiar un duplicado y borrarle algo
    al usuario a sus espaldas.
    """
    question = (
        f"🔁 ¿Apunto <b>{html.escape(text)}</b> {format_weekday_list(weekdays)}, todas las semanas?"
    )
    if not dropped:
        return question
    lines = [question, "", "Y quito estas, que la regla ya cubre:"]
    lines.extend(f"• {d}" for d in dropped)
    return "\n".join(lines)


def format_recurring_added(
    weekdays: str, text: str, *, replaced: bool = False, dropped: int = 0
) -> str:
    """Confirmación del alta + la pregunta del aviso, en un solo mensaje.

    Van juntas porque el grafo interrumpe justo después de guardar: si fueran dos mensajes,
    el primero se perdería (el runner solo manda el valor del `interrupt`).
    """
    what = "Actualizado" if replaced else "Guardado"
    plural = "s" if dropped != 1 else ""
    gone = f" Quité {dropped} entrada{plural} suelta{plural}." if dropped else ""
    return (
        f"✅ {what}: <b>{html.escape(text)}</b> {format_weekday_list(weekdays)}, todas las "
        f"semanas.{gone} Lo verás en /hoy, /manana y /semana.\n"
        f"\n"
        f"⏰ ¿Te aviso a alguna hora esos días? Dime la hora (por ejemplo «18:30» o «6 de "
        f"la tarde») o responde «no»."
    )


NO_RECURRING_REMINDER_TEXT = (
    "👍 Listo, sin aviso. Si luego lo quieres, dime «recuérdame los viernes a las 6 la natación»."
)


class StoredSlotLike(Protocol):
    """Lo que compose necesita de una franja guardada (`ScheduleSlot`)."""

    @property
    def week_label(self) -> str: ...
    @property
    def weekday(self) -> int: ...
    @property
    def subject(self) -> str: ...


def slot_place(slot: StoredSlotLike, *, schedule: str, cycle_weeks: int) -> str:
    """Dónde está la franja: «el martes de la Semana B en «Horario K4A»».

    La etiqueta de la semana solo aparece si el horario tiene ciclo: uno semanal no tiene
    «Semana A», y nombrarla importa el vocabulario de otro horario distinto.
    """
    day = WEEKDAYS_ES[slot.weekday - 1]
    week = f" de la Semana {html.escape(slot.week_label)}" if cycle_weeks > 1 else ""
    return f"el {day}{week} en «{html.escape(schedule)}»"


def slot_button_label(slot: StoredSlotLike, *, schedule: str, cycle_weeks: int) -> str:
    """Etiqueta corta y en texto plano: Telegram no admite HTML en los botones."""
    day = WEEKDAYS_ES[slot.weekday - 1]
    week = f" sem. {slot.week_label}" if cycle_weeks > 1 else ""
    label = f"{schedule}: {day}{week} · {slot.subject}"
    return label[:57] + "…" if len(label) > 60 else label


def format_slot_change_question(place: str, before: str, after: str) -> str:
    return (
        f"🔁 ¿Cambio {place}, que ahora dice «{html.escape(before)}», por "
        f"<b>{html.escape(after)}</b>?"
    )


def format_slot_changed(place: str, before: str, after: str) -> str:
    return (
        f"✅ Cambiado {place}: «{html.escape(before)}» → <b>{html.escape(after)}</b>. "
        f"Lo anterior queda guardado."
    )


def format_slot_candidates(places: Sequence[str]) -> str:
    lines = ["Encontré varias franjas. ¿Cuál cambio?"]
    lines.extend(f"• {p}" for p in places)
    return "\n".join(lines)


def format_remove_schedule_question(name: str) -> str:
    return (
        f"🗑️ ¿Quito el horario <b>{html.escape(name)}</b>? Dejará de salir a partir de hoy; "
        f"lo anterior queda guardado."
    )


def format_schedule_removed(name: str) -> str:
    return f"✅ Quitado el horario <b>{html.escape(name)}</b>. Ya no lo cuento a partir de hoy."


def format_schedule_candidates(names: Sequence[str]) -> str:
    lines = ["Tengo varios horarios. ¿Cuál quito?"]
    lines.extend(f"• {html.escape(n)}" for n in names)
    return "\n".join(lines)


NO_SLOT_FOUND_TEXT = (
    "No encontré esa franja en ningún horario vigente. Mira /horario para ver cómo está "
    "cargado y dime el día tal cual aparece."
)

ASK_SLOT_DAY_TEXT = "¿Qué día cambio? Por ejemplo: «el martes de la Semana B cámbialo por evento»."

ASK_SLOT_SUBJECT_TEXT = "¿Por qué lo cambio? Dime la materia o actividad nueva."


RECURRING_REMINDER_UNCLEAR_TEXT = (
    "No entendí la hora, así que lo dejo sin aviso (lo recurrente ya está guardado). Si lo "
    "quieres, dime «recuérdame los viernes a las 6 la natación»."
)

ASK_RECURRING_DAYS_TEXT = "¿Qué días se repite? Por ejemplo: «todos los viernes tiene natación»."


# --- Recordatorios ---------------------------------------------------------------------


class ReminderLike(Protocol):
    """Lo que compose necesita de un recordatorio, sin importar los modelos de Django."""

    @property
    def text(self) -> str: ...
    @property
    def time_of_day(self) -> time: ...
    @property
    def repeat(self) -> str: ...
    @property
    def weekdays(self) -> str: ...
    @property
    def on_date(self) -> date | None: ...
    @property
    def only_school_days(self) -> bool: ...


def format_hhmm(moment: time) -> str:
    return moment.strftime("%H:%M")


def describe_schedule(
    repeat: str, moment: time, weekdays: str, on_date: date | None, only_school_days: bool
) -> str:
    """«todos los días a las 07:00», «los lunes y miércoles a las 17:30»…"""
    at = f"a las {format_hhmm(moment)}"
    if repeat == "once":
        return f"el {format_date_es(on_date)} {at}" if on_date is not None else at
    if repeat == "weekly":
        listed = format_weekday_list(weekdays)
        return f"{listed} {at}" if listed else at
    return f"todos los días {at}" + (" que haya colegio" if only_school_days else "")


def describe_reminder(reminder: ReminderLike) -> str:
    """Una línea: para confirmar, para listar y para elegir cuál borrar."""
    when = describe_schedule(
        reminder.repeat,
        reminder.time_of_day,
        reminder.weekdays,
        reminder.on_date,
        reminder.only_school_days,
    )
    return f"«{html.escape(reminder.text)}» {when}"


def format_reminder_question(reminder: ReminderLike) -> str:
    return f"⏰ ¿Te aviso {describe_reminder(reminder)}?"


def format_reminder_added(reminder: ReminderLike) -> str:
    return f"✅ Hecho. Te aviso {describe_reminder(reminder)}."


def format_reminder_removed(reminder: ReminderLike) -> str:
    return f"✅ Quitado: ya no te aviso {describe_reminder(reminder)}."


def format_reminders(reminders: Sequence[ReminderLike]) -> str:
    if not reminders:
        return NO_REMINDERS_TEXT
    lines = ["⏰ <b>Recordatorios</b>"]
    lines.extend(f"• {describe_reminder(r)}" for r in reminders)
    lines.append("")
    lines.append("Dime «quita el recordatorio de…» para borrar uno.")
    return "\n".join(lines)


def format_reminder_candidates(reminders: Sequence[ReminderLike]) -> str:
    lines = ["Tengo varios. ¿Cuál quito?"]
    lines.extend(f"• {describe_reminder(r)}" for r in reminders)
    return "\n".join(lines)


def format_reminder(text: str, *, late: bool = False) -> str:
    """El mensaje que llega a la hora."""
    prefix = "⏰ <b>Recordatorio</b>"
    if late:
        # Se dice que va tarde en vez de fingir puntualidad: el bot estuvo caído.
        prefix += " (con retraso)"
    return f"{prefix}\n{html.escape(text)}"


NO_REMINDERS_TEXT = (
    "No tienes recordatorios programados. Dime algo como «recuérdame todos los días a las "
    "7 que revise la agenda»."
)

ASK_REMINDER_TIME_TEXT = (
    "¿A qué hora te aviso? Dímelo con la hora, por ejemplo «a las 7 de la mañana» o «19:30»."
)

REMINDER_NEVER_FIRES_TEXT = (
    "Con esas condiciones no llegaría a sonar nunca. ¿Me lo dices de otra forma?"
)

TOO_MANY_REMINDERS_TEXT = (
    "Ya tienes muchos recordatorios activos. Quita alguno con «quita el recordatorio de…» "
    "antes de añadir otro."
)

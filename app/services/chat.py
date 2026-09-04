"""Texto libre (flujo 7.2): clasificar intención y despachar a la lógica determinista.

El modelo solo clasifica y extrae datos; todo lo que toca la DB lo hace Python. No habla
con Telegram: devuelve un `ChatReply` y el handler decide qué teclado ponerle.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app.config import Settings
from app.db import repo
from app.db.models import AgendaEntry, Reminder
from app.llm import compose
from app.llm.prompting import format_history, weekday_es
from app.llm.provider import LLMError, LLMProviders
from app.llm.schemas import ChatTurn, Intent
from app.log import get_logger
from app.services import cache, reminders
from app.services import schedule as schedule_service
from app.services.scope import Scope

log = get_logger(__name__)

HISTORY_TURNS = 6
MAX_CANDIDATES = 6

CANCEL_WORDS = frozenset(
    {
        "descarta",
        "descartar",
        "descartalo",
        "descartala",
        "cancela",
        "cancelar",
        "cancelalo",
        "olvida",
        "olvidalo",
        "olvidate",
        "dejalo",
        "deja",
        "anula",
        "anular",
        "borra",
        "borralo",
        "basta",
        "para",
        "stop",
        "salir",
        "abortar",
        "nada",
        "❌",
        "no quiero",
        "no sigas",
        "no importa",
        "ya no",
        "mejor no",
        "asi no",
        "dejemoslo",
        "no gracias",
        "basta ya",
        "ya basta",
        "dejalo asi",
    }
)
"""Formas de decir «déjalo» durante el interrogatorio.

Se comprueban en Python, sin LLM: salir de una pregunta tiene que funcionar también
cuando el proveedor está caído, que es justo cuando más ganas dan de salir. Un «no» a
secas NO está en la lista: el modelo puede preguntar cosas de sí/no y ahí es una
respuesta legítima, no una cancelación.
"""


def _strip_accents(text: str) -> str:
    return text.translate(str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN"))


def is_cancel(text: str) -> bool:
    """¿El usuario está diciendo que lo deje, en vez de respondiendo la pregunta?"""
    cleaned = _strip_accents(text).lower().strip().strip(".!¡¿?,;: ")
    return " ".join(cleaned.split()) in CANCEL_WORDS


@dataclass
class ChatReply:
    """Respuesta lista para enviar; `edit` y `candidates` piden teclado al handler.

    `edit` es un dict y no un dataclass porque va tal cual al estado del grafo, que se
    guarda en el checkpointer.
    """

    text: str
    edit: dict[str, Any] | None = None
    candidates: list[tuple[int, str]] | None = None


def _new_edit_id() -> int:
    """Identificador de los botones de una edición.

    Antes era un contador en memoria que al reiniciar volvía a 1 y podía chocar con
    botones viejos. Ahora quien impide reanudar algo caducado es el grafo; esto solo
    distingue mensajes dentro de una conversación.
    """
    return int(time.time() * 1000) % 2_000_000_000


def week_range(today: date) -> tuple[date, date]:
    """De hoy al domingo de esta semana; si ya es fin de semana, la semana que viene."""
    if today.weekday() >= 5:
        monday = today + timedelta(days=7 - today.weekday())
        return monday, monday + timedelta(days=6)
    return today, today + timedelta(days=6 - today.weekday())


def candidate_label(entry: AgendaEntry) -> str:
    """Texto plano y corto para un botón (Telegram no admite HTML en los botones)."""
    label = f"{weekday_es(entry.entry_date)} {entry.entry_date.day}: {entry.text}"
    return label[:60]


# --- Clasificación ------------------------------------------------------------------------


async def classify(
    text: str,
    history: list[ChatTurn],
    *,
    has_pending: bool,
    settings: Settings,
    providers: LLMProviders,
    family_id: int | None = None,
) -> Intent:
    """Clasifica con la cadena de texto, pasando por la caché.

    La caché incluye el historial, así que solo acierta ante repeticiones inmediatas
    (doble envío, reintento): es correcta antes que optimista.
    """
    today = datetime.now(settings.zoneinfo).date()
    key = cache.build_key(
        task="intent",
        today=today,
        tz=settings.tz,
        inputs=[
            str(family_id),
            cache.hash_text(text),
            cache.hash_text(format_history(history)),
            str(has_pending),
        ],
    )
    started = time.monotonic()
    hit = await cache.get(Intent, key, settings)
    if hit is not None:
        await repo.log_llm_call(
            task="intent",
            family_id=family_id,
            provider=cache.CACHE_PROVIDER,
            ok=True,
            error=None,
            usage=None,
            duration_ms=int((time.monotonic() - started) * 1000),
            model=hit.model,
        )
        return hit.value

    try:
        run = await providers.text.run(
            lambda p: p.classify_intent(text, history, today, has_pending)
        )
    except LLMError as exc:
        for attempt in exc.attempts:
            await repo.log_llm_call(
                task="intent",
                family_id=family_id,
                provider=attempt.provider,
                ok=attempt.ok,
                error=attempt.error,
                usage=attempt.usage,
                duration_ms=attempt.duration_ms,
                prompt=attempt.prompt if settings.llm_trace_enabled else None,
                response=attempt.response if settings.llm_trace_enabled else None,
            )
        raise
    for attempt in run.attempts:
        await repo.log_llm_call(
            task="intent",
            family_id=family_id,
            provider=attempt.provider,
            ok=attempt.ok,
            error=attempt.error,
            usage=attempt.usage,
            duration_ms=attempt.duration_ms,
            prompt=attempt.prompt if settings.llm_trace_enabled else None,
            response=attempt.response if settings.llm_trace_enabled else None,
        )
    model = next(
        (a.usage.model for a in reversed(run.attempts) if a.ok and a.usage is not None), None
    )
    await cache.put(
        key, task="intent", provider=run.provider, model=model, value=run.value, settings=settings
    )
    log.info("intent_classified", action=run.value.action, provider=run.provider)
    return run.value


# --- Despacho ------------------------------------------------------------------------------


async def query_range(scope: Scope, intent: Intent, today: date) -> ChatReply:
    date_from = intent.date_from or today
    date_to = intent.date_to or date_from
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    entries = await repo.active_entries(scope.child_id, date_from, date_to)
    if date_from == date_to:
        title = f"📚 <b>{compose.format_date_es(date_from)}</b>:"
        # Un solo día lleva también la clase del horario, que es lo que más se pregunta.
        slots = await schedule_service.resolve_day(scope, date_from)
        lines = [title, *compose.slot_lines(slots)]
        lines.extend(compose.stored_line(e) for e in entries)
        if len(lines) == 1:
            return ChatReply(text=f"No tengo nada para el {compose.format_date_es(date_from)}.")
        return ChatReply(text="\n".join(lines))
    return ChatReply(
        text=compose.format_agenda(
            entries,
            title="📚 Esto es lo que tengo:",
            empty=(
                f"No tengo nada entre el {compose.format_date_es(date_from)} y el "
                f"{compose.format_date_es(date_to)}."
            ),
        )
    )


async def prepare_add(intent: Intent, today: date, chat_id: int) -> ChatReply:
    if intent.date_from is None or not intent.text:
        return ChatReply(
            text="¿Para qué día y qué agrego? Por ejemplo: «agrega que el martes lleva disfraz»."
        )
    text = intent.text.strip()
    kind = intent.kind or "note"
    edit: dict[str, Any] = {
        "edit_id": _new_edit_id(),
        "chat_id": chat_id,
        "action": "add",
        "entry_date": intent.date_from.isoformat(),
        "kind": kind,
        "text": text,
    }
    return ChatReply(text=compose.format_add_question(intent.date_from, kind, text), edit=edit)


async def prepare_recurring(intent: Intent, chat_id: int) -> ChatReply:
    """«Todos los viernes tiene natación»: una regla semanal, no una entrada con fecha.

    Antes esto no tenía dónde caer —`add_entry` exige una fecha y `add_reminder` una hora—,
    así que el clasificador lo mandaba a `unknown` y el bot contestaba «no te entendí» a
    una frase perfectamente clara.
    """
    text = (intent.text or "").strip()
    if not text:
        return ChatReply(text="¿Qué apunto? Por ejemplo: «todos los viernes tiene natación».")
    weekdays = reminders.format_weekdays(intent.weekdays or [])
    if not weekdays:
        return ChatReply(text=compose.ASK_RECURRING_DAYS_TEXT)
    edit: dict[str, Any] = {
        "edit_id": _new_edit_id(),
        "chat_id": chat_id,
        "action": "add_recurring",
        "weekdays": weekdays,
        "text": text,
    }
    return ChatReply(text=compose.format_recurring_question(weekdays, text), edit=edit)


async def prepare_remove(scope: Scope, intent: Intent, today: date, chat_id: int) -> ChatReply:
    date_from = intent.date_from or today
    date_to = intent.date_to or date_from
    candidates = await repo.find_active_entries(
        scope.child_id, date_from, date_to, intent.target_entry_hint
    )
    if not candidates and intent.target_entry_hint:
        # La pista no casó con nada: reintenta sin filtrar por texto.
        candidates = await repo.find_active_entries(scope.child_id, date_from, date_to)

    if not candidates:
        return ChatReply(
            text=f"No encontré nada que quitar el {compose.format_date_es(date_from)}."
        )

    if len(candidates) == 1:
        entry = candidates[0]
        return ChatReply(
            text=compose.format_remove_question(entry),
            edit={
                "edit_id": _new_edit_id(),
                "chat_id": chat_id,
                "action": "remove",
                "entry_date": entry.entry_date.isoformat(),
                "entry_id": entry.pk,
            },
        )

    shortlist = candidates[:MAX_CANDIDATES]
    return ChatReply(
        text=compose.format_candidates(shortlist),
        edit={
            "edit_id": _new_edit_id(),
            "chat_id": chat_id,
            "action": "remove",
            "entry_date": date_from.isoformat(),
        },
        candidates=[(e.pk, candidate_label(e)) for e in shortlist],
    )


# --- Horario: quitar una regla y cambiar una franja ------------------------------------


def _fold(text: str) -> str:
    return " ".join(_strip_accents(text).lower().split())


async def prepare_remove_recurring(
    scope: Scope, intent: Intent, today: date, chat_id: int
) -> ChatReply:
    """«Quita la natación de los viernes»: retira un horario vigente entero.

    Cierra la asimetría de la 10.1 —se podía añadir una regla pero no quitarla— y sirve
    igual para un horario que entró por foto. No borra: `is_active=false` con
    `superseded_by`, como todo lo demás.
    """
    templates = await repo.active_schedules(scope.child_id, today)
    if not templates:
        return ChatReply(text=compose.NO_SCHEDULE_TEXT)

    hint = _fold(intent.target_entry_hint or intent.text or "")
    matching = [t for t in templates if hint and hint in _fold(t.name)] if hint else []
    if not matching:
        # La pista no casó con ningún nombre: se ofrecen todos, como en las bajas de agenda.
        matching = templates

    if len(matching) == 1:
        found = matching[0]
        return ChatReply(
            text=compose.format_remove_schedule_question(found.name),
            edit={
                "edit_id": _new_edit_id(),
                "chat_id": chat_id,
                "action": "remove_recurring",
                "schedule_id": found.pk,
            },
        )

    shortlist = matching[:MAX_CANDIDATES]
    return ChatReply(
        text=compose.format_schedule_candidates([t.name for t in shortlist]),
        edit={"edit_id": _new_edit_id(), "chat_id": chat_id, "action": "remove_recurring"},
        candidates=[(t.pk, t.name[:60]) for t in shortlist],
    )


async def prepare_edit_slot(scope: Scope, intent: Intent, today: date, chat_id: int) -> ChatReply:
    """«El martes de la Semana B cámbialo por evento»: una casilla del horario.

    Quién es «esa» casilla lo decide Python cruzando día, etiqueta de semana y una pista
    de texto contra los horarios vigentes; si queda más de una, elige el usuario con
    botones. El modelo solo dijo qué día y qué materia nueva.
    """
    subject = (intent.text or "").strip()
    if not subject:
        return ChatReply(text=compose.ASK_SLOT_SUBJECT_TEXT)
    days = [d for d in (intent.weekdays or []) if 1 <= d <= 7]
    if not days:
        return ChatReply(text=compose.ASK_SLOT_DAY_TEXT)

    templates = await repo.active_schedules(scope.child_id, today)
    if not templates:
        return ChatReply(text=compose.NO_SCHEDULE_TEXT)
    slots = await repo.slots_for_schedules([t.pk for t in templates])

    label = _fold(intent.week_label or "")
    hint = _fold(intent.target_entry_hint or "")
    found = [
        (template, slot)
        for template in templates
        for slot in slots[template.pk]
        if slot.weekday in days
        # Un horario semanal no tiene semanas: nombrar la B no debe descartarlo.
        and (not label or template.cycle_weeks == 1 or _fold(slot.week_label) == label)
        and (not hint or hint in _fold(template.name) or hint in _fold(slot.subject))
    ]
    if not found:
        return ChatReply(text=compose.NO_SLOT_FOUND_TEXT)

    if len(found) == 1:
        template, slot = found[0]
        place = compose.slot_place(slot, schedule=template.name, cycle_weeks=template.cycle_weeks)
        return ChatReply(
            text=compose.format_slot_change_question(place, slot.subject, subject),
            edit={
                "edit_id": _new_edit_id(),
                "chat_id": chat_id,
                "action": "edit_slot",
                "slot_id": slot.pk,
                "text": subject,
            },
        )

    shortlist = found[:MAX_CANDIDATES]
    return ChatReply(
        text=compose.format_slot_candidates(
            [
                compose.slot_place(slot, schedule=t.name, cycle_weeks=t.cycle_weeks)
                + f", que ahora dice «{slot.subject}»"
                for t, slot in shortlist
            ]
        ),
        edit={
            "edit_id": _new_edit_id(),
            "chat_id": chat_id,
            "action": "edit_slot",
            "text": subject,
        },
        candidates=[
            (
                slot.pk,
                compose.slot_button_label(slot, schedule=t.name, cycle_weeks=t.cycle_weeks),
            )
            for t, slot in shortlist
        ],
    )


# --- Recordatorios --------------------------------------------------------------------


async def prepare_reminder(scope: Scope, intent: Intent, today: date, chat_id: int) -> ChatReply:
    """Prepara el alta de un recordatorio. **No guarda nada**: eso lo hace el ✅."""
    text = (intent.text or "").strip()
    if not text:
        return ChatReply(text="¿De qué te aviso? Por ejemplo: «recuérdame a las 7 el disfraz».")
    if not intent.time_of_day:
        # El prompt tiene prohibido adivinar una hora ambigua, así que aquí se pregunta.
        return ChatReply(text=compose.ASK_REMINDER_TIME_TEXT)

    repeat = intent.repeat or "once"
    weekdays = reminders.format_weekdays(intent.weekdays or []) if repeat == "weekly" else ""
    if repeat == "weekly" and not weekdays:
        return ChatReply(text="¿Qué días? Por ejemplo: «los lunes y miércoles a las 7».")
    on_date = (intent.date_from or today) if repeat == "once" else None

    edit: dict[str, Any] = {
        "edit_id": _new_edit_id(),
        "chat_id": chat_id,
        "action": "add_reminder",
        "text": text,
        "time_of_day": intent.time_of_day,
        "repeat": repeat,
        "weekdays": weekdays,
        "on_date": on_date.isoformat() if on_date is not None else None,
        "only_school_days": bool(intent.only_school_days),
    }
    return ChatReply(
        text=compose.format_reminder_question(reminders.draft_from_edit(edit)), edit=edit
    )


async def list_reminders(scope: Scope) -> ChatReply:
    """Sin confirmación: solo lee."""
    return ChatReply(text=compose.format_reminders(await repo.reminders_of(scope.child_id)))


async def prepare_remove_reminder(scope: Scope, intent: Intent, chat_id: int) -> ChatReply:
    candidates = await repo.find_active_reminders(scope.child_id, intent.target_entry_hint)
    if not candidates and intent.target_entry_hint:
        # La pista no casó con nada: se ofrecen todos, como en las entradas.
        candidates = await repo.find_active_reminders(scope.child_id)
    if not candidates:
        return ChatReply(text=compose.NO_REMINDERS_TEXT)

    if len(candidates) == 1:
        found = candidates[0]
        return ChatReply(
            text=f"¿Quito el recordatorio {compose.describe_reminder(found)}?",
            edit={
                "edit_id": _new_edit_id(),
                "chat_id": chat_id,
                "action": "remove_reminder",
                "reminder_id": found.pk,
            },
        )

    shortlist = candidates[:MAX_CANDIDATES]
    return ChatReply(
        text=compose.format_reminder_candidates(shortlist),
        edit={"edit_id": _new_edit_id(), "chat_id": chat_id, "action": "remove_reminder"},
        candidates=[(r.pk, reminder_label(r)) for r in shortlist],
    )


def reminder_label(reminder: Reminder) -> str:
    """Etiqueta corta para un botón de Telegram (máximo 60 caracteres)."""
    label = compose.describe_reminder(reminder).replace("«", "").replace("»", "")
    return label[:57] + "…" if len(label) > 60 else label


async def query_subject(scope: Scope, intent: Intent, today: date) -> ChatReply:
    """«¿cuándo hay natación?»: se calcula con el horario, sin tocar el LLM otra vez."""
    subject = (intent.subject or intent.text or "").strip()
    if not subject:
        return ChatReply(text="¿De qué materia? Por ejemplo: «¿cuándo hay natación?».")
    found = await schedule_service.find_subject(scope, subject, today, count=3)
    if not found and not await repo.active_schedules(scope.child_id, today):
        return ChatReply(text=compose.NO_SCHEDULE_TEXT)
    return ChatReply(text=compose.format_next_occurrences(subject, found))


async def dispatch(scope: Scope, intent: Intent, *, today: date, chat_id: int) -> ChatReply:
    """Intención ya clasificada → respuesta. `confirm`/`reject`/`correct_pending` los
    resuelve el handler porque necesitan el estado pendiente y los proveedores."""
    if intent.action == "query_range":
        return await query_range(scope, intent, today)
    if intent.action == "query_subject":
        return await query_subject(scope, intent, today)
    if intent.action == "add_entry":
        return await prepare_add(intent, today, chat_id)
    if intent.action == "add_recurring":
        return await prepare_recurring(intent, chat_id)
    if intent.action == "remove_recurring":
        return await prepare_remove_recurring(scope, intent, today, chat_id)
    if intent.action == "edit_slot":
        return await prepare_edit_slot(scope, intent, today, chat_id)
    if intent.action == "remove_entry":
        return await prepare_remove(scope, intent, today, chat_id)
    if intent.action == "add_reminder":
        return await prepare_reminder(scope, intent, today, chat_id)
    if intent.action == "list_reminders":
        return await list_reminders(scope)
    if intent.action == "remove_reminder":
        return await prepare_remove_reminder(scope, intent, chat_id)
    if intent.action == "help":
        return ChatReply(text=compose.HELP_TEXT)
    return ChatReply(
        text=(
            "No te entendí. Prueba con «¿qué hay mañana?», «agrega que el martes lleva "
            "disfraz» o «quita lo del jueves». /ayuda para ver todo."
        )
    )

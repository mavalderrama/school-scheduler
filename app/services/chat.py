"""Texto libre (flujo 7.2): clasificar intención y despachar a la lógica determinista.

El modelo solo clasifica y extrae datos; todo lo que toca la DB lo hace Python. No habla
con Telegram: devuelve un `ChatReply` y el handler decide qué teclado ponerle.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.config import Settings
from app.db import repo
from app.db.models import AgendaEntry
from app.llm import compose
from app.llm.prompting import format_history, weekday_es
from app.llm.provider import LLMError, LLMProviders
from app.llm.schemas import ChatTurn, Intent
from app.log import get_logger
from app.services import cache
from app.services import schedule as schedule_service
from app.services.confirm import PendingEdit, PendingStore

log = get_logger(__name__)

HISTORY_TURNS = 6
MAX_CANDIDATES = 6


@dataclass
class ChatReply:
    """Respuesta lista para enviar; `edit` y `candidates` piden teclado al handler."""

    text: str
    edit: PendingEdit | None = None
    candidates: list[tuple[int, str]] | None = None


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
                provider=attempt.provider,
                ok=attempt.ok,
                error=attempt.error,
                usage=attempt.usage,
                duration_ms=attempt.duration_ms,
            )
        raise
    for attempt in run.attempts:
        await repo.log_llm_call(
            task="intent",
            provider=attempt.provider,
            ok=attempt.ok,
            error=attempt.error,
            usage=attempt.usage,
            duration_ms=attempt.duration_ms,
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


async def query_range(intent: Intent, today: date, *, country: str = "CO") -> ChatReply:
    date_from = intent.date_from or today
    date_to = intent.date_to or date_from
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    entries = await repo.active_entries(date_from, date_to)
    if date_from == date_to:
        title = f"📚 <b>{compose.format_date_es(date_from)}</b>:"
        # Un solo día lleva también la clase del horario, que es lo que más se pregunta.
        slots = await schedule_service.resolve_day(date_from, country=country)
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


async def prepare_add(intent: Intent, today: date, store: PendingStore, chat_id: int) -> ChatReply:
    if intent.date_from is None or not intent.text:
        return ChatReply(
            text="¿Para qué día y qué agrego? Por ejemplo: «agrega que el martes lleva disfraz»."
        )
    edit = PendingEdit(
        edit_id=store.new_edit_id(),
        chat_id=chat_id,
        action="add",
        entry_date=intent.date_from,
        kind=intent.kind or "note",
        text=intent.text.strip(),
    )
    return ChatReply(
        text=compose.format_add_question(edit.entry_date, edit.kind or "note", edit.text or ""),
        edit=edit,
    )


async def prepare_remove(
    intent: Intent, today: date, store: PendingStore, chat_id: int
) -> ChatReply:
    date_from = intent.date_from or today
    date_to = intent.date_to or date_from
    candidates = await repo.find_active_entries(date_from, date_to, intent.target_entry_hint)
    if not candidates and intent.target_entry_hint:
        # La pista no casó con nada: reintenta sin filtrar por texto.
        candidates = await repo.find_active_entries(date_from, date_to)

    if not candidates:
        return ChatReply(
            text=f"No encontré nada que quitar el {compose.format_date_es(date_from)}."
        )

    if len(candidates) == 1:
        entry = candidates[0]
        edit = PendingEdit(
            edit_id=store.new_edit_id(),
            chat_id=chat_id,
            action="remove",
            entry_date=entry.entry_date,
            entry_id=entry.pk,
        )
        return ChatReply(text=compose.format_remove_question(entry), edit=edit)

    shortlist = candidates[:MAX_CANDIDATES]
    edit = PendingEdit(
        edit_id=store.new_edit_id(),
        chat_id=chat_id,
        action="remove",
        entry_date=date_from,
    )
    return ChatReply(
        text=compose.format_candidates(shortlist),
        edit=edit,
        candidates=[(e.pk, candidate_label(e)) for e in shortlist],
    )


async def query_subject(intent: Intent, today: date, *, country: str = "CO") -> ChatReply:
    """«¿cuándo hay natación?»: se calcula con el horario, sin tocar el LLM otra vez."""
    subject = (intent.subject or intent.text or "").strip()
    if not subject:
        return ChatReply(text="¿De qué materia? Por ejemplo: «¿cuándo hay natación?».")
    found = await schedule_service.find_subject(subject, today, country=country, count=3)
    if not found and not await repo.active_schedules(today):
        return ChatReply(text=compose.NO_SCHEDULE_TEXT)
    return ChatReply(text=compose.format_next_occurrences(subject, found))


async def dispatch(
    intent: Intent, *, today: date, store: PendingStore, chat_id: int, country: str = "CO"
) -> ChatReply:
    """Intención ya clasificada → respuesta. `confirm`/`reject`/`correct_pending` los
    resuelve el handler porque necesitan el estado pendiente y los proveedores."""
    if intent.action == "query_range":
        return await query_range(intent, today, country=country)
    if intent.action == "query_subject":
        return await query_subject(intent, today, country=country)
    if intent.action == "add_entry":
        return await prepare_add(intent, today, store, chat_id)
    if intent.action == "remove_entry":
        return await prepare_remove(intent, today, store, chat_id)
    if intent.action == "help":
        return ChatReply(text=compose.HELP_TEXT)
    return ChatReply(
        text=(
            "No te entendí. Prueba con «¿qué hay mañana?», «agrega que el martes lleva "
            "disfraz» o «quita lo del jueves». /ayuda para ver todo."
        )
    )

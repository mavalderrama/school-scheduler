"""Notificación diaria (7.3) y chequeo de huecos (7.4). Sin LLM: funciona con todo apagado.

No habla con Telegram directamente: recibe un `Sender` (chat_id, texto HTML) para poder
probarlo y para que el job y el comando `/manana` compartan la misma lógica. Idempotencia
vía `notifications_log`: un solo envío `ok` por (kind, target_date, chat_id), reforzado por
el unique parcial `notif_log_ok_unique`.
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from app.config import Settings
from app.db import repo
from app.db.models import AgendaEntry, NotificationKind
from app.llm.compose import KIND_LABELS, format_date_es
from app.llm.prompting import weekday_es
from app.log import get_logger

log = get_logger(__name__)

Sender = Callable[[int, str], Awaitable[None]]
"""Envía `texto` (HTML de Telegram) al `chat_id`. Lanza si falla."""

DAILY_KINDS = (NotificationKind.DAILY, NotificationKind.NUDGE_EMPTY)
KIND_ORDER = ("bring", "homework", "event", "note")


@dataclass(frozen=True)
class Outcome:
    chat_id: int
    kind: NotificationKind
    sent: bool
    skipped: bool = False
    error: str | None = None


# --- Fechas ----------------------------------------------------------------------------


def daily_target(today: date, skip_weekend: bool) -> date | None:
    """Mañana, o None si cae en fin de semana y SKIP_WEEKEND está activo."""
    target = today + timedelta(days=1)
    if skip_weekend and target.weekday() >= 5:
        return None
    return target


def next_week_days(today: date) -> list[date]:
    """Lunes a viernes de la semana siguiente a la de `today`."""
    next_monday = today + timedelta(days=7 - today.weekday())
    return [next_monday + timedelta(days=i) for i in range(5)]


# --- Textos ------------------------------------------------------------------------------


def format_daily(target: date, entries: Sequence[AgendaEntry]) -> str:
    """Formato de 7.3: una línea por tipo, ítems separados por coma."""
    lines = [f"📚 Mañana, {format_date_es(target)}"]
    for kind in KIND_ORDER:
        texts = [html.escape(e.text) for e in entries if e.kind == kind]
        if texts:
            emoji, label = KIND_LABELS[kind]
            lines.append(f"{emoji} {label}: {', '.join(texts)}")
    return "\n".join(lines)


def format_nudge(target: date) -> str:
    return f"📚 No tengo agenda para mañana ({format_date_es(target)}). ¿Me mandan foto?"


def format_gaps(gaps: Sequence[date]) -> str:
    days = ", ".join(f"{weekday_es(d)} {d.day}" for d in gaps)
    return f"📅 Para la semana que viene no tengo nada para: {days}. ¿Me mandan foto de la agenda?"


async def build_daily_message(target: date) -> tuple[NotificationKind, str]:
    entries = await repo.active_entries(target, target)
    if entries:
        return NotificationKind.DAILY, format_daily(target, entries)
    return NotificationKind.NUDGE_EMPTY, format_nudge(target)


# --- Envío -----------------------------------------------------------------------------


async def _send_to_chats(
    send: Sender,
    chat_ids: Sequence[int],
    *,
    kind: NotificationKind,
    idempotency_kinds: Sequence[NotificationKind],
    target: date,
    text: str,
) -> list[Outcome]:
    outcomes: list[Outcome] = []
    for chat_id in chat_ids:
        if await repo.notification_sent_ok(idempotency_kinds, target, chat_id):
            log.info("notify_skipped", kind=kind, target=target.isoformat(), chat_id=chat_id)
            outcomes.append(Outcome(chat_id, kind, sent=False, skipped=True))
            continue
        error: str | None = None
        try:
            await send(chat_id, text)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.exception("notify_failed", kind=kind, chat_id=chat_id)
        await repo.log_notification(kind, target, chat_id, ok=error is None, error=error)
        log.info(
            "notify_sent", kind=kind, target=target.isoformat(), chat_id=chat_id, ok=error is None
        )
        outcomes.append(Outcome(chat_id, kind, sent=error is None, error=error))
    return outcomes


async def send_daily(send: Sender, settings: Settings, today: date) -> list[Outcome]:
    """Notificación de las 19:00: lo de mañana, o el aviso de agenda vacía."""
    target = daily_target(today, settings.skip_weekend)
    if target is None:
        log.info("notify_weekend_skip", today=today.isoformat())
        return []
    kind, text = await build_daily_message(target)
    return await _send_to_chats(
        send,
        settings.notify_chat_ids,
        kind=kind,
        idempotency_kinds=DAILY_KINDS,
        target=target,
        text=text,
    )


async def send_gap_check(send: Sender, settings: Settings, today: date) -> list[Outcome]:
    """Domingo: días hábiles de la próxima semana sin entradas vigentes."""
    days = next_week_days(today)
    covered = await repo.active_dates(days[0], days[-1])
    gaps = [d for d in days if d not in covered]
    if not gaps:
        log.info("gap_check_clean", week_of=days[0].isoformat())
        return []
    return await _send_to_chats(
        send,
        settings.notify_chat_ids,
        kind=NotificationKind.GAP_CHECK,
        idempotency_kinds=(NotificationKind.GAP_CHECK,),
        target=days[0],
        text=format_gaps(gaps),
    )

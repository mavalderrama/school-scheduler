"""Notificación diaria (7.3) y chequeo de huecos (7.4). Sin LLM: funciona con todo apagado.

No habla con Telegram directamente: recibe un `Sender` (chat_id, texto HTML) para poder
probarlo y para que el job y el comando `/manana` compartan la misma lógica. Idempotencia
vía `notifications_log`: un solo envío `ok` por (kind, target_date, chat_id), reforzado por
el unique parcial `notif_log_ok_unique`.
"""

from __future__ import annotations

import html
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.config import Settings
from app.db import repo
from app.db.models import AgendaEntry, NotificationKind, Reminder
from app.llm.compose import KIND_LABELS, format_date_es, format_reminder, slot_lines
from app.llm.prompting import weekday_es
from app.log import get_logger
from app.services import ha, schoolcal
from app.services import schedule as schedule_service
from app.services.schedule import SlotResult
from app.services.scope import Scope

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


def daily_target(
    today: date,
    *,
    exceptions: dict[date, tuple[str, str]],
    country: str = "CO",
    skip_non_school: bool = True,
) -> date | None:
    """Mañana, **solo si mañana hay colegio**. Si no, None y esta tarde no se avisa nada.

    La notificación recuerda el próximo día de clase la tarde anterior. Antes solo saltaba
    los fines de semana, así que la víspera de un festivo mandaba el aviso de un día en el
    que no hay colegio. El caso «lunes festivo → se avisa el lunes por la noche, del martes»
    sale solo de esta regla, sin lógica extra: esa tarde, mañana sí es lectivo.
    """
    target = today + timedelta(days=1)
    if skip_non_school and not schoolcal.is_school_day(
        target, exceptions=exceptions, country=country
    ):
        return None
    return target


def next_week_days(today: date) -> list[date]:
    """Lunes a viernes de la semana siguiente a la de `today`."""
    next_monday = today + timedelta(days=7 - today.weekday())
    return [next_monday + timedelta(days=i) for i in range(5)]


def next_week_school_days(
    today: date, *, exceptions: dict[date, tuple[str, str]], country: str = "CO"
) -> list[date]:
    """Los días de la semana que viene en los que **sí** hay colegio.

    Reclamar la agenda de un festivo es ruido: si el lunes no hay clase, no es un hueco.
    """
    return [
        day
        for day in next_week_days(today)
        if schoolcal.is_school_day(day, exceptions=exceptions, country=country)
    ]


# --- Textos ------------------------------------------------------------------------------


def format_daily(
    target: date, entries: Sequence[AgendaEntry], slots: Sequence[SlotResult] = ()
) -> str:
    """Formato de 7.3, con las clases del horario primero (una línea por horario)."""
    lines = [f"📚 Mañana, {format_date_es(target)}"]
    lines.extend(slot_lines(slots))
    for kind in KIND_ORDER:
        texts = [html.escape(e.text) for e in entries if e.kind == kind]
        if texts:
            emoji, label = KIND_LABELS[kind]
            lines.append(f"{emoji} {label}: {', '.join(texts)}")
    return "\n".join(lines)


def _plain(text: str) -> str:
    """El HTML de Telegram no sirve en una notificación de Home Assistant."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def format_nudge(target: date) -> str:
    return f"📚 No tengo agenda para mañana ({format_date_es(target)}). ¿Me mandan foto?"


def format_gaps(gaps: Sequence[date]) -> str:
    days = ", ".join(f"{weekday_es(d)} {d.day}" for d in gaps)
    return f"📅 Para la semana que viene no tengo nada para: {days}. ¿Me mandan foto de la agenda?"


async def build_daily_message(
    scope: Scope, target: date, *, use_schedule: bool = True
) -> tuple[NotificationKind, str]:
    """Lo de mañana. El horario cuenta como contenido: si hay clase (o si mañana es festivo)
    hay algo que decir, y el aviso de agenda vacía deja de ser la única opción."""
    entries = await repo.active_entries(scope.child_id, target, target)
    slots = await schedule_service.resolve_day(scope, target) if use_schedule else []
    if entries or slots:
        return NotificationKind.DAILY, format_daily(target, entries, slots)
    return NotificationKind.NUDGE_EMPTY, format_nudge(target)


# --- Envío -----------------------------------------------------------------------------


async def _attempt(
    send: Sender,
    chat_id: int,
    text: str,
    *,
    settings: Settings | None,
    kind: NotificationKind,
) -> str | None:
    """Intenta el envío. Devuelve `None` si salió bien, o el error ya formateado.

    El plan B de Home Assistant vive aquí y no en cada llamador: es la rama de un envío que
    **ya falló**, y el resultado se anota junto al error para que `/estado` no diga que el
    aviso no llegó cuando sí llegó por la otra vía.
    """
    try:
        await send(chat_id, text)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.exception("notify_failed", kind=kind, chat_id=chat_id)
        if settings is not None and ha.configured(settings):
            relayed = await ha.notify(settings, _plain(text))
            error += " | HA: enviado" if relayed else " | HA: también falló"
        return error
    return None


async def _send_to_chats(
    send: Sender,
    chat_ids: Sequence[int],
    *,
    kind: NotificationKind,
    idempotency_kinds: Sequence[NotificationKind],
    target: date,
    text: str,
    settings: Settings | None = None,
    child_id: int | None = None,
    reminder_id: int | None = None,
) -> list[Outcome]:
    outcomes: list[Outcome] = []
    for chat_id in chat_ids:
        if await repo.notification_sent_ok(
            idempotency_kinds, target, chat_id, child_id, reminder_id
        ):
            log.info("notify_skipped", kind=kind, target=target.isoformat(), chat_id=chat_id)
            outcomes.append(Outcome(chat_id, kind, sent=False, skipped=True))
            continue
        error = await _attempt(send, chat_id, text, settings=settings, kind=kind)
        await repo.log_notification(
            kind,
            target,
            chat_id,
            ok=error is None,
            error=error,
            child_id=child_id,
            reminder_id=reminder_id,
        )
        log.info(
            "notify_sent", kind=kind, target=target.isoformat(), chat_id=chat_id, ok=error is None
        )
        outcomes.append(Outcome(chat_id, kind, sent=error is None, error=error))
    return outcomes


async def send_daily(
    send: Sender, settings: Settings, today: date, *, scope: Scope
) -> list[Outcome]:
    """Notificación diaria de **un niño**: lo de mañana, o el aviso de agenda vacía.

    El bucle por niños lo hace el planificador, no esto: aquí el ámbito ya viene resuelto,
    con su colegio, su país y su zona horaria.
    """
    exceptions = await repo.calendar_exceptions(scope.school_id)
    target = daily_target(
        today,
        exceptions=exceptions,
        country=scope.country,
        skip_non_school=settings.skip_weekend,
    )
    if target is None:
        info = schoolcal.day_info(
            today + timedelta(days=1), exceptions=exceptions, country=scope.country
        )
        log.info(
            "notify_skip_non_school_day",
            child=scope.child_id,
            today=today.isoformat(),
            reason=info.reason,
        )
        return []
    kind, text = await build_daily_message(scope, target, use_schedule=settings.schedule_enabled)
    chats = [scope.chat_id] if scope.chat_id is not None else []
    return await _send_to_chats(
        send,
        chats,
        kind=kind,
        idempotency_kinds=DAILY_KINDS,
        target=target,
        text=text,
        settings=settings,
        child_id=scope.child_id,
    )


async def send_gap_check(
    send: Sender, settings: Settings, today: date, *, scope: Scope
) -> list[Outcome]:
    """Domingo: días hábiles de la próxima semana sin entradas vigentes de ese niño."""
    week = next_week_days(today)
    exceptions = await repo.calendar_exceptions(scope.school_id)
    days = next_week_school_days(today, exceptions=exceptions, country=scope.country)
    if not days:
        log.info("gap_check_no_school_week", week_of=week[0].isoformat())
        return []
    covered = await repo.active_dates(scope.child_id, days[0], days[-1])
    gaps = [d for d in days if d not in covered]
    if not gaps:
        log.info("gap_check_clean", week_of=week[0].isoformat())
        return []
    chats = [scope.chat_id] if scope.chat_id is not None else []
    return await _send_to_chats(
        send,
        chats,
        kind=NotificationKind.GAP_CHECK,
        idempotency_kinds=(NotificationKind.GAP_CHECK,),
        target=week[0],
        text=format_gaps(gaps),
        settings=settings,
        child_id=scope.child_id,
    )


# --- Recordatorios ------------------------------------------------------------------------


async def send_reminder(
    send: Sender,
    settings: Settings,
    reminder: Reminder,
    *,
    scope: Scope,
    fire_at: datetime,
    late: bool = False,
) -> list[Outcome]:
    """Manda un recordatorio al chat donde se pidió.

    Pasa por `_send_to_chats` como todo lo demás para heredar el plan B de Home Assistant y
    la fila de auditoría. La fecha objetivo es la **local** de la ocurrencia: un recordatorio
    suena como mucho una vez al día, así que con esa fecha y el id basta para no repetirlo.
    """
    return await _send_to_chats(
        send,
        [reminder.chat_id],
        kind=NotificationKind.REMINDER,
        idempotency_kinds=(NotificationKind.REMINDER,),
        target=fire_at.astimezone(scope.zoneinfo).date(),
        text=format_reminder(reminder.text, late=late),
        settings=settings,
        child_id=scope.child_id,
        reminder_id=reminder.pk,
    )

"""Tareas programadas (APScheduler dentro del proceso del bot). No usan LLM.

- `daily_notify`: todos los días a DAILY_NOTIFY_TIME, lo de mañana (o el aviso de vacío).
- `gap_check`: domingos a GAP_CHECK_TIME, días hábiles de la próxima semana sin entradas.

`misfire_grace_time` alto y `coalesce` para que un reinicio poco después de la hora no
pierda el envío; la idempotencia la garantiza `notifications_log`.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from asgiref.sync import ThreadSensitiveContext
from django.utils import timezone

from app.config import Settings
from app.db import repo
from app.db.models import NotificationKind
from app.graph.runner import GraphRunner
from app.llm.provider import LLMProviders
from app.log import get_logger
from app.services import notify, reminders, scope

log = get_logger(__name__)

MISFIRE_GRACE_S = 3600
# El barrido de recordatorios NO usa la gracia de una hora de los demás jobs: aquí llegar
# tarde no es inofensivo. Si el barrido se pierde, la política de retraso la decide
# `reminders.due_action` mirando la ocurrencia, no APScheduler.
REMINDER_SWEEP_S = 60
REMINDER_GRACE_S = 60
REMINDER_BATCH = 50
RETENTION_HOUR, RETENTION_MINUTE = 4, 17  # de madrugada, lejos de las notificaciones
RETRY_BATCH = 3


def db_job[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Da a cada ejecución del job su propio hilo/conexión y limpia al terminar.

    Mismo bracket que `DjangoDBMiddleware`; usarlo en todo job que toque la DB.
    """

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        async with ThreadSensitiveContext():  # type: ignore[no-untyped-call]
            await repo.close_old()
            try:
                return await fn(*args, **kwargs)
            finally:
                await repo.close_old()

    return wrapper


def telegram_sender(bot: Bot) -> notify.Sender:
    async def send(chat_id: int, text: str) -> None:
        await bot.send_message(chat_id, text)

    return send


@db_job
async def daily_notify_job(bot: Bot, settings: Settings) -> None:
    """Un aviso por niño. Cada uno con la zona horaria y el calendario de **su** colegio."""
    sent = total = 0
    for child in await repo.active_children():
        sc = scope.of(child)
        today = datetime.now(sc.zoneinfo).date()
        outcomes = await notify.send_daily(telegram_sender(bot), settings, today, scope=sc)
        sent += sum(o.sent for o in outcomes)
        total += len(outcomes)
    log.info("daily_notify_done", sent=sent, total=total)


@db_job
async def gap_check_job(bot: Bot, settings: Settings) -> None:
    sent = total = 0
    for child in await repo.active_children():
        sc = scope.of(child)
        today = datetime.now(sc.zoneinfo).date()
        outcomes = await notify.send_gap_check(telegram_sender(bot), settings, today, scope=sc)
        sent += sum(o.sent for o in outcomes)
        total += len(outcomes)
    log.info("gap_check_done", sent=sent, total=total)


@db_job
async def retry_photos_job(
    bot: Bot, settings: Settings, providers: LLMProviders, runner: GraphRunner
) -> None:
    """Reintenta las fotos que quedaron sin leer (cuota agotada) y abandona las muy viejas.

    Sobrevive a reinicios porque el estado vive en `sources`, no en memoria.
    """
    now = timezone.now()
    give_up_before = now - timedelta(hours=settings.retry_give_up_hours)

    for source in await repo.abandon_stale_photos(give_up_before):
        log.warning("photo_abandoned", source_id=source.pk)
        if source.chat_id is not None:
            await bot.send_message(
                source.chat_id,
                "⚠️ No conseguí leer una foto que mandaste hace rato. ¿Me la reenvías?",
            )

    older_than = now - timedelta(minutes=settings.llm_retry_after_min)
    stale = await repo.photos_awaiting_extraction(
        older_than, give_up_before=give_up_before, limit=RETRY_BATCH
    )
    if not stale:
        return
    from app.bot import actions  # import tardío: evita un ciclo con los handlers

    done = 0
    for source in stale:
        if await actions.resume_photo(bot, source, runner):
            done += 1
    log.info("retry_photos_done", candidates=len(stale), recovered=done)


@db_job
async def reminders_job(bot: Bot, settings: Settings) -> None:
    """Dispara los recordatorios vencidos. Un barrido, no un job por recordatorio.

    El estado vive en `reminders.next_fire_at`, así que un reinicio no pierde nada y no hay
    que reprogramar nada al arrancar.

    **Se reserva antes de enviar** (`claim_reminder`): la entrega es *como mucho una vez*. Si
    el proceso muere entre la reserva y el envío, ese aviso se pierde; es preferible a que un
    reinicio en el momento justo lo mande dos veces.
    """
    now = timezone.now()
    send = telegram_sender(bot)
    exceptions_by_school: dict[int, dict[date, tuple[str, str]]] = {}
    fired = 0

    for reminder in await repo.due_reminders(now, limit=REMINDER_BATCH):
        due_at = reminder.next_fire_at
        if due_at is None:  # pragma: no cover - lo excluye la propia consulta
            continue
        sc = scope.of(reminder.child)
        if sc.school_id not in exceptions_by_school:
            exceptions_by_school[sc.school_id] = await repo.calendar_exceptions(sc.school_id)
        exceptions = exceptions_by_school[sc.school_id]

        following = reminders.next_occurrence(
            repeat=reminder.repeat,
            weekdays=reminder.weekdays,
            time_of_day=reminder.time_of_day,
            on_date=reminder.on_date,
            only_school_days=reminder.only_school_days,
            after=now,
            tz=sc.zoneinfo,
            exceptions=exceptions,
            country=sc.country,
        )
        # Reservar primero: si otro barrido se adelantó, este no manda nada.
        if not await repo.claim_reminder(
            reminder.pk, expected=due_at, next_fire_at=following, fired_at=now
        ):
            continue

        what = reminders.due_action(reminder.repeat, due_at, now)
        if what == "skip":
            log.info("reminder_skipped", reminder_id=reminder.pk, due_at=due_at.isoformat())
            await repo.log_notification(
                NotificationKind.REMINDER,
                due_at.astimezone(sc.zoneinfo).date(),
                reminder.chat_id,
                ok=False,
                error="fuera de plazo: el bot estaba caído a esa hora",
                child_id=sc.child_id,
                reminder_id=reminder.pk,
            )
            continue

        await notify.send_reminder(
            send, settings, reminder, scope=sc, fire_at=due_at, late=what == "late"
        )
        fired += 1

    if fired:
        log.info("reminders_done", fired=fired)


@db_job
async def purge_photos_job(settings: Settings) -> None:
    """Retención: borra el archivo de fotos ya resueltas y antiguas. La fila se conserva."""
    before = timezone.now() - timedelta(days=settings.photo_retention_days)
    purged = 0
    for source in await repo.photos_to_purge(before):
        if source.local_path:
            path = Path(source.local_path)
            try:
                await asyncio.to_thread(path.unlink, True)
            except OSError as exc:
                log.warning("purge_photo_failed", source_id=source.pk, error=str(exc))
                continue
        await repo.clear_local_path(source.pk)
        purged += 1
    log.info("purge_photos_done", purged=purged, retention_days=settings.photo_retention_days)

    # Misma idea con las trazas: se va el material pesado, la fila y las métricas se quedan.
    traces_before = timezone.now() - timedelta(days=settings.llm_trace_retention_days)
    cleared = await repo.purge_llm_traces(traces_before)
    log.info("purge_traces_done", cleared=cleared, retention_days=settings.llm_trace_retention_days)


def _hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def build_scheduler(settings: Settings) -> AsyncIOScheduler:
    return AsyncIOScheduler(timezone=settings.zoneinfo)


def register_jobs(
    scheduler: AsyncIOScheduler,
    settings: Settings,
    bot: Bot,
    providers: LLMProviders,
    runner: GraphRunner,
) -> None:
    tz = settings.zoneinfo
    daily_h, daily_m = _hhmm(settings.daily_notify_time)
    gap_h, gap_m = _hhmm(settings.gap_check_time)
    scheduler.add_job(
        daily_notify_job,
        CronTrigger(hour=daily_h, minute=daily_m, timezone=tz),
        args=[bot, settings],
        id="daily_notify",
        replace_existing=True,
        misfire_grace_time=MISFIRE_GRACE_S,
        coalesce=True,
    )
    scheduler.add_job(
        gap_check_job,
        CronTrigger(day_of_week="sun", hour=gap_h, minute=gap_m, timezone=tz),
        args=[bot, settings],
        id="gap_check",
        replace_existing=True,
        misfire_grace_time=MISFIRE_GRACE_S,
        coalesce=True,
    )
    scheduler.add_job(
        retry_photos_job,
        IntervalTrigger(minutes=max(settings.llm_retry_after_min, 5), timezone=tz),
        args=[bot, settings, providers, runner],
        id="retry_photos",
        replace_existing=True,
        misfire_grace_time=MISFIRE_GRACE_S,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        reminders_job,
        IntervalTrigger(seconds=REMINDER_SWEEP_S, timezone=tz),
        args=[bot, settings],
        id="reminders",
        replace_existing=True,
        misfire_grace_time=REMINDER_GRACE_S,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        purge_photos_job,
        CronTrigger(hour=RETENTION_HOUR, minute=RETENTION_MINUTE, timezone=tz),
        args=[settings],
        id="purge_photos",
        replace_existing=True,
        misfire_grace_time=MISFIRE_GRACE_S,
        coalesce=True,
    )
    log.info(
        "scheduler_jobs_registered",
        count=5,
        daily_notify_time=settings.daily_notify_time,
        gap_check_time=settings.gap_check_time,
        skip_weekend=settings.skip_weekend,
        notify_chats=len(settings.notify_chat_ids),
        retry_every_min=max(settings.llm_retry_after_min, 5),
        photo_retention_days=settings.photo_retention_days,
    )

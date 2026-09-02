"""Tareas programadas (APScheduler dentro del proceso del bot). No usan LLM.

- `daily_notify`: todos los días a DAILY_NOTIFY_TIME, lo de mañana (o el aviso de vacío).
- `gap_check`: domingos a GAP_CHECK_TIME, días hábiles de la próxima semana sin entradas.

`misfire_grace_time` alto y `coalesce` para que un reinicio poco después de la hora no
pierda el envío; la idempotencia la garantiza `notifications_log`.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from datetime import datetime

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from asgiref.sync import ThreadSensitiveContext

from app.config import Settings
from app.db import repo
from app.log import get_logger
from app.services import notify

log = get_logger(__name__)

MISFIRE_GRACE_S = 3600


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
    today = datetime.now(settings.zoneinfo).date()
    outcomes = await notify.send_daily(telegram_sender(bot), settings, today)
    log.info("daily_notify_done", sent=sum(o.sent for o in outcomes), total=len(outcomes))


@db_job
async def gap_check_job(bot: Bot, settings: Settings) -> None:
    today = datetime.now(settings.zoneinfo).date()
    outcomes = await notify.send_gap_check(telegram_sender(bot), settings, today)
    log.info("gap_check_done", sent=sum(o.sent for o in outcomes), total=len(outcomes))


def _hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def build_scheduler(settings: Settings) -> AsyncIOScheduler:
    return AsyncIOScheduler(timezone=settings.zoneinfo)


def register_jobs(scheduler: AsyncIOScheduler, settings: Settings, bot: Bot) -> None:
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
    log.info(
        "scheduler_jobs_registered",
        count=2,
        daily_notify_time=settings.daily_notify_time,
        gap_check_time=settings.gap_check_time,
        skip_weekend=settings.skip_weekend,
        notify_chats=len(settings.notify_chat_ids),
    )

"""Tareas programadas (APScheduler dentro del proceso del bot). Los jobs llegan en Fase 2."""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import Settings
from app.log import get_logger

log = get_logger(__name__)


def build_scheduler(settings: Settings) -> AsyncIOScheduler:
    return AsyncIOScheduler(timezone=settings.zoneinfo)


def register_jobs(scheduler: AsyncIOScheduler, settings: Settings) -> None:
    """Fase 2: notificación diaria y chequeo de huecos. Por ahora no registra nada."""
    log.info(
        "scheduler_jobs_registered",
        count=0,
        daily_notify_time=settings.daily_notify_time,
        gap_check_time=settings.gap_check_time,
    )

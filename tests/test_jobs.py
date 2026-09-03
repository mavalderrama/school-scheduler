"""Registro de jobs en APScheduler: horas, zona horaria y día del chequeo de huecos."""

from __future__ import annotations

from typing import Any, cast

from apscheduler.triggers.cron import CronTrigger

from app.config import Settings
from app.graph.runner import GraphRunner
from app.scheduler.jobs import build_scheduler, register_jobs
from tests.test_ingest import providers
from tests.test_provider import FakeProvider


def _field(trigger: CronTrigger, name: str) -> str:
    return next(str(f) for f in trigger.fields if f.name == name)


def test_register_jobs_uses_configured_times_and_timezone(settings: Settings) -> None:
    settings = settings.model_copy(update={"daily_notify_time": "18:30", "gap_check_time": "17:05"})
    scheduler = build_scheduler(settings)
    bot: Any = object()
    runner = cast(GraphRunner, object())  # los jobs solo lo pasan adelante
    register_jobs(scheduler, settings, bot, providers(FakeProvider("a")), runner)

    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {"daily_notify", "gap_check", "retry_photos", "purge_photos"}

    daily = jobs["daily_notify"].trigger
    assert isinstance(daily, CronTrigger)
    assert (_field(daily, "hour"), _field(daily, "minute")) == ("18", "30")
    assert _field(daily, "day_of_week") == "*"
    assert str(daily.timezone) == "America/Bogota"

    gap = jobs["gap_check"].trigger
    assert isinstance(gap, CronTrigger)
    assert (_field(gap, "day_of_week"), _field(gap, "hour"), _field(gap, "minute")) == (
        "sun",
        "17",
        "5",
    )
    assert jobs["daily_notify"].args == (bot, settings)
    assert jobs["daily_notify"].misfire_grace_time == 3600

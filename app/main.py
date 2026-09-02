"""Arranque: configuración, Django, base de datos, proveedores de LLM, scheduler, admin y bot.

Un solo proceso y un solo event loop: polling de aiogram, APScheduler y uvicorn (admin de
Django) corren como tareas del mismo `TaskGroup`. Las señales las gestiona `run()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

from app.django_bootstrap import setup_django

setup_django()  # antes de importar cualquier módulo con modelos

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402

from app.bot.handlers import callbacks, commands, photo, text  # noqa: E402
from app.bot.middlewares.auth import AuthMiddleware  # noqa: E402
from app.bot.middlewares.db import DjangoDBMiddleware  # noqa: E402
from app.config import (  # noqa: E402
    ConfigError,
    Settings,
    harden_environment,
    load_settings,
    startup_warnings,
)
from app.db import repo  # noqa: E402
from app.llm.provider import build_providers  # noqa: E402
from app.log import configure_logging, get_logger  # noqa: E402
from app.scheduler.jobs import build_scheduler, register_jobs  # noqa: E402
from app.services.confirm import PendingStore  # noqa: E402
from app.web import admin_url, build_admin_server  # noqa: E402

log = get_logger(__name__)

_SIGNALS = (signal.SIGINT, signal.SIGTERM)


async def run(settings: Settings) -> None:
    for warning in startup_warnings(settings):
        log.warning("config_warning", detail=warning)
    harden_environment(settings)
    settings.photos_dir.mkdir(parents=True, exist_ok=True)

    await repo.check_connection()
    log.info("db_connected")

    if settings.django_superuser_username and settings.django_superuser_password:
        created = await repo.ensure_superuser(
            settings.django_superuser_username,
            settings.django_superuser_password,
            settings.django_superuser_email,
        )
        log.info("superuser_ready", username=settings.django_superuser_username, created=created)

    providers = build_providers(settings)
    log.info("llm_providers", vision=providers.vision.name, text=providers.text.name)

    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(settings=settings, providers=providers, pending=PendingStore())
    dp.update.outer_middleware(AuthMiddleware(settings.allowed_user_ids, settings.allowed_chat_ids))
    dp.update.outer_middleware(DjangoDBMiddleware())
    dp.include_router(commands.router)
    dp.include_router(photo.router)
    dp.include_router(callbacks.router)
    dp.include_router(text.router)

    scheduler = build_scheduler(settings)
    register_jobs(scheduler, settings)
    scheduler.start()

    server = build_admin_server(settings) if settings.admin_enabled else None

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in _SIGNALS:
        loop.add_signal_handler(sig, stop.set)

    async def wait_stop() -> None:
        await stop.wait()
        log.info("shutdown_requested")
        if server is not None:
            server.should_exit = True
        with contextlib.suppress(RuntimeError):  # el polling puede no haber arrancado aún
            await dp.stop_polling()

    me = await bot.get_me()
    log.info(
        "bot_started",
        username=me.username,
        tz=settings.tz,
        admin=admin_url(settings) if server is not None else None,
    )
    try:
        async with asyncio.TaskGroup() as tg:
            polling = tg.create_task(
                dp.start_polling(
                    bot,
                    handle_signals=False,
                    allowed_updates=dp.resolve_used_update_types(),
                )
            )
            # Si el polling termina por sí solo, apagar el resto.
            polling.add_done_callback(lambda _: stop.set())
            if server is not None:
                tg.create_task(server.serve())
            tg.create_task(wait_stop())
    finally:
        for sig in _SIGNALS:
            loop.remove_signal_handler(sig)
        scheduler.shutdown(wait=False)
        await repo.close_all()
        await bot.session.close()
        log.info("bot_stopped")


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    configure_logging(settings)
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()

"""Arranque: configuración, base de datos, proveedores de LLM, scheduler y bot (long polling)."""

from __future__ import annotations

import asyncio
import contextlib
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import commands
from app.bot.middlewares.auth import AuthMiddleware
from app.config import ConfigError, Settings, harden_environment, load_settings, startup_warnings
from app.db.session import check_connection, create_engine, make_session_factory
from app.llm.provider import build_providers
from app.log import configure_logging, get_logger
from app.scheduler.jobs import build_scheduler, register_jobs

log = get_logger(__name__)


async def run(settings: Settings) -> None:
    for warning in startup_warnings(settings):
        log.warning("config_warning", detail=warning)
    harden_environment(settings)
    settings.photos_dir.mkdir(parents=True, exist_ok=True)

    engine = create_engine(settings)
    await check_connection(engine)
    session_factory = make_session_factory(engine)
    log.info("db_connected")

    providers = build_providers(settings)
    log.info("llm_providers", vision=providers.vision.name, text=providers.text.name)

    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(settings=settings, providers=providers, session_factory=session_factory)
    dp.update.outer_middleware(AuthMiddleware(settings.allowed_user_ids, settings.allowed_chat_ids))
    dp.include_router(commands.router)

    scheduler = build_scheduler(settings)
    register_jobs(scheduler, settings)
    scheduler.start()

    me = await bot.get_me()
    log.info("bot_started", username=me.username, tz=settings.tz)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await engine.dispose()
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

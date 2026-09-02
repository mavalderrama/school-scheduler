"""Comandos básicos. Deben funcionar sin LLM."""

from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.config import Settings
from app.llm.compose import format_date_es
from app.services import notify

router = Router(name="commands")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Hola, soy el bot de la agenda escolar. "
        "Mándame una foto de la agenda y te digo qué entendí. "
        "Usa /manana para ver lo de mañana y /ping para comprobar que estoy vivo."
    )


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    await message.answer("pong")


@router.message(Command("manana"))
async def cmd_manana(message: Message, settings: Settings) -> None:
    """Lo de mañana, con el mismo formato que la notificación de las 19:00 (sin registrarla)."""
    tomorrow = datetime.now(settings.zoneinfo).date() + timedelta(days=1)
    if settings.skip_weekend and tomorrow.weekday() >= 5:
        await message.answer(f"Mañana es {format_date_es(tomorrow)}: no hay colegio. 🎉")
        return
    _, text = await notify.build_daily_message(tomorrow)
    await message.answer(text)

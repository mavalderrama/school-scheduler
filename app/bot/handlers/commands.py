"""Comandos básicos. Deben funcionar sin LLM."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

router = Router(name="commands")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Hola, soy el bot de la agenda escolar. "
        "Mándame una foto de la agenda y te digo qué entendí. "
        "Usa /ping para comprobar que estoy vivo."
    )


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    await message.answer("pong")

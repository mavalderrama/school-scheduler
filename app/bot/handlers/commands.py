"""Comandos. Todos funcionan sin LLM: son la red de seguridad cuando la IA está caída."""

from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.config import Settings
from app.db import repo
from app.llm import compose
from app.services import chat, notify
from app.services.confirm import PendingEdit, PendingPhoto, PendingStore

router = Router(name="commands")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(compose.HELP_TEXT)


@router.message(Command("ayuda"))
async def cmd_ayuda(message: Message) -> None:
    await message.answer(compose.HELP_TEXT)


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    await message.answer("pong")


@router.message(Command("hoy"))
async def cmd_hoy(message: Message, settings: Settings) -> None:
    today = datetime.now(settings.zoneinfo).date()
    entries = await repo.active_entries(today, today)
    await message.answer(
        compose.format_agenda(
            entries,
            title=f"📚 Hoy, {compose.format_date_es(today)}:",
            empty=f"No tengo nada para hoy ({compose.format_date_es(today)}).",
        )
    )


@router.message(Command("manana"))
async def cmd_manana(message: Message, settings: Settings) -> None:
    """Lo de mañana, con el mismo formato que la notificación de las 19:00 (sin registrarla)."""
    tomorrow = datetime.now(settings.zoneinfo).date() + timedelta(days=1)
    if settings.skip_weekend and tomorrow.weekday() >= 5:
        await message.answer(f"Mañana es {compose.format_date_es(tomorrow)}: no hay colegio. 🎉")
        return
    _, text = await notify.build_daily_message(tomorrow)
    await message.answer(text)


@router.message(Command("semana"))
async def cmd_semana(message: Message, settings: Settings) -> None:
    today = datetime.now(settings.zoneinfo).date()
    date_from, date_to = chat.week_range(today)
    entries = await repo.active_entries(date_from, date_to)
    await message.answer(
        compose.format_agenda(
            entries,
            title="📚 Esta semana:",
            empty=(
                f"No tengo nada entre el {compose.format_date_es(date_from)} y el "
                f"{compose.format_date_es(date_to)}."
            ),
        )
    )


@router.message(Command("pendiente"))
async def cmd_pendiente(message: Message, pending: PendingStore) -> None:
    current = pending.get(message.chat.id)
    if current is None:
        queued = pending.queued(message.chat.id)
        extra = f" Hay {queued} foto(s) en cola." if queued else ""
        await message.answer(f"No hay nada pendiente de confirmar.{extra}")
        return
    if isinstance(current, PendingPhoto):
        await message.answer(
            "📷 Hay una foto pendiente de confirmar:\n\n"
            + compose.format_extraction(current.extraction)
        )
        return
    if isinstance(current, PendingEdit) and current.action == "add":
        await message.answer(
            "✍️ Pendiente: "
            + compose.format_add_question(
                current.entry_date, current.kind or "note", current.text or ""
            )
        )
        return
    await message.answer("✍️ Hay una baja pendiente de confirmar.")

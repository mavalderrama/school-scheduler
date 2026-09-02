"""Comandos. Todos funcionan sin LLM: son la red de seguridad cuando la IA está caída."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.config import Settings
from app.db import repo
from app.llm import compose
from app.llm.provider import LLMProviders
from app.services import chat, notify, status
from app.services import schedule as schedule_service
from app.services.confirm import PendingEdit, PendingPhoto, PendingQuestions, PendingStore

router = Router(name="commands")


async def _slots(day: date, settings: Settings) -> list[schedule_service.SlotResult]:
    """Las clases del día, una por horario vigente. Vacío si no hay horarios o está apagado."""
    if not settings.schedule_enabled:
        return []
    return await schedule_service.resolve_day(day, country=settings.school_country)


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
    lines = [f"📚 Hoy, {compose.format_date_es(today)}:"]
    lines.extend(compose.slot_lines(await _slots(today, settings)))
    lines.extend(compose.stored_line(e) for e in entries)
    if len(lines) == 1:
        lines.append("No tengo nada apuntado.")
    await message.answer("\n".join(lines))


@router.message(Command("manana"))
async def cmd_manana(message: Message, settings: Settings) -> None:
    """Lo de mañana, con el mismo formato que la notificación de las 19:00 (sin registrarla)."""
    tomorrow = datetime.now(settings.zoneinfo).date() + timedelta(days=1)
    if settings.skip_weekend and tomorrow.weekday() >= 5:
        await message.answer(f"Mañana es {compose.format_date_es(tomorrow)}: no hay colegio. 🎉")
        return
    _, text = await notify.build_daily_message(
        tomorrow,
        country=settings.school_country,
        use_schedule=settings.schedule_enabled,
    )
    await message.answer(text)


@router.message(Command("semana"))
async def cmd_semana(message: Message, settings: Settings) -> None:
    today = datetime.now(settings.zoneinfo).date()
    date_from, date_to = chat.week_range(today)
    entries = await repo.active_entries(date_from, date_to)
    agenda_text = compose.format_agenda(
        entries,
        title="📚 Esta semana:",
        empty=(
            f"No tengo nada apuntado entre el {compose.format_date_es(date_from)} y el "
            f"{compose.format_date_es(date_to)}."
        ),
    )
    if not settings.schedule_enabled:
        await message.answer(agenda_text)
        return
    monday = date_from - timedelta(days=date_from.weekday())
    plan = await schedule_service.resolve_week(monday, country=settings.school_country)
    if not any(plan):
        await message.answer(agenda_text)
        return
    labels = [s.week_label for day in plan for s in day if s.week_label]
    header = f"🗓️ <b>Semana {labels[0]}</b>" if labels else "🗓️ <b>Esta semana</b>"
    lines = [header]
    for day_slots in plan:
        lines.extend(compose.slot_lines(day_slots, with_date=True))
    await message.answer("\n".join(lines) + "\n\n" + agenda_text)


@router.message(Command("horario"))
async def cmd_horario(message: Message, settings: Settings) -> None:
    """La tabla completa del horario rotativo y en qué semana estamos. Sin LLM."""
    today = datetime.now(settings.zoneinfo).date()
    loaded = await schedule_service.load_all(today)
    if not loaded:
        await message.answer(compose.NO_SCHEDULE_TEXT)
        return
    blocks = []
    for item in loaded:
        index = schedule_service.week_index(today, item.template)
        current = next((s.week_label for s in item.slots if s.week_index == index), None)
        blocks.append(
            compose.format_schedule_table(item.template.name, item.slots, current_label=current)
        )
    await message.answer("\n\n".join(blocks))


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
    if isinstance(current, PendingQuestions):
        await message.answer(
            "❓ Te hice una pregunta y sigo esperando la respuesta:\n\n"
            + compose.format_question(current.current or "", remaining=0)
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


@router.message(Command("cancelar"))
async def cmd_cancelar(
    message: Message,
    settings: Settings,
    providers: LLMProviders,
    pending: PendingStore,
) -> None:
    """Salida de emergencia sin LLM: descarta lo que haya pendiente en este chat."""
    from app.bot import actions

    chat_id = message.chat.id
    current = pending.get(chat_id)
    if current is None:
        await message.answer("No hay nada pendiente que cancelar.")
        return
    if isinstance(current, PendingEdit):
        pending.clear(chat_id)
        await message.answer("Listo, no cambio nada.")
        return
    await message.answer(await actions.reject_photo(current))
    if message.bot is not None:
        await actions.continue_queue(message.bot, chat_id, settings, providers, pending)


@router.message(Command("estado"))
async def cmd_estado(message: Message, settings: Settings, providers: LLMProviders) -> None:
    """Informe de operación. `/estado check` además hace healthcheck real (gasta cuota)."""
    text = (message.text or "").split()
    if len(text) > 1 and text[1].lower() in {"check", "full", "salud"}:
        await message.answer("🩺 Comprobando proveedores (esto gasta una llamada)...")
        await message.answer(await status.check_providers(providers))
        return
    await message.answer(await status.build_status(settings, providers))

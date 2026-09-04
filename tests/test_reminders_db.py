"""Recordatorios contra la base: el barrido, la reserva y que no se manden dos veces."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, cast

import pytest
from aiogram import Bot
from django.db.utils import IntegrityError

from app.config import Settings
from app.db import repo
from app.db.models import NotificationKind, Reminder, RepeatKind
from app.scheduler import jobs
from app.services import reminders, scope
from app.services.scope import Scope
from tests.conftest import TENANT, make_child

pytestmark = pytest.mark.django_db(transaction=True)

SEVEN = time(7, 0)


class FakeBot:
    """Lo único que el job usa de aiogram es `send_message`."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id: int, text: str) -> None:
        if self.fail:
            raise RuntimeError("telegram caído")
        self.sent.append((chat_id, text))


async def a_scope() -> Scope:
    found = await scope.for_child(TENANT.child_id)
    assert found is not None
    return found


async def make_reminder(
    *,
    child_id: int | None = None,
    chat_id: int | None = None,
    next_fire_at: datetime | None = None,
    repeat: str = RepeatKind.DAILY,
    text: str = "revisar la agenda",
    on_date: date | None = None,
    **fields: Any,
) -> Reminder:
    sc = await a_scope()
    when = next_fire_at if next_fire_at is not None else datetime.now(sc.zoneinfo)
    return await repo.create_reminder(
        child_id=child_id if child_id is not None else TENANT.child_id,
        chat_id=chat_id if chat_id is not None else TENANT.chat_id,
        text=text,
        time_of_day=when.astimezone(sc.zoneinfo).timetz().replace(tzinfo=None),
        repeat=repeat,
        on_date=on_date if on_date is not None else (when.date() if repeat == "once" else None),
        next_fire_at=when,
        **fields,
    )


# --- El barrido -----------------------------------------------------------------------------


async def test_a_due_reminder_reaches_its_own_chat(settings: Settings) -> None:
    await make_reminder(chat_id=-4242, text="llevar el disfraz")
    bot = FakeBot()

    await jobs.reminders_job(cast(Bot, bot), settings)

    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == -4242
    assert "llevar el disfraz" in text
    assert "Recordatorio" in text


async def test_a_reminder_that_is_not_due_yet_is_left_alone(settings: Settings) -> None:
    sc = await a_scope()
    await make_reminder(next_fire_at=datetime.now(sc.zoneinfo) + timedelta(hours=2))
    bot = FakeBot()

    await jobs.reminders_job(cast(Bot, bot), settings)

    assert bot.sent == []


async def test_two_sweeps_in_a_row_send_only_once(settings: Settings) -> None:
    """La reserva es lo que lo garantiza: el segundo barrido ya no encuentra la ocurrencia."""
    await make_reminder()
    bot = FakeBot()

    await jobs.reminders_job(cast(Bot, bot), settings)
    await jobs.reminders_job(cast(Bot, bot), settings)

    assert len(bot.sent) == 1


async def test_a_daily_one_is_rearmed_for_the_next_day(settings: Settings) -> None:
    reminder = await make_reminder()
    before = reminder.next_fire_at
    assert before is not None

    await jobs.reminders_job(cast(Bot, FakeBot()), settings)

    again = await repo.get_reminder(reminder.pk, child_id=TENANT.child_id)
    assert again is not None
    assert again.is_active is True
    assert again.next_fire_at == before + timedelta(days=1)
    assert again.last_fired_at is not None


async def test_a_one_off_switches_itself_off_after_firing(settings: Settings) -> None:
    reminder = await make_reminder(repeat=RepeatKind.ONCE)

    await jobs.reminders_job(cast(Bot, FakeBot()), settings)

    again = await repo.get_reminder(reminder.pk, child_id=TENANT.child_id)
    assert again is not None
    assert (again.is_active, again.next_fire_at) == (False, None)
    assert await repo.reminders_of(TENANT.child_id) == []


# --- El bot estuvo caído ---------------------------------------------------------------------


async def test_a_repeating_one_hours_late_is_not_sent_but_is_recorded(settings: Settings) -> None:
    """Un diario de las 7:00 no se suelta a las 13:00: el de mañana es el bueno."""
    sc = await a_scope()
    late = datetime.now(sc.zoneinfo) - timedelta(hours=6)
    reminder = await make_reminder(next_fire_at=late)
    bot = FakeBot()

    await jobs.reminders_job(cast(Bot, bot), settings)

    assert bot.sent == []
    again = await repo.get_reminder(reminder.pk, child_id=TENANT.child_id)
    assert again is not None and again.next_fire_at is not None
    assert again.next_fire_at > datetime.now(sc.zoneinfo)  # rearmado hacia adelante
    logged = await repo.notifications(NotificationKind.REMINDER)
    assert [(row.ok, "fuera de plazo" in (row.error or "")) for row in logged] == [(False, True)]


async def test_a_one_off_hours_late_is_still_sent_with_a_warning(settings: Settings) -> None:
    """Se hizo una sola vez: perderla en silencio es peor que llegar tarde."""
    sc = await a_scope()
    late = datetime.now(sc.zoneinfo) - timedelta(hours=6)
    await make_reminder(repeat=RepeatKind.ONCE, next_fire_at=late, text="el disfraz")
    bot = FakeBot()

    await jobs.reminders_job(cast(Bot, bot), settings)

    assert len(bot.sent) == 1
    assert "retraso" in bot.sent[0][1]
    assert "el disfraz" in bot.sent[0][1]


# --- Errores y auditoría ----------------------------------------------------------------------


async def test_a_failed_send_is_logged_and_the_reminder_still_advances(
    settings: Settings,
) -> None:
    reminder = await make_reminder()

    await jobs.reminders_job(cast(Bot, FakeBot(fail=True)), settings)

    logged = await repo.notifications(NotificationKind.REMINDER)
    assert len(logged) == 1
    assert logged[0].ok is False and logged[0].reminder_id == reminder.pk
    # No se reintenta en bucle: ya está apuntado al día siguiente.
    again = await repo.get_reminder(reminder.pk, child_id=TENANT.child_id)
    assert again is not None and again.is_active is True


async def test_two_different_reminders_the_same_day_both_get_logged(settings: Settings) -> None:
    """Sin el recordatorio en la clave única, el segundo no se podría ni registrar."""
    await make_reminder(text="uno")
    await make_reminder(text="dos")
    bot = FakeBot()

    await jobs.reminders_job(cast(Bot, bot), settings)

    assert len(bot.sent) == 2
    logged = await repo.notifications(NotificationKind.REMINDER)
    assert [row.ok for row in logged] == [True, True]


async def test_the_daily_notification_is_still_idempotent() -> None:
    """La clave cambió de forma: el aviso diario tiene que seguir sin repetirse."""
    day = date(2026, 9, 8)
    await repo.log_notification(
        NotificationKind.DAILY, day, TENANT.chat_id, ok=True, error=None, child_id=TENANT.child_id
    )
    assert (
        await repo.notification_sent_ok(
            [NotificationKind.DAILY], day, TENANT.chat_id, TENANT.child_id
        )
        is True
    )
    with pytest.raises(IntegrityError):
        await repo.log_notification(
            NotificationKind.DAILY,
            day,
            TENANT.chat_id,
            ok=True,
            error=None,
            child_id=TENANT.child_id,
        )


async def test_a_reminder_row_does_not_silence_the_daily_notification() -> None:
    """Y al revés: un recordatorio de ese día no debe contar como el aviso diario."""
    day = date(2026, 9, 8)
    reminder = await make_reminder()
    await repo.log_notification(
        NotificationKind.REMINDER,
        day,
        TENANT.chat_id,
        ok=True,
        error=None,
        child_id=TENANT.child_id,
        reminder_id=reminder.pk,
    )
    assert (
        await repo.notification_sent_ok(
            [NotificationKind.DAILY], day, TENANT.chat_id, TENANT.child_id
        )
        is False
    )


# --- Invariantes de la tabla --------------------------------------------------------------


async def test_an_active_reminder_must_have_a_next_time() -> None:
    """Una promesa que nadie va a cumplir no se puede guardar. Sin el ayudante, que rellena."""
    with pytest.raises(IntegrityError):
        await repo.create_reminder(
            child_id=TENANT.child_id,
            chat_id=TENANT.chat_id,
            text="nunca suena",
            time_of_day=SEVEN,
            repeat=RepeatKind.DAILY,
            next_fire_at=None,
        )


async def test_deactivating_clears_the_next_time() -> None:
    reminder = await make_reminder()

    assert await repo.deactivate_reminder(reminder.pk, child_id=TENANT.child_id) is True

    again = await repo.get_reminder(reminder.pk, child_id=TENANT.child_id)
    assert again is not None and (again.is_active, again.next_fire_at) == (False, None)
    # Y ya no lo coge el barrido.
    assert await repo.due_reminders(datetime.now(tz=(await a_scope()).zoneinfo)) == []


async def test_claiming_twice_with_the_same_expectation_fails_the_second_time() -> None:
    sc = await a_scope()
    reminder = await make_reminder()
    due = reminder.next_fire_at
    assert due is not None
    later = due + timedelta(days=1)
    now = datetime.now(sc.zoneinfo)

    assert (
        await repo.claim_reminder(reminder.pk, expected=due, next_fire_at=later, fired_at=now)
        is True
    )
    assert (
        await repo.claim_reminder(reminder.pk, expected=due, next_fire_at=later, fired_at=now)
        is False
    )


# --- Aislamiento ------------------------------------------------------------------------------


async def test_the_sweep_sends_each_family_to_its_own_chat(settings: Settings) -> None:
    other = await make_child("Otra", chat_id=-777010)
    await make_reminder(chat_id=TENANT.chat_id, text="lo nuestro")
    await make_reminder(child_id=other.pk, chat_id=-777010, text="lo suyo")
    bot = FakeBot()

    await jobs.reminders_job(cast(Bot, bot), settings)

    assert sorted(bot.sent) == sorted(
        [
            (TENANT.chat_id, next(t for c, t in bot.sent if c == TENANT.chat_id)),
            (-777010, next(t for c, t in bot.sent if c == -777010)),
        ]
    )
    ours = next(t for c, t in bot.sent if c == TENANT.chat_id)
    theirs = next(t for c, t in bot.sent if c == -777010)
    assert "lo nuestro" in ours and "lo suyo" not in ours
    assert "lo suyo" in theirs and "lo nuestro" not in theirs


async def test_a_reminder_of_another_family_is_out_of_reach() -> None:
    other = await make_child("Otra", chat_id=-777011)
    theirs = await make_reminder(child_id=other.pk, chat_id=-777011, text="secreto")

    assert await repo.get_reminder(theirs.pk, child_id=TENANT.child_id) is None
    assert await repo.deactivate_reminder(theirs.pk, child_id=TENANT.child_id) is False
    assert [r.text for r in await repo.reminders_of(TENANT.child_id)] == []
    # Y sigue vivo para su dueño.
    assert [r.text for r in await repo.reminders_of(other.pk)] == ["secreto"]


async def test_the_search_by_hint_stays_within_the_child() -> None:
    other = await make_child("Otra", chat_id=-777012)
    await make_reminder(child_id=other.pk, chat_id=-777012, text="natación del jueves")
    await make_reminder(text="natación del martes")

    found = await repo.find_active_reminders(TENANT.child_id, "natación")
    assert [r.text for r in found] == ["natación del martes"]


async def test_the_max_per_child_is_a_real_number() -> None:
    """El tope existe para que un malentendido con el LLM no llene el chat."""
    assert 0 < reminders.MAX_PER_CHILD <= 100

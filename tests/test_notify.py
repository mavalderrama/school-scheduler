"""Notificación diaria y chequeo de huecos: formato, idempotencia, fin de semana, errores."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.config import Settings
from app.db import repo
from app.db.models import NotificationKind, SourceKind
from app.llm.schemas import ExtractedEntry, ExtractionResult
from app.services import agenda, notify

pytestmark = pytest.mark.django_db(transaction=True)

MON, TUE, WED = date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9)  # lunes, martes, miércoles
FRI, SAT, SUN = date(2026, 9, 11), date(2026, 9, 12), date(2026, 9, 13)


class FakeSender:
    def __init__(self, fail_for: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail_for = fail_for or set()

    async def __call__(self, chat_id: int, text: str) -> None:
        if chat_id in self.fail_for:
            raise RuntimeError("telegram caído")
        self.sent.append((chat_id, text))


async def seed(*entries: tuple[date, str, str]) -> None:
    source = await repo.create_source(SourceKind.MANUAL)
    await agenda.apply_source(
        source.pk,
        ExtractionResult(
            entries=[
                ExtractedEntry(entry_date=d, kind=k, text=t, confidence="high")
                for d, k, t in entries
            ],
            doubts=[],
            detected_language="es",
        ),
    )


# --- Puro --------------------------------------------------------------------------------


def test_daily_target_skips_weekend() -> None:
    assert notify.daily_target(MON, skip_weekend=True) == TUE
    assert notify.daily_target(FRI, skip_weekend=True) is None  # mañana sábado
    assert notify.daily_target(SAT, skip_weekend=True) is None  # mañana domingo
    assert notify.daily_target(SUN, skip_weekend=True) == date(2026, 9, 14)
    assert notify.daily_target(FRI, skip_weekend=False) == SAT


def test_next_week_days() -> None:
    assert notify.next_week_days(SUN) == [date(2026, 9, 14) + timedelta(days=i) for i in range(5)]
    assert notify.next_week_days(WED)[0] == date(2026, 9, 14)


def test_format_nudge_and_gaps() -> None:
    assert (
        notify.format_nudge(TUE)
        == "📚 No tengo agenda para mañana (martes 8 de septiembre). ¿Me mandan foto?"
    )
    assert notify.format_gaps([WED, date(2026, 9, 10)]).startswith(
        "📅 Para la semana que viene no tengo nada para: miércoles 9, jueves 10."
    )


# --- Con DB ---------------------------------------------------------------------------------


async def test_daily_message_format_groups_by_kind(settings: Settings) -> None:
    await seed(
        (TUE, "bring", "sudadera"),
        (TUE, "bring", "botella de <agua>"),
        (TUE, "homework", "cuaderno de números pág. 12"),
        (TUE, "event", "salida al parque"),
        (WED, "note", "no debe salir"),
    )
    kind, text = await notify.build_daily_message(TUE)
    assert kind == NotificationKind.DAILY
    assert text == (
        "📚 Mañana, martes 8 de septiembre\n"
        "🎒 Llevar: sudadera, botella de &lt;agua&gt;\n"
        "📝 Tarea: cuaderno de números pág. 12\n"
        "📌 Evento: salida al parque"
    )


async def test_send_daily_is_idempotent_per_chat(settings: Settings) -> None:
    settings = settings.model_copy(update={"notify_chat_ids": [-100, -200]})
    await seed((TUE, "bring", "sudadera"))
    sender = FakeSender()
    first = await notify.send_daily(sender, settings, MON)
    assert [(o.chat_id, o.sent, o.skipped) for o in first] == [
        (-100, True, False),
        (-200, True, False),
    ]
    assert [c for c, _ in sender.sent] == [-100, -200]

    second = await notify.send_daily(sender, settings, MON)
    assert all(o.skipped for o in second)
    assert len(sender.sent) == 2  # no reenvía
    rows = await repo.notifications(NotificationKind.DAILY)
    assert [(r.chat_id, r.ok, r.target_date) for r in rows] == [
        (-100, True, TUE),
        (-200, True, TUE),
    ]


async def test_send_daily_nudges_when_empty(settings: Settings) -> None:
    sender = FakeSender()
    outcomes = await notify.send_daily(sender, settings, MON)
    assert [o.kind for o in outcomes] == [NotificationKind.NUDGE_EMPTY]
    assert sender.sent == [(-100999, notify.format_nudge(TUE))]
    # Y no se vuelve a mandar aunque la agenda siga vacía.
    assert all(o.skipped for o in await notify.send_daily(sender, settings, MON))


async def test_nudge_then_photo_does_not_resend_that_day(settings: Settings) -> None:
    sender = FakeSender()
    await notify.send_daily(sender, settings, MON)
    await seed((TUE, "bring", "sudadera"))
    outcomes = await notify.send_daily(sender, settings, MON)
    assert outcomes[0].skipped and len(sender.sent) == 1


async def test_send_daily_skips_weekend(settings: Settings) -> None:
    sender = FakeSender()
    assert await notify.send_daily(sender, settings, FRI) == []
    assert sender.sent == []


async def test_failed_send_is_logged_and_retried_next_run(settings: Settings) -> None:
    await seed((TUE, "bring", "sudadera"))
    failing = FakeSender(fail_for={-100999})
    outcomes = await notify.send_daily(failing, settings, MON)
    assert outcomes[0].sent is False and "telegram caído" in (outcomes[0].error or "")
    rows = await repo.notifications()
    assert [(r.ok, r.error is not None) for r in rows] == [(False, True)]

    working = FakeSender()
    again = await notify.send_daily(working, settings, MON)
    assert again[0].sent is True and not again[0].skipped
    assert [r.ok for r in await repo.notifications()] == [False, True]


async def test_gap_check_reports_uncovered_weekdays(settings: Settings) -> None:
    next_mon, next_tue = date(2026, 9, 14), date(2026, 9, 15)
    await seed((next_mon, "bring", "x"), (next_tue, "note", "y"))
    sender = FakeSender()
    outcomes = await notify.send_gap_check(sender, settings, SUN)
    assert [o.kind for o in outcomes] == [NotificationKind.GAP_CHECK]
    assert sender.sent[0][1] == notify.format_gaps(
        [date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)]
    )
    assert all(o.skipped for o in await notify.send_gap_check(sender, settings, SUN))


async def test_gap_check_silent_when_week_is_covered(settings: Settings) -> None:
    await seed(*((date(2026, 9, 14 + i), "note", "x") for i in range(5)))
    sender = FakeSender()
    assert await notify.send_gap_check(sender, settings, SUN) == []
    assert sender.sent == []


# --- Fase 6: el horario cuenta como contenido -----------------------------------------------


async def seed_schedule(anchor: date = date(2026, 8, 31)) -> None:
    """El horario K4A, reducido a las franjas que usan estos tests."""
    from app.llm.schemas import ScheduleDraft, SlotDraft

    source = await repo.create_source(SourceKind.PHOTO, chat_id=-100999)
    await agenda.apply_source(
        source.pk,
        ExtractionResult(
            entries=[],
            doubts=[],
            detected_language="es",
            doc_type="schedule",
            schedule=ScheduleDraft(
                name="Horario K4A",
                cycle_weeks=2,
                anchor_monday=anchor,
                slots=[
                    SlotDraft(week_label="A", weekday=1, rotation="1", subject="Artes plásticas"),
                    SlotDraft(
                        week_label="A", weekday=2, rotation="2", subject="Expresión corporal"
                    ),
                    SlotDraft(week_label="B", weekday=1, rotation="6", subject="Deporte 2"),
                    SlotDraft(week_label="B", weekday=2, rotation="7", subject="Motricidad"),
                ],
            ),
        ),
        today=anchor,
    )


async def test_a_class_replaces_the_empty_nudge(settings: Settings) -> None:
    """Antes, sin entradas, solo salía «mándame foto». Ahora se avisa de la clase."""
    await seed_schedule()
    send = FakeSender()
    outcomes = await notify.send_daily(send, settings, MON)  # mañana es martes 8, Semana B

    assert [o.kind for o in outcomes] == [NotificationKind.DAILY]
    text = send.sent[0][1]
    assert "Motricidad" in text and "Semana B" in text and "rot. 7" in text
    assert "¿Me mandan foto?" not in text


async def test_the_class_comes_before_the_agenda_entries(settings: Settings) -> None:
    await seed_schedule()
    await seed((TUE, "bring", "sudadera"))
    send = FakeSender()
    await notify.send_daily(send, settings, MON)

    text = send.sent[0][1]
    assert text.index("Motricidad") < text.index("sudadera")


async def test_a_holiday_is_announced_instead_of_asking_for_a_photo(settings: Settings) -> None:
    """El 12 de octubre de 2026 es lunes festivo: mejor decirlo que pedir una foto."""
    await seed_schedule()
    send = FakeSender()
    await notify.send_daily(send, settings, date(2026, 10, 11))  # domingo -> mañana lunes 12

    text = send.sent[0][1]
    assert "sin clase" in text and "Día de la Raza" in text


async def test_without_a_schedule_the_nudge_still_works(settings: Settings) -> None:
    """Sin horario cargado no se inventa nada: sigue el aviso de agenda vacía."""
    send = FakeSender()
    outcomes = await notify.send_daily(send, settings, MON)
    assert [o.kind for o in outcomes] == [NotificationKind.NUDGE_EMPTY]
    assert "¿Me mandan foto?" in send.sent[0][1]


async def test_the_schedule_can_be_turned_off(settings: Settings) -> None:
    await seed_schedule()
    off = settings.model_copy(update={"schedule_enabled": False})
    send = FakeSender()
    outcomes = await notify.send_daily(send, off, MON)
    assert [o.kind for o in outcomes] == [NotificationKind.NUDGE_EMPTY]

"""El grafo de conversación y, sobre todo, que sobreviva a un reinicio.

Esta es la prueba que motivó toda la fase: al desplegar, una foto ya leída que esperaba ✅
quedó huérfana y hubo que reenviarla. Aquí se simula el reinicio de verdad —se tira el
saver y el grafo y se reconstruyen desde Postgres— y la conversación tiene que continuar.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.db import repo
from app.graph.build import build_graph, checkpointer_pool
from app.graph.runner import GraphRunner
from app.graph.state import GraphContext, GraphState
from app.llm.provider import LLMProviders
from app.llm.schemas import ExtractionResult, ScheduleDraft, SlotDraft
from app.llm.tenant import TenantProviders
from tests.conftest import TENANT, TEST_DATABASE_URL
from tests.test_ingest import providers
from tests.test_provider import FakeProvider

pytestmark = pytest.mark.django_db(transaction=True)

ANCHOR = date(2026, 8, 31)
CHAT = -100777


def today() -> date:
    """Hoy de verdad. Un horario se guarda vigente **desde hoy**, así que preguntar por una
    fecha fija hacía que el test caducara: pasó al llegar el día siguiente."""
    return datetime.now(ZoneInfo("America/Bogota")).date()


K4A = [
    ("A", 1, "1", "Artes plásticas"),
    ("A", 4, "4", "Música"),
    ("B", 4, "9", "Natación"),
]


def schedule_extraction(anchor: date | None) -> ExtractionResult:
    return ExtractionResult(
        entries=[],
        doubts=[],
        detected_language="es",
        doc_type="schedule",
        schedule=ScheduleDraft(
            name="Horario K4A",
            cycle_weeks=2,
            anchor_monday=anchor,
            slots=[SlotDraft(week_label=w, weekday=d, rotation=r, subject=s) for w, d, r, s in K4A],
        ),
    )


async def fake_download(file_id: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"\xff\xd8fake-jpeg")  # noqa: ASYNC240 (test)


def fake_tenants(settings: Settings, provider: FakeProvider) -> TenantProviders:
    """Un resolutor que devuelve siempre la misma cadena falsa.

    Los tests del grafo no van de credenciales: comprueban que la conversación sobrevive a
    un reinicio, así que aquí el resolutor solo tiene que existir.
    """
    tenants = TenantProviders(settings)
    chain = providers(provider)

    async def always(family_id: int) -> LLMProviders:
        return chain

    tenants.for_family = always  # type: ignore[method-assign]
    return tenants


def photo_state(queue: list[dict[str, Any]] | None = None) -> GraphState:
    return {
        "chat_id": CHAT,
        "child_id": TENANT.child_id,
        "flow": "photo",
        "photo": {
            "file_id": "f1",
            "user_id": 111,
            "display_name": "Alejandro",
            "caption": None,
        },
        "queue": queue or [],
        "questions": [],
        "answers": [],
        "attempts": 0,
    }


async def make_runner(settings: Settings, provider: FakeProvider) -> AsyncIterator[GraphRunner]:
    """Un runner nuevo con su propio saver: reconstruirlo simula un reinicio del proceso."""
    async with checkpointer_pool(TEST_DATABASE_URL) as saver:
        graph = build_graph().compile(checkpointer=saver)
        context = GraphContext(
            settings=settings, tenants=fake_tenants(settings, provider), download=fake_download
        )
        yield GraphRunner(graph, context)


@pytest.fixture
async def clean_thread() -> AsyncIterator[None]:
    """Cada test empieza con el hilo de este chat vacío."""
    async with checkpointer_pool(TEST_DATABASE_URL) as saver:
        await saver.adelete_thread(f"chat:{CHAT}")
    yield
    async with checkpointer_pool(TEST_DATABASE_URL) as saver:
        await saver.adelete_thread(f"chat:{CHAT}")


# --- Lo esencial: sobrevivir a un reinicio ---------------------------------------------------


async def test_the_conversation_survives_a_restart(settings: Settings, clean_thread: None) -> None:
    """Foto → pregunta → **reinicio** → respuesta → resumen → ✅. Sin reenviar nada."""
    provider = FakeProvider("claude_sdk", result=schedule_extraction(None))
    provider.refined = schedule_extraction(ANCHOR)

    # 1) Llega la foto: al horario le falta el lunes ancla, así que el bot pregunta.
    async for runner in make_runner(settings, provider):
        turn = await runner.start(CHAT, photo_state())
        assert turn.ask is not None and turn.ask.kind == "ask"
        assert "lunes" in (turn.ask.text or "").lower()

    # 2) --- REINICIO --- se tira el runner, el grafo y el saver.

    # 3) Otro proceso, otro saver: el hilo sigue esperando la respuesta.
    async for runner in make_runner(settings, provider):
        assert await runner.is_waiting(CHAT) is True
        resumed = await runner.resume(CHAT, "el martes 1 de septiembre")
        assert resumed is not None and resumed.ask is not None
        assert resumed.ask.kind == "summary"  # ya no falta nada: pide confirmar

    # 4) Otro reinicio más, y se confirma.
    async for runner in make_runner(settings, provider):
        resumed = await runner.resume(CHAT, {"action": "confirm"})
        assert resumed is not None and resumed.finished
        assert resumed.reply is not None and "Guardado" in resumed.reply

    template = await repo.active_schedule(TENANT.child_id, today())
    assert template is not None and template.anchor_monday == ANCHOR


async def test_a_photo_awaiting_confirmation_survives_a_restart(
    settings: Settings, clean_thread: None
) -> None:
    """El caso exacto que se rompió en producción: leída, esperando ✅, y el bot reinicia."""
    provider = FakeProvider("claude_sdk", result=schedule_extraction(ANCHOR))

    async for runner in make_runner(settings, provider):
        turn = await runner.start(CHAT, photo_state())
        assert turn.ask is not None and turn.ask.kind == "summary"

    async for runner in make_runner(settings, provider):
        # Antes, aquí el ✅ contestaba «esta lectura ya no está activa».
        resumed = await runner.resume(CHAT, {"action": "confirm"})
        assert resumed is not None and resumed.finished and resumed.reply is not None

    assert await repo.active_schedule(TENANT.child_id, today()) is not None


async def test_the_photo_queue_survives_a_restart(settings: Settings, clean_thread: None) -> None:
    """La cola no dejaba ni rastro: una foto encolada era irrecuperable."""
    provider = FakeProvider("claude_sdk", result=schedule_extraction(ANCHOR))
    second = {"file_id": "f2", "user_id": 111, "display_name": "Alejandro", "caption": None}

    async for runner in make_runner(settings, provider):
        await runner.start(CHAT, photo_state())
        assert await runner.enqueue(CHAT, second) == 1

    async for runner in make_runner(settings, provider):
        state = await runner.snapshot(CHAT)
        assert state is not None
        assert [p["file_id"] for p in state.get("queue", [])] == ["f2"]
        assert await runner.is_waiting(CHAT) is True


# --- Guardas ---------------------------------------------------------------------------------


async def test_resuming_a_thread_that_waits_for_nothing_is_ignored(
    settings: Settings, clean_thread: None
) -> None:
    """Un ✅ pulsado dos veces no debe envenenar la siguiente pregunta.

    `Command(resume=...)` sobre un hilo sin interrupción **no falla**: deja el valor
    guardado y se lo come la siguiente pregunta que haya. Por eso el runner comprueba
    antes si hay algo esperando.
    """
    provider = FakeProvider("claude_sdk", result=schedule_extraction(ANCHOR))
    async for runner in make_runner(settings, provider):
        await runner.start(CHAT, photo_state())
        first = await runner.resume(CHAT, {"action": "confirm"})
        assert first is not None and first.finished
        # Segunda pulsación del mismo botón.
        assert await runner.resume(CHAT, {"action": "confirm"}) is None


async def test_cancelling_during_a_question_discards_the_photo(
    settings: Settings, clean_thread: None
) -> None:
    provider = FakeProvider("claude_sdk", result=schedule_extraction(None))
    async for runner in make_runner(settings, provider):
        turn = await runner.start(CHAT, photo_state())
        assert turn.ask is not None and turn.ask.kind == "ask"
        resumed = await runner.resume(CHAT, "descarta")
        assert resumed is not None and resumed.finished
        assert resumed.reply is not None and "descarto" in resumed.reply.lower()
    assert await repo.active_schedule(TENANT.child_id, today()) is None


async def test_the_question_round_is_still_bounded(settings: Settings, clean_thread: None) -> None:
    """El modelo nunca resuelve el ancla: tras `MAX_REFINE_ROUNDS` se pide confirmar igual."""
    provider = FakeProvider("claude_sdk", result=schedule_extraction(None))
    provider.refined = schedule_extraction(None)  # nunca completa

    async for runner in make_runner(settings, provider):
        turn = await runner.start(CHAT, photo_state())
        for _ in range(4):
            if turn.ask is None or turn.ask.kind != "ask":
                break
            resumed = await runner.resume(CHAT, "no lo sé")
            assert resumed is not None
            turn = resumed
        assert turn.ask is not None
        assert turn.ask.kind == "summary" and turn.ask.gave_up is True


async def test_an_abandoned_conversation_expires(settings: Settings, clean_thread: None) -> None:
    """Una pregunta de hace tres días no debe revivir cuando el usuario escriba otra cosa."""
    provider = FakeProvider("claude_sdk", result=schedule_extraction(None))
    async for runner in make_runner(settings, provider):
        turn = await runner.start(CHAT, photo_state())
        assert turn.ask is not None
        assert await runner.is_waiting(CHAT) is True

    # Mismo hilo, pero con la caducidad a cero: se considera abandonado.
    async with checkpointer_pool(TEST_DATABASE_URL) as saver:
        graph = build_graph().compile(checkpointer=saver)
        context = GraphContext(
            settings=settings, tenants=fake_tenants(settings, provider), download=fake_download
        )
        stale = GraphRunner(graph, context, saver, ttl_hours=0)
        assert await stale.is_waiting(CHAT) is False
        assert await stale.resume(CHAT, "tarde") is None


async def test_a_reminder_confirmation_survives_a_restart(
    settings: Settings, clean_thread: None
) -> None:
    """El `edit` del recordatorio pasa por el checkpointer: fechas y nulos incluidos."""
    provider = FakeProvider("claude_sdk", result=schedule_extraction(ANCHOR))
    edit = {
        "edit_id": 77,
        "chat_id": CHAT,
        "action": "add_reminder",
        "text": "el disfraz",
        "time_of_day": "07:00",
        "repeat": "weekly",
        "weekdays": "13",
        "on_date": None,
        "only_school_days": True,
    }
    state: GraphState = {
        "chat_id": CHAT,
        "child_id": TENANT.child_id,
        "flow": "edit",
        "edit": edit,
        "user_id": None,
        "queue": [],
    }

    async for runner in make_runner(settings, provider):
        turn = await runner.start(CHAT, state)
        assert turn.ask is not None and turn.ask.kind == "edit"

    # --- REINICIO --- y se confirma desde otro proceso.
    async for runner in make_runner(settings, provider):
        resumed = await runner.resume(CHAT, {"action": "confirm", "edit_id": 77})
        assert resumed is not None and resumed.finished
        assert resumed.reply is not None and "Te aviso" in resumed.reply

    saved = await repo.reminders_of(TENANT.child_id)
    assert len(saved) == 1
    assert (saved[0].weekdays, saved[0].only_school_days) == ("13", True)
    assert saved[0].next_fire_at is not None


async def test_rejecting_a_reminder_saves_nothing(settings: Settings, clean_thread: None) -> None:
    provider = FakeProvider("claude_sdk", result=schedule_extraction(ANCHOR))
    state: GraphState = {
        "chat_id": CHAT,
        "child_id": TENANT.child_id,
        "flow": "edit",
        "edit": {
            "edit_id": 78,
            "chat_id": CHAT,
            "action": "add_reminder",
            "text": "nada",
            "time_of_day": "07:00",
            "repeat": "daily",
            "weekdays": "",
            "on_date": None,
            "only_school_days": False,
        },
        "user_id": None,
        "queue": [],
    }

    async for runner in make_runner(settings, provider):
        await runner.start(CHAT, state)
        resumed = await runner.resume(CHAT, {"action": "reject", "edit_id": 78})
        assert resumed is not None and resumed.finished

    assert await repo.reminders_of(TENANT.child_id) == []


# --- Fase 10.1: alta recurrente y la oferta de aviso ------------------------------------------


def recurring_state() -> GraphState:
    return {
        "chat_id": CHAT,
        "child_id": TENANT.child_id,
        "flow": "edit",
        "edit": {
            "edit_id": 90,
            "chat_id": CHAT,
            "action": "add_recurring",
            "weekdays": "5",
            "text": "natación",
        },
        "user_id": None,
        "queue": [],
    }


async def test_a_recurring_is_saved_and_then_offers_the_reminder(
    settings: Settings, clean_thread: None
) -> None:
    """✅ guarda la regla y **sigue preguntando** si además quiere aviso a una hora."""
    provider = FakeProvider("claude_sdk", result=schedule_extraction(ANCHOR))

    async for runner in make_runner(settings, provider):
        turn = await runner.start(CHAT, recurring_state())
        assert turn.ask is not None and turn.ask.kind == "edit"

        saved = await runner.resume(CHAT, {"action": "confirm", "edit_id": 90})
        assert saved is not None and saved.ask is not None
        assert saved.ask.kind == "offer_reminder"
        # La confirmación del alta viaja en la misma pregunta: si no, se perdería.
        assert "Guardado" in (saved.ask.text or "") and "viernes" in (saved.ask.text or "")

        # La regla ya está guardada aunque el aviso siga sin decidirse.
        assert [t.name for t in await repo.active_schedules(TENANT.child_id, today())] == [
            "Natación"
        ]

        answered = await runner.resume(CHAT, "a las 6 de la tarde")
        assert answered is not None and answered.finished
        assert answered.reply is not None and "Te aviso" in answered.reply

    reminders_saved = await repo.reminders_of(TENANT.child_id)
    assert len(reminders_saved) == 1
    assert reminders_saved[0].weekdays == "5"
    assert reminders_saved[0].repeat == "weekly"
    assert reminders_saved[0].only_school_days is True
    assert reminders_saved[0].text == "natación"


async def test_saying_no_to_the_offer_keeps_the_recurring(
    settings: Settings, clean_thread: None
) -> None:
    """«No» aquí es «sin aviso», nunca deshacer lo que ya se guardó."""
    provider = FakeProvider("claude_sdk", result=schedule_extraction(ANCHOR))

    async for runner in make_runner(settings, provider):
        await runner.start(CHAT, recurring_state())
        await runner.resume(CHAT, {"action": "confirm", "edit_id": 90})
        done = await runner.resume(CHAT, "no")
        assert done is not None and done.finished
        assert done.reply is not None and "sin aviso" in done.reply

    assert await repo.reminders_of(TENANT.child_id) == []
    assert [t.name for t in await repo.active_schedules(TENANT.child_id, today())] == ["Natación"]


async def test_the_offer_survives_a_restart(settings: Settings, clean_thread: None) -> None:
    """La pregunta del aviso es estado como cualquier otro: no se pierde al reiniciar."""
    provider = FakeProvider("claude_sdk", result=schedule_extraction(ANCHOR))

    async for runner in make_runner(settings, provider):
        await runner.start(CHAT, recurring_state())
        turn = await runner.resume(CHAT, {"action": "confirm", "edit_id": 90})
        assert turn is not None and turn.ask is not None

    # --- REINICIO ---
    async for runner in make_runner(settings, provider):
        assert await runner.is_waiting(CHAT) is True
        answered = await runner.resume(CHAT, "18:30")
        assert answered is not None and answered.finished
        assert answered.reply is not None and "18:30" in answered.reply

    assert len(await repo.reminders_of(TENANT.child_id)) == 1


async def test_an_answer_that_is_not_an_hour_does_not_invent_one(
    settings: Settings, clean_thread: None
) -> None:
    provider = FakeProvider("claude_sdk", result=schedule_extraction(ANCHOR))

    async for runner in make_runner(settings, provider):
        await runner.start(CHAT, recurring_state())
        await runner.resume(CHAT, {"action": "confirm", "edit_id": 90})
        done = await runner.resume(CHAT, "pues no sé, cuando toque")
        assert done is not None and done.finished
        assert done.reply is not None and "No entendí la hora" in done.reply

    assert await repo.reminders_of(TENANT.child_id) == []

"""El grafo de conversación y, sobre todo, que sobreviva a un reinicio.

Esta es la prueba que motivó toda la fase: al desplegar, una foto ya leída que esperaba ✅
quedó huérfana y hubo que reenviarla. Aquí se simula el reinicio de verdad —se tira el
saver y el grafo y se reconstruyen desde Postgres— y la conversación tiene que continuar.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.db import repo
from app.graph.build import build_graph, checkpointer_pool
from app.graph.runner import GraphRunner
from app.graph.state import GraphContext, GraphState
from app.llm.schemas import ExtractionResult, ScheduleDraft, SlotDraft
from tests.conftest import TENANT, TEST_DATABASE_URL
from tests.test_ingest import providers
from tests.test_provider import FakeProvider

pytestmark = pytest.mark.django_db(transaction=True)

ANCHOR = date(2026, 8, 31)
CHAT = -100777

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
            settings=settings, providers=providers(provider), download=fake_download
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

    template = await repo.active_schedule(TENANT.child_id, date(2026, 9, 2))
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

    assert await repo.active_schedule(TENANT.child_id, date(2026, 9, 2)) is not None


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
    assert await repo.active_schedule(TENANT.child_id, date(2026, 9, 2)) is None


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
            settings=settings, providers=providers(provider), download=fake_download
        )
        stale = GraphRunner(graph, context, saver, ttl_hours=0)
        assert await stale.is_waiting(CHAT) is False
        assert await stale.resume(CHAT, "tarde") is None

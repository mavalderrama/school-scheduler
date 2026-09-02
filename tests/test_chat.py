"""Texto libre: clasificación con caché y despacho a la lógica determinista."""

from __future__ import annotations

from datetime import date

import pytest

from app.config import Settings
from app.db import repo
from app.db.models import SourceKind
from app.llm.provider import LLMUnavailableError
from app.llm.schemas import ChatTurn, ExtractedEntry, ExtractionResult, Intent
from app.services import agenda, chat
from app.services.confirm import PendingStore
from tests.test_ingest import providers
from tests.test_provider import FakeProvider

pytestmark = pytest.mark.django_db(transaction=True)

MON, TUE, WED = date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9)
SAT, SUN = date(2026, 9, 12), date(2026, 9, 13)


class IntentProvider(FakeProvider):
    """Proveedor falso que devuelve una intención fija."""

    def __init__(self, name: str, intent: Intent, **kwargs: object) -> None:
        super().__init__(name, **kwargs)  # type: ignore[arg-type]
        self.intent = intent
        self.prompts: list[str] = []

    async def classify_intent(
        self, text: str, history: list[ChatTurn], today: date, has_pending: bool
    ) -> Intent:
        await self._maybe_fail()
        self.prompts.append(text)
        return self.intent


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


# --- Rango de la semana (puro) ------------------------------------------------------------


def test_week_range_from_midweek_and_weekend() -> None:
    assert chat.week_range(MON) == (MON, date(2026, 9, 13))
    assert chat.week_range(WED) == (WED, date(2026, 9, 13))
    # Sábado y domingo miran ya a la semana siguiente.
    assert chat.week_range(SAT) == (date(2026, 9, 14), date(2026, 9, 20))
    assert chat.week_range(SUN) == (date(2026, 9, 14), date(2026, 9, 20))


# --- Clasificación ---------------------------------------------------------------------------


async def test_classify_logs_the_call_and_caches_the_result(settings: Settings) -> None:
    fake = IntentProvider("claude_sdk", Intent(action="help"))
    chain = providers(fake)

    first = await chat.classify(
        "¿qué sabes hacer?", [], has_pending=False, settings=settings, providers=chain
    )
    second = await chat.classify(
        "¿qué sabes hacer?", [], has_pending=False, settings=settings, providers=chain
    )

    assert (first.action, second.action) == ("help", "help")
    assert fake.calls == 1  # la segunda sale de la caché
    assert [c.provider for c in await repo.llm_calls("intent")] == ["claude_sdk", "cache"]


async def test_classify_key_depends_on_history_and_pending(settings: Settings) -> None:
    fake = IntentProvider("a", Intent(action="help"))
    chain = providers(fake)
    await chat.classify("hola", [], has_pending=False, settings=settings, providers=chain)
    await chat.classify(
        "hola",
        [ChatTurn(role="user", content="algo")],
        has_pending=False,
        settings=settings,
        providers=chain,
    )
    await chat.classify("hola", [], has_pending=True, settings=settings, providers=chain)
    assert fake.calls == 3  # ninguna reutiliza la anterior


async def test_classify_propagates_llm_error_and_logs_attempts(settings: Settings) -> None:
    fake = IntentProvider("a", Intent(action="help"), fail=LLMUnavailableError("caído"))
    with pytest.raises(LLMUnavailableError):
        await chat.classify(
            "hola", [], has_pending=False, settings=settings, providers=providers(fake)
        )
    assert [(c.provider, c.ok) for c in await repo.llm_calls("intent")] == [("a", False)]


# --- Despacho ---------------------------------------------------------------------------------


async def test_query_single_day(settings: Settings) -> None:
    await seed((TUE, "bring", "sudadera"), (TUE, "homework", "pág. 12"), (WED, "note", "otra"))
    reply = await chat.dispatch(
        Intent(action="query_range", date_from=TUE, date_to=TUE),
        today=MON,
        store=PendingStore(),
        chat_id=1,
    )
    assert "martes 8 de septiembre" in reply.text
    assert "🎒 Llevar: sudadera" in reply.text
    assert "📝 Tarea: pág. 12" in reply.text
    assert "otra" not in reply.text
    assert reply.edit is None


async def test_query_range_groups_by_day(settings: Settings) -> None:
    await seed((TUE, "bring", "sudadera"), (WED, "event", "izada"))
    reply = await chat.dispatch(
        Intent(action="query_range", date_from=MON, date_to=WED),
        today=MON,
        store=PendingStore(),
        chat_id=1,
    )
    assert reply.text.index("martes 8") < reply.text.index("miércoles 9")


async def test_query_empty_range() -> None:
    reply = await chat.dispatch(
        Intent(action="query_range", date_from=TUE, date_to=TUE),
        today=MON,
        store=PendingStore(),
        chat_id=1,
    )
    assert "No tengo nada" in reply.text


async def test_query_without_dates_uses_today() -> None:
    await seed((MON, "note", "hoy toca"))
    reply = await chat.dispatch(
        Intent(action="query_range"), today=MON, store=PendingStore(), chat_id=1
    )
    assert "hoy toca" in reply.text


async def test_add_entry_asks_for_confirmation() -> None:
    store = PendingStore()
    reply = await chat.dispatch(
        Intent(action="add_entry", date_from=TUE, kind="bring", text="disfraz"),
        today=MON,
        store=store,
        chat_id=1,
    )
    assert reply.edit is not None
    assert (reply.edit.action, reply.edit.entry_date, reply.edit.text) == ("add", TUE, "disfraz")
    assert "¿Agrego" in reply.text and "disfraz" in reply.text
    # Todavía no ha tocado la DB: nada se guarda sin confirmar.
    assert await repo.active_entries(TUE, TUE) == []


async def test_add_entry_without_data_asks_again() -> None:
    reply = await chat.dispatch(
        Intent(action="add_entry", text="disfraz"), today=MON, store=PendingStore(), chat_id=1
    )
    assert reply.edit is None
    assert "¿Para qué día" in reply.text


async def test_remove_single_candidate_asks_for_confirmation() -> None:
    await seed((WED, "event", "salida al parque"))
    reply = await chat.dispatch(
        Intent(action="remove_entry", date_from=WED, target_entry_hint="salida"),
        today=MON,
        store=PendingStore(),
        chat_id=1,
    )
    assert reply.edit is not None and reply.edit.action == "remove"
    assert reply.candidates is None
    assert "¿Quito" in reply.text
    assert (await repo.active_entries(WED, WED))[0].is_active is True


async def test_remove_several_candidates_offers_a_choice() -> None:
    await seed((WED, "bring", "sudadera"), (WED, "bring", "botella"), (WED, "note", "x"))
    reply = await chat.dispatch(
        Intent(action="remove_entry", date_from=WED),
        today=MON,
        store=PendingStore(),
        chat_id=1,
    )
    assert reply.candidates is not None and len(reply.candidates) == 3
    assert all(len(label) <= 60 for _, label in reply.candidates)
    assert "¿Cuál quito?" in reply.text


async def test_remove_hint_without_matches_falls_back_to_the_whole_day() -> None:
    await seed((WED, "bring", "sudadera"))
    reply = await chat.dispatch(
        Intent(action="remove_entry", date_from=WED, target_entry_hint="paraguas rojo"),
        today=MON,
        store=PendingStore(),
        chat_id=1,
    )
    assert reply.edit is not None and reply.edit.entry_id is not None


async def test_remove_with_nothing_there() -> None:
    reply = await chat.dispatch(
        Intent(action="remove_entry", date_from=WED), today=MON, store=PendingStore(), chat_id=1
    )
    assert "No encontré nada" in reply.text
    assert reply.edit is None


async def test_help_and_unknown() -> None:
    store = PendingStore()
    assert (
        "agenda escolar"
        in (await chat.dispatch(Intent(action="help"), today=MON, store=store, chat_id=1)).text
    )
    assert (
        "No te entendí"
        in (await chat.dispatch(Intent(action="unknown"), today=MON, store=store, chat_id=1)).text
    )

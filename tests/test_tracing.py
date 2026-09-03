"""Traza de las llamadas al LLM: prompt y respuesta guardados, retención y OTel apagado.

Lo que motivó esto: `llm_calls` decía que el refinado fallaba con `error_max_turns`, pero
no qué se le había mandado ni qué había contestado, que es lo que hacía falta para
entenderlo. Ahora eso queda en la fila y se ve en el admin.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.utils import timezone

from app.config import Settings
from app.db import repo
from app.llm.provider import FallbackProvider, LLMProviders, LLMUnavailableError
from app.llm.schemas import ExtractedEntry, ExtractionResult
from app.services import cache, chat, ingest
from tests.conftest import TENANT
from tests.test_ingest import fake_download
from tests.test_provider import FakeProvider

pytestmark = pytest.mark.django_db(transaction=True)

STRONG = ExtractionResult(
    entries=[
        ExtractedEntry(
            entry_date=date(2026, 9, 3), kind="bring", text="sudadera", confidence="high"
        )
    ],
    doubts=[],
    detected_language="es",
)


class TracingProvider(FakeProvider):
    """Proveedor falso que además rellena la traza, como hacen los de verdad."""

    async def _maybe_fail(self) -> None:
        self.last_prompt = "PROMPT: lee la agenda"
        self.last_response = {"entries": [], "nota": "respuesta cruda"}
        await super()._maybe_fail()


def chain(provider: FakeProvider) -> LLMProviders:
    return LLMProviders(
        vision=FallbackProvider("vision", provider, None, timeout_s=5),
        text=FallbackProvider("text", provider, None, timeout_s=5),
    )


async def run_photo(settings: Settings, provider: FakeProvider) -> None:
    await ingest.ingest_photo(
        file_id="f",
        user_id=111,
        display_name="Alejandro",
        chat_id=-100,
        download=fake_download,
        settings=settings,
        providers=chain(provider),
        child_id=TENANT.child_id,
        family_id=TENANT.family_id,
    )


async def test_a_successful_call_stores_prompt_and_response(settings: Settings) -> None:
    await run_photo(settings, TracingProvider("claude_sdk", result=STRONG))
    call = (await repo.llm_calls("vision"))[0]
    assert call.prompt == "PROMPT: lee la agenda"
    assert call.response == {"entries": [], "nota": "respuesta cruda"}


async def test_a_failed_call_also_stores_the_trace(settings: Settings) -> None:
    """Es justo cuando falla cuando hace falta ver qué se mandó y qué contestó."""
    provider = TracingProvider("claude_sdk", fail=LLMUnavailableError("error_max_turns"))
    with pytest.raises(ingest.IngestError):
        await run_photo(settings, provider)
    call = (await repo.llm_calls("vision"))[0]
    assert call.ok is False
    assert call.prompt == "PROMPT: lee la agenda"
    assert call.response == {"entries": [], "nota": "respuesta cruda"}
    assert call.error is not None and "error_max_turns" in call.error


async def test_the_trace_can_be_turned_off(settings: Settings) -> None:
    off = settings.model_copy(update={"llm_trace_enabled": False})
    await run_photo(off, TracingProvider("claude_sdk", result=STRONG))
    call = (await repo.llm_calls("vision"))[0]
    assert call.prompt is None and call.response is None
    # Las métricas se siguen registrando: apagar la traza no ciega el consumo.
    assert call.provider == "claude_sdk" and call.ok is True


async def test_a_cache_hit_has_no_trace_of_its_own(settings: Settings) -> None:
    """Un acierto de caché no llama al modelo, así que no hay prompt que guardar."""
    provider = TracingProvider("claude_sdk", result=STRONG)
    await run_photo(settings, provider)
    await run_photo(settings, provider)
    hit = next(c for c in await repo.llm_calls("vision") if c.provider == cache.CACHE_PROVIDER)
    assert hit.prompt is None and hit.response is None


async def test_intent_calls_are_traced_too(settings: Settings) -> None:
    provider = TracingProvider("claude_sdk")
    await chat.classify(
        "¿qué hay mañana?", [], has_pending=False, settings=settings, providers=chain(provider)
    )
    call = (await repo.llm_calls("intent"))[0]
    assert call.prompt == "PROMPT: lee la agenda"


async def test_retention_clears_the_payload_but_keeps_the_row(settings: Settings) -> None:
    await run_photo(settings, TracingProvider("claude_sdk", result=STRONG))
    call = (await repo.llm_calls("vision"))[0]

    # Todavía reciente: no se toca.
    assert await repo.purge_llm_traces(timezone.now() - timedelta(days=30)) == 0

    from app.db.models import LLMCall

    await LLMCall.objects.filter(pk=call.pk).aupdate(created_at=timezone.now() - timedelta(days=60))
    assert await repo.purge_llm_traces(timezone.now() - timedelta(days=30)) == 1

    refreshed = await LLMCall.objects.aget(pk=call.pk)
    assert refreshed.prompt is None and refreshed.response is None
    # La auditoría sobrevive: sigue habiendo fila, proveedor, tokens y duración.
    assert refreshed.provider == "claude_sdk"
    assert refreshed.input_tokens == 10


def test_otel_is_off_by_default(settings: Settings) -> None:
    """Sin OTEL_ENABLED no se instala nada y el span es un no-op."""
    from app import obs

    assert settings.otel_enabled is False
    assert obs.setup_tracing(settings) is False
    with obs.llm_span("vision", "claude_sdk", "sonnet") as span:
        assert span is None
    obs.record_usage(None, None, ok=True, error=None)  # no revienta con span None

"""Factory por config y cadena de fallback."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from app.llm.anthropic_api import AnthropicAPIProvider
from app.llm.claude_sdk import ClaudeSDKProvider
from app.llm.ollama import OllamaProvider
from app.llm.provider import (
    FallbackProvider,
    LLMQuotaError,
    LLMUnavailableError,
    build_provider,
    build_providers,
)
from app.llm.schemas import (
    ChatTurn,
    ExtractedEntry,
    ExtractionResult,
    Intent,
    LLMUsage,
    ProviderHealth,
    QAPair,
)
from tests.conftest import make_settings

TODAY = date(2026, 9, 1)


def entry(text: str = "sudadera", confidence: str = "high") -> ExtractedEntry:
    return ExtractedEntry(
        entry_date=date(2026, 9, 2),
        kind="bring",
        text=text,
        confidence=confidence,
    )


class FakeProvider:
    """Proveedor de prueba con comportamiento configurable."""

    def __init__(
        self,
        name: str,
        *,
        fail: Exception | None = None,
        delay: float = 0,
        result: ExtractionResult | None = None,
    ) -> None:
        self.name = name
        self.fail = fail
        self.delay = delay
        self.result = result or ExtractionResult(entries=[], doubts=[], detected_language="es")
        self.calls = 0
        self.last_usage: LLMUsage | None = None
        self.corrections: list[str] = []
        self.refinements: list[list[QAPair]] = []
        # Si se fija, `refine_extraction` la devuelve en vez de `result`.
        self.refined: ExtractionResult | None = None

    async def _maybe_fail(self) -> None:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        self.last_usage = LLMUsage(self.name, "fake-model", 10, 5, 0.001, 12)
        if self.fail:
            raise self.fail

    async def extract_from_image(self, image_path: Path, today: date) -> ExtractionResult:
        await self._maybe_fail()
        return self.result

    async def correct_extraction(
        self, extraction: ExtractionResult, correction: str, today: date
    ) -> ExtractionResult:
        await self._maybe_fail()
        self.corrections.append(correction)
        return self.result

    async def refine_extraction(
        self, extraction: ExtractionResult, pairs: list[QAPair], today: date
    ) -> ExtractionResult:
        await self._maybe_fail()
        self.refinements.append(list(pairs))
        return self.refined or self.result

    async def classify_intent(
        self, text: str, history: list[ChatTurn], today: date, has_pending: bool
    ) -> Intent:
        await self._maybe_fail()
        return Intent(action="help")

    async def healthcheck(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, ok=self.fail is None)


def test_build_provider_returns_each_implementation() -> None:
    s = make_settings(ollama_base_url="http://ollama:11434", anthropic_api_key="sk-ant-api-test")
    assert isinstance(build_provider("claude_sdk", s), ClaudeSDKProvider)
    assert isinstance(build_provider("ollama", s), OllamaProvider)
    assert isinstance(build_provider("anthropic_api", s), AnthropicAPIProvider)


def test_build_providers_shares_instances_and_names_chain() -> None:
    s = make_settings(
        llm_vision_provider="claude_sdk",
        llm_vision_fallback="ollama",
        llm_text_provider="ollama",
        ollama_base_url="http://ollama:11434",
    )
    providers = build_providers(s)
    assert providers.vision.name == "claude_sdk+ollama"
    assert providers.text.name == "ollama"
    assert providers.vision.fallback is providers.text.primary


async def test_fallback_used_when_primary_raises_llm_error() -> None:
    primary = FakeProvider("a", fail=LLMUnavailableError("caído"))
    fallback = FakeProvider("b")
    chain = FallbackProvider("text", primary, fallback, timeout_s=5)
    intent = await chain.classify_intent("hola", [], TODAY, False)
    assert intent.action == "help"
    assert chain.last_used == "b"
    assert primary.calls == 1 and fallback.calls == 1


async def test_fallback_used_on_timeout() -> None:
    primary = FakeProvider("a", delay=0.2)
    fallback = FakeProvider("b")
    chain = FallbackProvider("vision", primary, fallback, timeout_s=0.05)
    await chain.extract_from_image(Path("x.jpg"), TODAY)
    assert chain.last_used == "b"


async def test_error_propagates_without_fallback() -> None:
    chain = FallbackProvider("text", FakeProvider("a", fail=LLMUnavailableError("x")), None, 5)
    with pytest.raises(LLMUnavailableError):
        await chain.classify_intent("hola", [], TODAY, False)


async def test_not_implemented_is_not_masked_by_fallback() -> None:
    primary = FakeProvider("a", fail=NotImplementedError("Fase 3"))
    fallback = FakeProvider("b")
    chain = FallbackProvider("text", primary, fallback, timeout_s=5)
    with pytest.raises(NotImplementedError):
        await chain.classify_intent("hola", [], TODAY, False)
    assert fallback.calls == 0


async def test_primary_success_skips_fallback() -> None:
    primary = FakeProvider("a")
    fallback = FakeProvider("b")
    chain = FallbackProvider("text", primary, fallback, timeout_s=5)
    await chain.classify_intent("hola", [], TODAY, False)
    assert chain.last_used == "a" and fallback.calls == 0


# --- run(): intentos, usage y criterio de aceptación ---------------------------------


async def test_run_reports_every_attempt_with_usage() -> None:
    primary = FakeProvider("a", fail=LLMUnavailableError("caído"))
    fallback = FakeProvider(
        "b", result=ExtractionResult(entries=[entry()], doubts=[], detected_language="es")
    )
    chain = FallbackProvider("vision", primary, fallback, timeout_s=5)
    run = await chain.run(lambda p: p.extract_from_image(Path("x.jpg"), TODAY))
    assert run.provider == "b"
    assert [(a.provider, a.ok) for a in run.attempts] == [("a", False), ("b", True)]
    assert run.attempts[0].error == "caído"
    assert run.attempts[1].usage is not None and run.attempts[1].usage.model == "fake-model"


async def test_run_uses_fallback_when_result_is_weak() -> None:
    weak = ExtractionResult(entries=[entry(confidence="low")], doubts=["?"], detected_language="es")
    strong = ExtractionResult(entries=[entry()], doubts=[], detected_language="es")
    primary = FakeProvider("a", result=weak)
    fallback = FakeProvider("b", result=strong)
    chain = FallbackProvider("vision", primary, fallback, timeout_s=5)
    run = await chain.run(
        lambda p: p.extract_from_image(Path("x.jpg"), TODAY),
        accept=lambda r: bool(r.entries) and any(e.confidence != "low" for e in r.entries),
    )
    assert run.provider == "b" and run.value is strong
    assert all(a.ok for a in run.attempts) and len(run.attempts) == 2


async def test_run_keeps_weak_primary_result_if_fallback_fails() -> None:
    weak = ExtractionResult(entries=[], doubts=["nada"], detected_language="es")
    primary = FakeProvider("a", result=weak)
    fallback = FakeProvider("b", fail=LLMUnavailableError("caído"))
    chain = FallbackProvider("vision", primary, fallback, timeout_s=5)
    run = await chain.run(
        lambda p: p.extract_from_image(Path("x.jpg"), TODAY), accept=lambda r: bool(r.entries)
    )
    assert run.provider == "a" and run.value is weak
    assert [(a.provider, a.ok) for a in run.attempts] == [("a", True), ("b", False)]


async def test_run_raises_last_error_with_attempts_attached() -> None:
    primary = FakeProvider("a", fail=LLMUnavailableError("caído"))
    fallback = FakeProvider("b", fail=LLMQuotaError("límite"))
    chain = FallbackProvider("vision", primary, fallback, timeout_s=5)
    with pytest.raises(LLMQuotaError) as info:
        await chain.run(lambda p: p.extract_from_image(Path("x.jpg"), TODAY))
    assert [a.provider for a in info.value.attempts] == ["a", "b"]
    assert info.value.attempts[0].exception is not None


async def test_run_timeout_is_reported_as_unavailable() -> None:
    chain = FallbackProvider("vision", FakeProvider("a", delay=0.2), None, timeout_s=0.05)
    with pytest.raises(LLMUnavailableError, match="timeout") as info:
        await chain.run(lambda p: p.extract_from_image(Path("x.jpg"), TODAY))
    assert info.value.attempts[0].error is not None and "timeout" in info.value.attempts[0].error

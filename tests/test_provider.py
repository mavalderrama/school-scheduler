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
    LLMUnavailableError,
    build_provider,
    build_providers,
)
from app.llm.schemas import ChatTurn, ExtractionResult, Intent, ProviderHealth
from tests.conftest import make_settings

TODAY = date(2026, 9, 1)


class FakeProvider:
    """Proveedor de prueba con comportamiento configurable."""

    def __init__(self, name: str, *, fail: Exception | None = None, delay: float = 0) -> None:
        self.name = name
        self.fail = fail
        self.delay = delay
        self.calls = 0

    async def _maybe_fail(self) -> None:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail

    async def extract_from_image(self, image_path: Path, today: date) -> ExtractionResult:
        await self._maybe_fail()
        return ExtractionResult(entries=[], doubts=[], detected_language="es")

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

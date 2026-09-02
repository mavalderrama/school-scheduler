"""Interfaz común de proveedores de LLM, factory por config y cadena de fallback.

Regla de oro: el modelo nunca ejecuta nada. Los proveedores solo devuelven JSON
validado con los modelos de `schemas.py`; la lógica corre en `app/services`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar

from app.config import ProviderName, Settings
from app.llm.schemas import ChatTurn, ExtractionResult, Intent, ProviderHealth
from app.log import get_logger

if TYPE_CHECKING:
    pass

log = get_logger(__name__)

Task = Literal["vision", "text"]
T = TypeVar("T")


class LLMError(Exception):
    """Error controlado de un proveedor. La cadena de fallback lo intercepta."""


class LLMUnavailableError(LLMError):
    """El proveedor no responde (red, subproceso, autenticación, timeout)."""


class LLMQuotaError(LLMError):
    """Límite de uso o cuota agotada. No reintentar en bucle; reencolar o usar fallback."""


class LLMOutputError(LLMError):
    """El modelo respondió pero el JSON no valida contra el schema tras reintentar."""


class LLMProvider(Protocol):
    """Contrato que cumplen OllamaProvider, ClaudeSDKProvider y AnthropicAPIProvider."""

    name: str

    async def extract_from_image(self, image_path: Path, today: date) -> ExtractionResult: ...

    async def classify_intent(
        self,
        text: str,
        history: list[ChatTurn],
        today: date,
        has_pending: bool,
    ) -> Intent: ...

    async def healthcheck(self) -> ProviderHealth: ...


def build_provider(name: ProviderName, settings: Settings) -> LLMProvider:
    """Instancia un proveedor por nombre. Importa perezosamente para no exigir extras."""
    if name == "ollama":
        from app.llm.ollama import OllamaProvider

        return OllamaProvider(settings)
    if name == "claude_sdk":
        from app.llm.claude_sdk import ClaudeSDKProvider

        return ClaudeSDKProvider(settings)
    if name == "anthropic_api":
        from app.llm.anthropic_api import AnthropicAPIProvider

        return AnthropicAPIProvider(settings)
    raise ValueError(f"proveedor desconocido: {name!r}")


class FallbackProvider:
    """Encadena un proveedor principal con uno de respaldo y aplica timeouts por tarea.

    Si el principal lanza `LLMError` (o se agota el timeout) se registra y se intenta
    el fallback. `NotImplementedError` se propaga: es un stub, no un fallo del servicio.
    """

    def __init__(
        self,
        task: Task,
        primary: LLMProvider,
        fallback: LLMProvider | None,
        timeout_s: float,
    ) -> None:
        self.task = task
        self.primary = primary
        self.fallback = fallback
        self.timeout_s = timeout_s
        self.name = primary.name if fallback is None else f"{primary.name}+{fallback.name}"
        self.last_used: str | None = None

    @property
    def providers(self) -> list[LLMProvider]:
        return [self.primary] if self.fallback is None else [self.primary, self.fallback]

    async def _run(self, call: Callable[[LLMProvider], Awaitable[T]]) -> T:
        try:
            async with asyncio.timeout(self.timeout_s):
                result = await call(self.primary)
            self.last_used = self.primary.name
            return result
        except (LLMError, TimeoutError) as exc:
            if self.fallback is None:
                raise
            log.warning(
                "llm_primary_failed",
                task=self.task,
                provider=self.primary.name,
                fallback=self.fallback.name,
                error=str(exc),
            )
        async with asyncio.timeout(self.timeout_s):
            result = await call(self.fallback)
        self.last_used = self.fallback.name
        return result

    async def extract_from_image(self, image_path: Path, today: date) -> ExtractionResult:
        return await self._run(lambda p: p.extract_from_image(image_path, today))

    async def classify_intent(
        self,
        text: str,
        history: list[ChatTurn],
        today: date,
        has_pending: bool,
    ) -> Intent:
        return await self._run(lambda p: p.classify_intent(text, history, today, has_pending))

    async def healthcheck(self) -> ProviderHealth:
        return await self.primary.healthcheck()


@dataclass
class LLMProviders:
    """Proveedores efectivos por tarea, ya con su cadena de fallback."""

    vision: FallbackProvider
    text: FallbackProvider


def build_providers(settings: Settings) -> LLMProviders:
    """Construye las cadenas de visión y texto según la configuración."""
    cache: dict[str, LLMProvider] = {}

    def get(name: str) -> LLMProvider:
        if name not in cache:
            cache[name] = build_provider(name, settings)  # type: ignore[arg-type]
        return cache[name]

    vision_fallback = (
        None if settings.llm_vision_fallback == "none" else get(settings.llm_vision_fallback)
    )
    text_fallback = (
        None if settings.llm_text_fallback == "none" else get(settings.llm_text_fallback)
    )
    return LLMProviders(
        vision=FallbackProvider(
            "vision",
            get(settings.llm_vision_provider),
            vision_fallback,
            settings.llm_vision_timeout,
        ),
        text=FallbackProvider(
            "text", get(settings.llm_text_provider), text_fallback, settings.llm_text_timeout
        ),
    )

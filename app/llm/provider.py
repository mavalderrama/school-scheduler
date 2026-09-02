"""Interfaz común de proveedores de LLM, factory por config y cadena de fallback.

Regla de oro: el modelo nunca ejecuta nada. Los proveedores solo devuelven JSON
validado con los modelos de `schemas.py`; la lógica corre en `app/services`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, Protocol

from app.config import ProviderName, Settings
from app.llm.schemas import ChatTurn, ExtractionResult, Intent, LLMUsage, ProviderHealth
from app.log import get_logger

log = get_logger(__name__)

Task = Literal["vision", "text"]


class LLMError(Exception):
    """Error controlado de un proveedor. La cadena de fallback lo intercepta.

    `attempts` lo rellena `FallbackProvider` al relanzar, para registrar en `llm_calls`.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.attempts: list[LLMAttempt] = []


class LLMUnavailableError(LLMError):
    """El proveedor no responde (red, subproceso, autenticación, timeout)."""


class LLMQuotaError(LLMError):
    """Límite de uso o cuota agotada. No reintentar en bucle; reencolar o usar fallback."""


class LLMOutputError(LLMError):
    """El modelo respondió pero el JSON no valida contra el schema tras reintentar."""


class LLMProvider(Protocol):
    """Contrato que cumplen OllamaProvider, ClaudeSDKProvider y AnthropicAPIProvider.

    `last_usage` guarda el consumo de la última llamada; `FallbackProvider` lo lee bajo
    un lock justo después de cada llamada para registrarlo en `llm_calls`.
    """

    name: str
    last_usage: LLMUsage | None

    async def extract_from_image(self, image_path: Path, today: date) -> ExtractionResult: ...

    async def correct_extraction(
        self, extraction: ExtractionResult, correction: str, today: date
    ) -> ExtractionResult: ...

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


@dataclass(frozen=True)
class LLMAttempt:
    """Un intento contra un proveedor, para `llm_calls`."""

    provider: str
    ok: bool
    error: str | None
    usage: LLMUsage | None
    duration_ms: int
    exception: LLMError | None = None


@dataclass
class LLMRun[R]:
    """Resultado de la cadena: el valor, quién lo produjo y todos los intentos."""

    value: R
    provider: str
    attempts: list[LLMAttempt] = field(default_factory=list)


class FallbackProvider:
    """Encadena un proveedor principal con uno de respaldo y aplica timeouts por tarea.

    Si el principal lanza `LLMError` (o se agota el timeout) se registra y se intenta
    el fallback. Con `accept` se puede pedir fallback también cuando el principal responde
    pero el resultado es débil (p. ej. visión sin entradas). `NotImplementedError` se
    propaga: es un stub, no un fallo del servicio.
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
        # Serializa las llamadas de la cadena: `last_usage` del proveedor se lee tras cada
        # llamada y las llamadas al LLM son lentas y una a la vez por diseño.
        self._lock = asyncio.Lock()

    @property
    def providers(self) -> list[LLMProvider]:
        return [self.primary] if self.fallback is None else [self.primary, self.fallback]

    async def _attempt[T](
        self, provider: LLMProvider, call: Callable[[LLMProvider], Awaitable[T]]
    ) -> tuple[T | None, LLMAttempt]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        provider.last_usage = None
        try:
            async with asyncio.timeout(self.timeout_s):
                value = await call(provider)
        except (LLMError, TimeoutError) as exc:
            duration = int((loop.time() - started) * 1000)
            error: LLMError = (
                LLMUnavailableError(f"{provider.name}: timeout tras {self.timeout_s:.0f}s")
                if isinstance(exc, TimeoutError)
                else exc
            )
            return None, LLMAttempt(
                provider.name, False, str(error), provider.last_usage, duration, error
            )
        duration = int((loop.time() - started) * 1000)
        return value, LLMAttempt(provider.name, True, None, provider.last_usage, duration)

    async def run[T](
        self,
        call: Callable[[LLMProvider], Awaitable[T]],
        *,
        accept: Callable[[T], bool] | None = None,
    ) -> LLMRun[T]:
        """Ejecuta la cadena y devuelve valor + intentos. Lanza el error del último intento."""
        async with self._lock:
            attempts: list[LLMAttempt] = []
            value, attempt = await self._attempt(self.primary, call)
            attempts.append(attempt)
            primary_value = value
            accepted = attempt.ok and (accept is None or accept(value))  # type: ignore[arg-type]
            if accepted or self.fallback is None:
                if not attempt.ok:
                    raise self._error(attempt, attempts)
                self.last_used = self.primary.name
                return LLMRun(value, self.primary.name, attempts)  # type: ignore[arg-type]

            log.warning(
                "llm_primary_failed",
                task=self.task,
                provider=self.primary.name,
                fallback=self.fallback.name,
                error=attempt.error or "resultado débil",
            )
            value, attempt = await self._attempt(self.fallback, call)
            attempts.append(attempt)
            if attempt.ok:
                self.last_used = self.fallback.name
                return LLMRun(value, self.fallback.name, attempts)  # type: ignore[arg-type]
            if primary_value is not None:
                # El principal respondió (débil) y el fallback falló: vale lo del principal.
                self.last_used = self.primary.name
                return LLMRun(primary_value, self.primary.name, attempts)
            raise self._error(attempt, attempts)

    @staticmethod
    def _error(attempt: LLMAttempt, attempts: list[LLMAttempt]) -> LLMError:
        error = attempt.exception or LLMUnavailableError(attempt.error or "error desconocido")
        error.attempts = attempts
        return error

    async def extract_from_image(self, image_path: Path, today: date) -> ExtractionResult:
        run = await self.run(lambda p: p.extract_from_image(image_path, today))
        return run.value

    async def correct_extraction(
        self, extraction: ExtractionResult, correction: str, today: date
    ) -> ExtractionResult:
        run = await self.run(lambda p: p.correct_extraction(extraction, correction, today))
        return run.value

    async def classify_intent(
        self,
        text: str,
        history: list[ChatTurn],
        today: date,
        has_pending: bool,
    ) -> Intent:
        run = await self.run(lambda p: p.classify_intent(text, history, today, has_pending))
        return run.value

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

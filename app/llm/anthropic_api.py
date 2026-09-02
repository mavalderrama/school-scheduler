"""Proveedor Claude por API key (pago por uso) con el SDK oficial `anthropic`."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from anthropic import APIConnectionError, APIStatusError, AsyncAnthropic

from app.config import Settings
from app.llm.schemas import ChatTurn, ExtractionResult, Intent, ProviderHealth


class AnthropicAPIProvider:
    name = "anthropic_api"

    def __init__(self, settings: Settings) -> None:
        if not settings.anthropic_api_key:
            raise ValueError("AnthropicAPIProvider requiere ANTHROPIC_API_KEY")
        self.model = settings.anthropic_api_model
        # La key se pasa explícitamente: nunca depende del entorno del proceso.
        self._client = AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            timeout=float(settings.llm_vision_timeout),
            max_retries=2,
        )

    async def extract_from_image(self, image_path: Path, today: date) -> ExtractionResult:
        raise NotImplementedError("Fase 1")

    async def classify_intent(
        self,
        text: str,
        history: list[ChatTurn],
        today: date,
        has_pending: bool,
    ) -> Intent:
        raise NotImplementedError("Fase 3")

    async def healthcheck(self) -> ProviderHealth:
        """Valida la key y que el modelo configurado existe, sin gastar tokens."""
        started = time.monotonic()
        try:
            info = await self._client.with_options(timeout=10.0).models.retrieve(self.model)
        except APIStatusError as exc:
            return ProviderHealth(
                name=self.name,
                ok=False,
                detail=f"HTTP {exc.status_code}: {exc.message}",
                model=self.model,
            )
        except APIConnectionError as exc:
            return ProviderHealth(name=self.name, ok=False, detail=f"sin conexión: {exc}")
        latency = int((time.monotonic() - started) * 1000)
        return ProviderHealth(
            name=self.name,
            ok=True,
            detail=f"modelo {info.id} ({info.display_name})",
            model=info.id,
            latency_ms=latency,
        )

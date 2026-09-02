"""Proveedor de modelo abierto self-hosted (Ollama) vía su API compatible con OpenAI."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from app.config import Settings
from app.llm.schemas import ChatTurn, ExtractionResult, Intent, ProviderHealth


class OllamaProvider:
    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        if not settings.ollama_base_url:
            raise ValueError("OllamaProvider requiere OLLAMA_BASE_URL")
        base = settings.ollama_base_url.rstrip("/")
        self.vision_model = settings.ollama_vision_model
        self.text_model = settings.ollama_text_model
        self._client = AsyncOpenAI(
            base_url=f"{base}/v1",
            api_key="ollama",  # Ollama no valida la key pero el SDK exige una
            timeout=float(settings.llm_vision_timeout),
            max_retries=0,
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
        """Comprueba que Ollama responde y que los dos modelos configurados están descargados."""
        started = time.monotonic()
        try:
            page = await self._client.with_options(timeout=10.0).models.list()
        except (APIConnectionError, APIStatusError) as exc:
            return ProviderHealth(name=self.name, ok=False, detail=f"sin conexión: {exc}")
        latency = int((time.monotonic() - started) * 1000)
        available = {m.id for m in page.data}
        missing = [m for m in (self.vision_model, self.text_model) if m not in available]
        if missing:
            return ProviderHealth(
                name=self.name,
                ok=False,
                detail=f"faltan modelos en Ollama: {', '.join(missing)} (ollama pull ...)",
                latency_ms=latency,
            )
        return ProviderHealth(
            name=self.name,
            ok=True,
            detail=f"modelos disponibles: {self.vision_model}, {self.text_model}",
            model=f"{self.vision_model} / {self.text_model}",
            latency_ms=latency,
        )

"""Contratos del LLM (sección 6 del plan). Todo lo que devuelve un modelo pasa por aquí."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

EntryKind = Literal["bring", "homework", "event", "note"]


class ExtractedEntry(BaseModel):
    """Una entrada de agenda leída de una foto."""

    entry_date: date = Field(description="Fecha absoluta, resuelta por el modelo usando 'hoy'")
    kind: EntryKind
    text: str = Field(description="Conciso, sin repetir la fecha")
    confidence: Literal["high", "medium", "low"]


class ExtractionResult(BaseModel):
    """Resultado de `extract_from_image`."""

    entries: list[ExtractedEntry]
    doubts: list[str] = Field(description="Lo que no se pudo leer o es ambiguo")
    detected_language: str


IntentAction = Literal[
    "query_range",
    "add_entry",
    "remove_entry",
    "confirm",
    "reject",
    "correct_pending",
    "help",
    "unknown",
]


class Intent(BaseModel):
    """Resultado de `classify_intent`. El modelo solo clasifica; Python ejecuta."""

    action: IntentAction
    date_from: date | None = None
    date_to: date | None = None
    kind: EntryKind | None = None
    text: str | None = None
    target_entry_hint: str | None = Field(
        default=None, description="Para remove: 'lo del jueves', 'el disfraz'"
    )


class ChatTurn(BaseModel):
    """Un turno del historial corto que viaja en el prompt de intención."""

    role: Literal["user", "assistant"]
    content: str


class OkProbe(BaseModel):
    """Salida mínima para el healthcheck real: el modelo debe responder {"ok": true}."""

    ok: bool


@dataclass(frozen=True)
class ProviderHealth:
    """Resultado de `healthcheck()` de un proveedor."""

    name: str
    ok: bool
    detail: str = ""
    model: str | None = None
    latency_ms: int | None = None


@dataclass(frozen=True)
class LLMUsage:
    """Consumo de una llamada, para registrar en `llm_calls` (Fase 1).

    Los dos campos de caché van al final y con default: hay construcción posicional
    en los tests. Solo los reportan claude_sdk y anthropic_api.
    """

    provider: str
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    duration_ms: int
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

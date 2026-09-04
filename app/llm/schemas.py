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


WEEKDAY_LABELS = {1: "lunes", 2: "martes", 3: "miércoles", 4: "jueves", 5: "viernes"}


class SlotDraft(BaseModel):
    """Una fila de la tabla de un horario rotativo."""

    week_label: str = Field(description="Etiqueta de la semana tal cual aparece: 'A', 'B'")
    weekday: int = Field(ge=1, le=7, description="ISO: 1 lunes ... 5 viernes")
    rotation: str | None = Field(
        default=None, description="Número o nombre de la rotación; puede no ser numérico"
    )
    subject: str = Field(description="Materia o actividad")


class ScheduleDraft(BaseModel):
    """Horario rotativo leído de una foto, antes de resolver lo que falta.

    `anchor_monday` casi nunca está en la imagen: es lo que el bot pregunta después.
    """

    name: str | None = Field(default=None, description="Título del horario si aparece")
    cycle_weeks: int = Field(default=2, ge=1, le=8, description="Semanas distintas del ciclo")
    slots: list[SlotDraft] = Field(default_factory=list)
    anchor_monday: date | None = Field(
        default=None, description="Lunes en que empezó la primera semana del ciclo"
    )


DocType = Literal["agenda", "schedule"]


class ExtractionResult(BaseModel):
    """Resultado de `extract_from_image`.

    `doc_type` decide qué se guarda: entradas por fecha o un horario rotativo. Tiene
    default para que el contrato de la Fase 1 siga valiendo tal cual.
    """

    entries: list[ExtractedEntry] = Field(default_factory=list)
    doubts: list[str] = Field(description="Lo que no se pudo leer o es ambiguo")
    detected_language: str
    doc_type: DocType = "agenda"
    schedule: ScheduleDraft | None = None
    questions: list[str] = Field(
        default_factory=list,
        description="Preguntas concretas que harían falta para poder guardar esto",
    )


class QAPair(BaseModel):
    """Una pregunta del bot y la respuesta del usuario, para `refine_extraction`."""

    question: str
    answer: str


ReminderRepeat = Literal["once", "daily", "weekly"]


IntentAction = Literal[
    "query_range",
    "query_subject",
    "add_entry",
    "add_recurring",
    "remove_entry",
    "add_reminder",
    "list_reminders",
    "remove_reminder",
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
    subject: str | None = Field(
        default=None, description="Para query_subject: la materia por la que preguntan"
    )
    # La hora va como texto con patrón, no como `time`: es el formato que ya usa la
    # configuración (HH:MM) y el que los cuatro proveedores pasan igual en el JSON schema.
    # Quien la convierte es Python.
    time_of_day: str | None = Field(
        default=None,
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="Para los recordatorios: hora local en 24h HH:MM. Null si no la dijeron",
    )
    repeat: ReminderRepeat | None = Field(
        default=None, description="Para add_reminder: cada cuánto se repite"
    )
    weekdays: list[int] | None = Field(
        default=None,
        description=(
            "Días que se repiten, ISO 1=lunes ... 7=domingo. Para repeat='weekly' y para "
            "add_recurring"
        ),
    )
    only_school_days: bool | None = Field(
        default=None, description="True solo si piden que sea únicamente los días de colegio"
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

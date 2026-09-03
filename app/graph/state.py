"""Estado del grafo de conversación y contexto de ejecución.

Dos reglas que mandan sobre todo lo demás en este paquete:

1. **El estado es JSON plano.** Los modelos pydantic entran y salen con
   `model_dump(mode="json")` / `model_validate`, no se guardan como objetos. Así no
   dependemos del serializador del checkpointer y el estado se puede leer a ojo en la DB
   cuando algo va mal.
2. **Lo que no es serializable no va en el estado**, va en el contexto de la invocación
   (`Runtime[GraphContext]`), que LangGraph no persiste: settings, proveedores de LLM y la
   función de descarga. El grafo **no importa aiogram en ningún sitio**.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from app.config import Settings
from app.llm.provider import LLMProviders

Downloader = Callable[[str, Path], Awaitable[None]]

Flow = Literal["photo", "edit"]
Decision = Literal["confirm", "reject", "correct", "add", "replace"]


@dataclass
class GraphContext:
    """Lo que los nodos necesitan y no se puede (ni se debe) persistir."""

    settings: Settings
    providers: LLMProviders
    download: Downloader


class GraphState(TypedDict, total=False):
    """Estado conversacional de un chat. `thread_id` del grafo = `str(chat_id)`."""

    chat_id: int
    flow: Flow

    # --- Flujo de la foto ---
    photo: dict[str, Any] | None
    """`QueuedPhoto` serializada: la foto que se está procesando ahora."""
    source_id: int | None
    extraction: dict[str, Any] | None
    """`ExtractionResult` serializada; se rehidrata en cada nodo que la necesita."""
    questions: list[str]
    answers: list[dict[str, str]]
    """`QAPair` serializados, en el orden en que se preguntaron."""
    attempts: int
    gave_up: bool
    """Se agotaron las rondas de preguntas: se avisa una vez y se pide confirmar igual."""

    # --- Flujo de alta/baja por texto ---
    edit: dict[str, Any] | None
    user_id: int | None

    # --- Común ---
    queue: list[dict[str, Any]]
    """Fotos que llegaron mientras había algo pendiente. Antes se perdían al reiniciar."""
    reply: str | None
    """Texto final para el chat; lo envía el runner, nunca un nodo."""
    error: str | None
    decision: dict[str, Any] | None
    """Lo que respondió el usuario al `interrupt` de confirmación."""
    cancel: bool
    """El usuario dijo «descarta» durante una pregunta o una corrección."""

"""Ciclo de confirmación: una confirmación pendiente por chat y cola de fotos en espera.

Lo pendiente puede ser una foto (`PendingPhoto`, con su ciclo ✅/✏️/❌), una edición por
texto (`PendingEdit`, alta o baja con ✅/❌) o un interrogatorio (`PendingQuestions`: al bot
le faltan datos esenciales y los pregunta antes de guardar nada). Solo hay una cosa a la vez
por chat, como pide el plan; las fotos que lleguen mientras tanto esperan en cola.

Estado en memoria del proceso. Si el bot se reinicia con una confirmación pendiente, la
source queda `pending` en la DB y el usuario vuelve a mandar la foto (Fase 4 persiste esto).
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from app.llm.schemas import ExtractionResult, QAPair


@dataclass(frozen=True)
class QueuedPhoto:
    """Foto que llegó mientras había una confirmación pendiente.

    `caption` es lo que el usuario escribió junto a la foto en Telegram: contexto para
    leerla («márcalo como PAC horario extendido»), no una orden para el modelo.
    """

    file_id: str
    user_id: int
    display_name: str
    caption: str | None = None


@dataclass
class PendingPhoto:
    """Extracción a la espera de ✅ / ✏️ / ❌."""

    source_id: int
    chat_id: int
    extraction: ExtractionResult
    awaiting_correction: bool = False
    summary_message_id: int | None = None


@dataclass
class PendingEdit:
    """Alta o baja pedida por texto, a la espera de ✅ / ❌."""

    edit_id: int
    chat_id: int
    action: Literal["add", "remove"]
    entry_date: date
    kind: str | None = None  # solo en `add`
    text: str | None = None  # solo en `add`
    entry_id: int | None = None  # solo en `remove`


@dataclass
class PendingQuestions:
    """Extracción incompleta: el bot pregunta lo que falta antes de guardar.

    `attempts` corta el bucle: si dos rondas de respuestas no resuelven lo esencial, el bot
    lo dice en vez de seguir preguntando lo mismo.
    """

    source_id: int
    chat_id: int
    extraction: ExtractionResult
    questions: list[str]
    answers: list[QAPair] = field(default_factory=list)
    attempts: int = 0

    @property
    def current(self) -> str | None:
        """La pregunta que toca, o None si ya se respondieron todas las de esta ronda."""
        asked = len(self.answers)
        return self.questions[asked] if asked < len(self.questions) else None

    def answer(self, text: str) -> None:
        question = self.current
        if question is not None:
            self.answers.append(QAPair(question=question, answer=text))

    @property
    def complete(self) -> bool:
        return self.current is None


Pending = PendingPhoto | PendingEdit | PendingQuestions


@dataclass
class PendingStore:
    _pending: dict[int, Pending] = field(default_factory=dict)
    _queues: dict[int, deque[QueuedPhoto]] = field(default_factory=dict)
    _edit_ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def get(self, chat_id: int) -> Pending | None:
        return self._pending.get(chat_id)

    def photo(self, chat_id: int) -> PendingPhoto | None:
        """Lo pendiente solo si es una foto (para el ciclo de corrección)."""
        current = self._pending.get(chat_id)
        return current if isinstance(current, PendingPhoto) else None

    def questions(self, chat_id: int) -> PendingQuestions | None:
        """Lo pendiente solo si es un interrogatorio abierto."""
        current = self._pending.get(chat_id)
        return current if isinstance(current, PendingQuestions) else None

    def set(self, pending: Pending) -> None:
        self._pending[pending.chat_id] = pending

    def new_edit_id(self) -> int:
        """Identificador incremental: detecta botones de una edición ya resuelta."""
        return next(self._edit_ids)

    def clear(self, chat_id: int) -> QueuedPhoto | None:
        """Cierra la confirmación del chat y devuelve la siguiente foto en cola, si hay."""
        self._pending.pop(chat_id, None)
        queue = self._queues.get(chat_id)
        if queue:
            return queue.popleft()
        return None

    def enqueue(self, chat_id: int, photo: QueuedPhoto) -> int:
        """Encola una foto; devuelve la posición en la cola (1 = la siguiente)."""
        queue = self._queues.setdefault(chat_id, deque())
        queue.append(photo)
        return len(queue)

    def queued(self, chat_id: int) -> int:
        return len(self._queues.get(chat_id, ()))

"""Ciclo de confirmación: una confirmación pendiente por chat y cola de fotos en espera.

Estado en memoria del proceso. Si el bot se reinicia con una confirmación pendiente, la
source queda `pending` en la DB y el usuario vuelve a mandar la foto (Fase 4 persiste esto).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from app.llm.schemas import ExtractionResult


@dataclass(frozen=True)
class QueuedPhoto:
    """Foto que llegó mientras había una confirmación pendiente."""

    file_id: str
    user_id: int
    display_name: str


@dataclass
class Pending:
    """Extracción a la espera de ✅ / ✏️ / ❌."""

    source_id: int
    chat_id: int
    extraction: ExtractionResult
    awaiting_correction: bool = False
    summary_message_id: int | None = None


@dataclass
class PendingStore:
    _pending: dict[int, Pending] = field(default_factory=dict)
    _queues: dict[int, deque[QueuedPhoto]] = field(default_factory=dict)

    def get(self, chat_id: int) -> Pending | None:
        return self._pending.get(chat_id)

    def set(self, pending: Pending) -> None:
        self._pending[pending.chat_id] = pending

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

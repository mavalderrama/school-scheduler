"""Ciclo de confirmación: una confirmación pendiente por chat y cola de fotos en espera.

Lo pendiente puede ser una foto (`PendingPhoto`, con su ciclo ✅/✏️/❌) o una edición por
texto (`PendingEdit`, alta o baja con ✅/❌). Solo hay una a la vez por chat, como pide el
plan; las fotos que lleguen mientras tanto esperan en cola.

Estado en memoria del proceso. Si el bot se reinicia con una confirmación pendiente, la
source queda `pending` en la DB y el usuario vuelve a mandar la foto (Fase 4 persiste esto).
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from app.llm.schemas import ExtractionResult


@dataclass(frozen=True)
class QueuedPhoto:
    """Foto que llegó mientras había una confirmación pendiente."""

    file_id: str
    user_id: int
    display_name: str


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


Pending = PendingPhoto | PendingEdit


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

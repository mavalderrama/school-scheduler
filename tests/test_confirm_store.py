"""Estado de confirmación por chat y cola de fotos."""

from __future__ import annotations

from app.llm.schemas import ExtractionResult
from app.services.confirm import Pending, PendingStore, QueuedPhoto

EMPTY = ExtractionResult(entries=[], doubts=[], detected_language="es")


def test_one_pending_per_chat_and_queue_order() -> None:
    store = PendingStore()
    assert store.get(1) is None
    store.set(Pending(source_id=10, chat_id=1, extraction=EMPTY))
    assert store.get(1) is not None and store.get(2) is None

    assert store.enqueue(1, QueuedPhoto("f1", 111, "Mamá")) == 1
    assert store.enqueue(1, QueuedPhoto("f2", 222, "Papá")) == 2
    assert store.queued(1) == 2

    nxt = store.clear(1)
    assert store.get(1) is None
    assert nxt is not None and nxt.file_id == "f1"
    assert store.clear(1) is not None
    assert store.clear(1) is None

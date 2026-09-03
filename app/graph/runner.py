"""Puente entre el transporte (aiogram) y el grafo.

Es el único sitio que conoce las dos cosas. El grafo devuelve *qué* hay que mandar en el
valor del `interrupt()`; aquí se traduce a mensajes y teclados de Telegram. Así ningún nodo
manda nada y reanudar no reenvía nada.

Tres cosas que no son opcionales, verificadas contra el paquete:

- **Un lock por chat.** LangGraph no serializa las invocaciones sobre un mismo `thread_id`:
  dos updates a la vez se pisarían el checkpoint. El lock del propio saver protege la fila,
  no la ejecución.
- **Comprobar que el hilo está interrumpido antes de reanudar.** `Command(resume=...)` en
  un hilo que no espera nada **no falla**: deja el valor guardado y se lo come la siguiente
  pregunta. Un ✅ pulsado dos veces envenenaría la conversación siguiente.
- **`durability="sync"`**: el checkpoint se confirma antes de seguir, que es justo lo que
  hace falta cuando el proceso puede morir en cualquier momento.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from app.graph.build import Graph
from app.graph.state import GraphContext, GraphState
from app.log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Ask:
    """Lo que el grafo quiere preguntar o mostrar. El handler lo convierte en un mensaje."""

    kind: str
    text: str | None = None
    source_id: int | None = None
    schedules: list[tuple[int, str]] | None = None
    gave_up: bool = False
    edit: dict[str, Any] | None = None


@dataclass(frozen=True)
class Turn:
    """Resultado de invocar o reanudar: o el bot pregunta algo, o ya tiene una respuesta."""

    ask: Ask | None = None
    reply: str | None = None
    error: str | None = None
    finished: bool = False


class GraphRunner:
    """Invoca y reanuda el grafo, con un lock por chat."""

    def __init__(
        self,
        graph: Graph,
        context: GraphContext,
        saver: BaseCheckpointSaver[Any] | None = None,
        ttl_hours: int = 24,
    ) -> None:
        self._graph = graph
        self._context = context
        self._saver = saver
        self._ttl = timedelta(hours=ttl_hours)
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _expired(self, snap: Any) -> bool:
        """Una conversación abandonada caduca: no debe revivir días después."""
        created = getattr(snap, "created_at", None)
        if not created:
            return False
        when = datetime.fromisoformat(created) if isinstance(created, str) else created
        return datetime.now(UTC) - when > self._ttl

    def _config(self, chat_id: int) -> Any:
        return {"configurable": {"thread_id": f"chat:{chat_id}"}, "recursion_limit": 40}

    # --- Lectura del estado ---------------------------------------------------------------

    async def snapshot(self, chat_id: int) -> GraphState | None:
        """Estado guardado del chat, o None si no hay hilo. Sustituye a `PendingStore.get`."""
        snap = await self._graph.aget_state(self._config(chat_id))
        if not snap.created_at:
            return None
        return cast(GraphState, snap.values)

    async def is_waiting(self, chat_id: int) -> bool:
        """¿El bot está esperando una respuesta de este chat?

        Se mira `next` (tareas pendientes) y no `interrupts`: encolar una foto con
        `aupdate_state` **vacía `interrupts`** aunque el hilo siga esperando de verdad, y
        reanudar sigue funcionando. `next` vacío es la única señal fiable de «ya terminó».
        """
        snap = await self._graph.aget_state(self._config(chat_id))
        return bool(snap.next) and not self._expired(snap)

    async def pending_ask(self, chat_id: int) -> Ask | None:
        """Qué está esperando ahora mismo, para `/pendiente`."""
        snap = await self._graph.aget_state(self._config(chat_id))
        if not snap.interrupts:
            return None
        return _to_ask(snap.interrupts[0].value)

    async def forget(self, chat_id: int) -> None:
        """Olvida el hilo de este chat. Para limpieza por caducidad."""
        if self._saver is not None:
            await self._saver.adelete_thread(f"chat:{chat_id}")

    # --- Invocación -----------------------------------------------------------------------

    async def start(self, chat_id: int, state: GraphState) -> Turn:
        """Arranca un flujo nuevo en este chat."""
        async with self._locks[chat_id]:
            return await self._run(chat_id, state)

    async def resume(self, chat_id: int, value: Any) -> Turn | None:
        """Reanuda con lo que respondió el usuario. None = no había nada esperando."""
        async with self._locks[chat_id]:
            snap = await self._graph.aget_state(self._config(chat_id))
            if not snap.next or self._expired(snap):
                # Botón viejo, doble pulsación o conversación caducada: reanudar dejaría el
                # valor esperando a la siguiente pregunta, así que se ignora.
                return None
            return await self._run(chat_id, Command(resume=value))

    async def enqueue(self, chat_id: int, photo: dict[str, Any]) -> int:
        """Encola una foto sin reanudar el hilo. Antes esto solo vivía en memoria."""
        async with self._locks[chat_id]:
            snap = await self._graph.aget_state(self._config(chat_id))
            queue = [*cast(list[dict[str, Any]], snap.values.get("queue", [])), photo]
            await self._graph.aupdate_state(self._config(chat_id), {"queue": queue})
            return len(queue)

    async def drain(self, chat_id: int) -> Turn | None:
        """Arranca la siguiente foto de la cola, si la hay. Se llama al terminar un flujo."""
        async with self._locks[chat_id]:
            snap = await self._graph.aget_state(self._config(chat_id))
            if snap.next:
                return None  # sigue ocupado
            queue = list(cast(list[dict[str, Any]], snap.values.get("queue", [])))
            if not queue:
                return None
            head, rest = queue[0], queue[1:]
            state: GraphState = {
                "chat_id": chat_id,
                "flow": "photo",
                "photo": head,
                "queue": rest,
                "questions": [],
                "answers": [],
                "attempts": 0,
                "extraction": None,
                "source_id": None,
                "decision": None,
                "reply": None,
                "error": None,
                "cancel": False,
                "gave_up": False,
            }
            return await self._run(chat_id, state)

    async def cancel(self, chat_id: int) -> Turn | None:
        """`/cancelar`: descarta lo pendiente sin pasar por el LLM."""
        return await self.resume(chat_id, {"action": "reject"})

    async def _run(self, chat_id: int, payload: Any) -> Turn:
        result = await self._graph.ainvoke(
            payload,
            config=self._config(chat_id),
            context=self._context,
            durability="sync",
        )
        return _to_turn(cast(dict[str, Any], result))


def _to_ask(value: Any) -> Ask:
    data = value if isinstance(value, dict) else {}
    return Ask(
        kind=str(data.get("kind", "")),
        text=data.get("text"),
        source_id=data.get("source_id"),
        schedules=data.get("schedules"),
        gave_up=bool(data.get("gave_up", False)),
        edit=data.get("edit"),
    )


def _to_turn(result: dict[str, Any]) -> Turn:
    interrupts = result.get("__interrupt__")
    if interrupts:
        return Turn(ask=_to_ask(interrupts[0].value))
    return Turn(
        reply=result.get("reply"),
        error=result.get("error"),
        finished=True,
    )

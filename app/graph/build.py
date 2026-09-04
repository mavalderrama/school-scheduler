"""Construcción del grafo y del checkpointer.

El grafo es una máquina de estados explícita de lo que antes era un `dict` en memoria.
Las aristas condicionales son Python puro: deciden `ingest.pending_questions` y la decisión
que pulsó el usuario, nunca el modelo.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.graph import nodes
from app.graph.state import GraphContext, GraphState
from app.log import get_logger
from app.services import ingest

log = get_logger(__name__)

Graph = CompiledStateGraph[GraphState, GraphContext, GraphState, GraphState]


# --- Aristas (Python puro, sin LLM) ---------------------------------------------------------


def route_after_extract(state: GraphState) -> Literal["triage", "finish"]:
    return "finish" if state.get("error") else "triage"


def route_triage(state: GraphState) -> Literal["ask", "present"]:
    """Preguntar solo si falta algo esencial y quedan rondas; si no, a confirmar."""
    questions = state.get("questions", [])
    if questions and state.get("attempts", 0) < ingest.MAX_REFINE_ROUNDS:
        return "ask"
    return "present"


def route_after_ask(state: GraphState) -> Literal["refine", "reject_photo"]:
    return "reject_photo" if state.get("cancel") else "refine"


def route_after_refine(state: GraphState) -> Literal["triage", "present"]:
    # Un fallo del refinado no tumba la conversación: se vuelve a preguntar.
    return "present" if state.get("error") else "triage"


def route_decision(
    state: GraphState,
) -> Literal["apply_photo", "reject_photo", "correct"]:
    decision = state.get("decision") or {}
    action = decision.get("action") if isinstance(decision, dict) else None
    if action == "reject":
        return "reject_photo"
    if action == "correct":
        return "correct"
    return "apply_photo"


def route_after_correct(state: GraphState) -> Literal["triage", "reject_photo"]:
    return "reject_photo" if state.get("cancel") else "triage"


def route_edit(state: GraphState) -> Literal["apply_edit", "finish"]:
    decision = state.get("decision") or {}
    action = decision.get("action") if isinstance(decision, dict) else None
    return "apply_edit" if action == "confirm" else "finish"


def route_after_apply_edit(state: GraphState) -> Literal["offer_reminder", "finish"]:
    """Un alta recurrente sigue con la oferta de aviso; el resto termina aquí."""
    return "offer_reminder" if state.get("reminder_offer") else "finish"


def route_start(state: GraphState) -> Literal["extract", "present_edit"]:
    return "present_edit" if state.get("flow") == "edit" else "extract"


async def finish(state: GraphState) -> dict[str, Any]:
    """Nodo terminal. La cola se drena desde el runner, no desde el grafo."""
    return {}


# --- El grafo ------------------------------------------------------------------------------


def build_graph() -> StateGraph[GraphState, GraphContext, GraphState, GraphState]:
    builder: StateGraph[GraphState, GraphContext, GraphState, GraphState] = StateGraph(
        GraphState, context_schema=GraphContext
    )
    builder.add_node("extract", nodes.extract)
    builder.add_node("triage", nodes.triage)
    builder.add_node("ask", nodes.ask)
    builder.add_node("refine", nodes.refine)
    builder.add_node("present", nodes.present)
    builder.add_node("correct", nodes.correct)
    builder.add_node("apply_photo", nodes.apply_photo)
    builder.add_node("reject_photo", nodes.reject_photo)
    builder.add_node("present_edit", nodes.present_edit)
    builder.add_node("apply_edit", nodes.apply_edit)
    builder.add_node("offer_reminder", nodes.offer_reminder)
    builder.add_node("finish", finish)

    builder.add_conditional_edges(START, route_start)
    builder.add_conditional_edges("extract", route_after_extract)
    builder.add_conditional_edges("triage", route_triage)
    builder.add_conditional_edges("ask", route_after_ask)
    builder.add_conditional_edges("refine", route_after_refine)
    builder.add_conditional_edges("present", route_decision)
    builder.add_conditional_edges("correct", route_after_correct)
    builder.add_conditional_edges("present_edit", route_edit)
    builder.add_edge("apply_photo", "finish")
    builder.add_edge("reject_photo", "finish")
    builder.add_conditional_edges("apply_edit", route_after_apply_edit)
    builder.add_edge("offer_reminder", "finish")
    builder.add_edge("finish", END)
    return builder


@asynccontextmanager
async def checkpointer_pool(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    """Pool propio de psycopg para el checkpointer, separado del de Django.

    Los tres `kwargs` no son opcionales: `setup()` corre `CREATE INDEX CONCURRENTLY`, que
    no puede ir dentro de una transacción (de ahí `autocommit`), y el código del
    checkpointer indexa las filas por nombre (de ahí `row_factory`).
    """
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        conninfo=database_url,
        max_size=5,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    try:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()  # idempotente: se salta las migraciones ya aplicadas
        log.info("checkpointer_ready")
        yield saver
    finally:
        await pool.close()

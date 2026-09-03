"""Nodos del grafo. Llaman a los servicios que ya existen; no reimplementan lógica.

**Ningún nodo habla con Telegram.** Al reanudar, LangGraph vuelve a ejecutar el nodo
interrumpido desde el principio (comprobado: un efecto colocado antes del `interrupt()` se
ejecuta cuatro veces en dos rondas de preguntas). Por eso el valor del `interrupt()` lleva
*qué* hay que mandar y es el runner quien lo manda, ya fuera del grafo.

Corolario práctico: en un nodo con `interrupt()`, la llamada va **lo primero**. Lo que
venga después solo se ejecuta cuando el usuario ya respondió, y por tanto una sola vez.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.db import repo
from app.graph.state import GraphContext, GraphState
from app.llm import compose
from app.llm.provider import LLMError
from app.llm.schemas import ExtractionResult, QAPair
from app.log import get_logger
from app.services import agenda, chat, ingest

log = get_logger(__name__)

ASK = "ask"
SUMMARY = "summary"
CORRECTION = "correction"
EDIT = "edit"


def _extraction(state: GraphState) -> ExtractionResult:
    return ExtractionResult.model_validate(state.get("extraction") or {})


def _answers(state: GraphState) -> list[QAPair]:
    return [QAPair.model_validate(a) for a in state.get("answers", [])]


# --- Foto ---------------------------------------------------------------------------------


async def extract(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    """Descarga y lee la foto. Un `IngestError` corta el flujo con un mensaje para el chat.

    Con `source_id` y `local_path` ya puestos es un reintento tras cuota: la foto está en
    disco y la source existe, así que solo se vuelve a leer.
    """
    ctx = runtime.context
    photo = state.get("photo") or {}
    source_id = state.get("source_id")
    if source_id is not None and photo.get("local_path"):
        try:
            extraction, _ = await ingest.extract_photo(
                source_id,
                Path(photo["local_path"]),
                ctx.settings,
                ctx.providers,
                photo.get("caption"),
            )
        except LLMError as exc:
            return {"error": str(exc)}
        return {
            "extraction": extraction.model_dump(mode="json"),
            "questions": [],
            "answers": [],
            "attempts": 0,
            "error": None,
        }
    try:
        result = await ingest.ingest_photo(
            file_id=photo["file_id"],
            user_id=photo["user_id"],
            display_name=photo["display_name"],
            chat_id=state["chat_id"],
            download=ctx.download,
            settings=ctx.settings,
            providers=ctx.providers,
            note=photo.get("caption"),
        )
    except ingest.IngestError as exc:
        return {"error": exc.user_message, "source_id": exc.source_id}
    return {
        "source_id": result.source_id,
        "extraction": result.extraction.model_dump(mode="json"),
        "questions": [],
        "answers": [],
        "attempts": 0,
        "error": None,
    }


async def triage(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    """Qué falta para poder guardar. **Lo decide Python**, no el modelo (regla de oro)."""
    questions = ingest.pending_questions(_extraction(state))
    exhausted = bool(questions) and state.get("attempts", 0) >= ingest.MAX_REFINE_ROUNDS
    return {"questions": questions, "gave_up": exhausted}


async def ask(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    """Pregunta lo que falta y espera. El `interrupt` va primero: lo de abajo corre una vez."""
    questions = state.get("questions", [])
    answers = state.get("answers", [])
    index = len(answers)
    answer = interrupt(
        {
            "kind": ASK,
            "text": compose.format_question(questions[index], remaining=len(questions) - index - 1),
            "source_id": state.get("source_id"),
        }
    )
    if chat.is_cancel(str(answer)):
        return {"reply": None, "error": None, "answers": answers, "questions": [], "cancel": True}
    pair = QAPair(question=questions[index], answer=str(answer))
    return {"answers": [*answers, pair.model_dump(mode="json")]}


async def refine(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    """Reinterpreta la extracción con las respuestas. Otra ronda si sigue faltando algo."""
    ctx = runtime.context
    source_id = state.get("source_id")
    if source_id is None:
        return {"error": "no hay foto pendiente"}
    try:
        refined = await ingest.refine_extraction(
            source_id, _extraction(state), _answers(state), ctx.settings, ctx.providers
        )
    except LLMError as exc:
        log.warning("graph_refine_failed", source_id=source_id, error=str(exc))
        # Se devuelve la última respuesta a la cola: la pregunta vuelve a estar viva.
        return {"answers": state.get("answers", [])[:-1], "error": compose.REFINE_FAILED_TEXT}
    return {
        "extraction": refined.model_dump(mode="json"),
        "answers": [],
        "attempts": state.get("attempts", 0) + 1,
        "error": None,
    }


async def present(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    """Resumen + ✅/✏️/❌ (o ➕/🔁 si ya hay horarios) y espera la decisión."""
    extraction = _extraction(state)
    existing: list[tuple[int, str]] = []
    if extraction.doc_type == "schedule":
        existing = [(t.pk, t.name) for t in await repo.active_schedules()]

    decision = interrupt(
        {
            "kind": SUMMARY,
            "text": compose.format_extraction(extraction),
            "source_id": state.get("source_id"),
            "gave_up": state.get("gave_up", False),
            "schedules": existing,
        }
    )
    return {"decision": decision}


async def correct(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    """Tras ✏️: pide el texto de la corrección y la aplica."""
    ctx = runtime.context
    text = interrupt({"kind": CORRECTION, "source_id": state.get("source_id")})
    if chat.is_cancel(str(text)):
        return {"cancel": True}
    source_id = state.get("source_id")
    if source_id is None:
        return {"error": "no hay foto pendiente"}
    try:
        corrected = await ingest.correct_extraction(
            source_id, _extraction(state), str(text), ctx.settings, ctx.providers
        )
    except LLMError as exc:
        log.warning("graph_correction_failed", source_id=source_id, error=str(exc))
        return {"error": compose.CORRECTION_FAILED_TEXT}
    return {"extraction": corrected.model_dump(mode="json"), "error": None}


async def apply_photo(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    """Confirma la extracción. `replace_ids` sale del botón, no de una suposición."""
    ctx = runtime.context
    source_id = state.get("source_id")
    if source_id is None:
        return {"error": "no hay foto pendiente"}
    extraction = _extraction(state)
    decision = state.get("decision") or {}
    replace_ids = decision.get("replace_ids", []) if isinstance(decision, dict) else []

    replaced = None
    if replace_ids:
        old = next((t for t in await repo.active_schedules() if t.pk in set(replace_ids)), None)
        replaced = old.name if old is not None else None

    today = datetime.now(ctx.settings.zoneinfo).date()
    result = await agenda.apply_source(source_id, extraction, today=today, replace_ids=replace_ids)
    draft = extraction.schedule
    if result.schedule_id is not None and draft is not None and draft.anchor_monday is not None:
        reply = compose.format_schedule_applied_multi(
            draft.name or "Horario", result.slots, draft.anchor_monday, replaced
        )
    else:
        reply = compose.format_applied(result.dates, result.inserted, result.superseded)
    return {"reply": reply}


async def reject_photo(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    source_id = state.get("source_id")
    if source_id is not None:
        await agenda.reject_source(source_id)
    return {"reply": compose.REJECTED_TEXT}


# --- Alta y baja por texto ------------------------------------------------------------------


async def present_edit(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    """Pregunta si se aplica el alta/baja y espera ✅/❌ (o la candidata elegida)."""
    edit = state.get("edit") or {}
    decision = interrupt({"kind": EDIT, "edit": edit})
    return {"decision": decision}


async def apply_edit(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    """Ejecuta el alta o la baja ya confirmada."""
    edit = state.get("edit") or {}
    decision = state.get("decision") or {}
    user_id = state.get("user_id")
    entry_id = decision.get("entry_id") if isinstance(decision, dict) else None

    if edit.get("action") == "add":
        added = await agenda.add_entry(
            date.fromisoformat(edit["entry_date"]),
            edit.get("kind") or "note",
            edit.get("text") or "",
            user_id,
        )
        return {"reply": compose.format_added(added)}

    target = entry_id or edit.get("entry_id")
    if target is None:
        return {"reply": "No sé cuál quitar. Vuelve a pedírmelo, por favor."}
    found = await repo.get_entry(int(target))
    if found is None or not found.is_active:
        return {"reply": "Esa entrada ya no está vigente."}
    await agenda.remove_entry(found.pk, user_id)
    return {"reply": compose.format_removed(found)}

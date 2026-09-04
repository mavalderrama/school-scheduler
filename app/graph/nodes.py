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
from app.llm.tenant import NoCredentialsError
from app.log import get_logger
from app.services import agenda, chat, ingest, reminders
from app.services import schedule as schedule_service
from app.services import scope as scope_service

log = get_logger(__name__)

ASK = "ask"
SUMMARY = "summary"
CORRECTION = "correction"
EDIT = "edit"
OFFER_REMINDER = "offer_reminder"


async def _scope(state: GraphState) -> scope_service.Scope:
    """El ámbito del niño de este hilo. Si desapareciera, es un fallo de programación."""
    found = await scope_service.for_child(state["child_id"])
    if found is None:
        raise ValueError(f"el niño {state.get('child_id')} ya no existe")
    return found


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
    sc = await _scope(state)
    try:
        chain = await ctx.tenants.for_family(sc.family_id)
    except NoCredentialsError as exc:
        # Sin clave no hay lectura posible, pero el mensaje explica cómo arreglarlo.
        return {"error": str(exc)}
    photo = state.get("photo") or {}
    source_id = state.get("source_id")
    if source_id is not None and photo.get("local_path"):
        try:
            extraction, _ = await ingest.extract_photo(
                source_id,
                Path(photo["local_path"]),
                ctx.settings,
                chain,
                photo.get("caption"),
                family_id=sc.family_id,
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
            child_id=state["child_id"],
            family_id=sc.family_id,
            download=ctx.download,
            settings=ctx.settings,
            providers=chain,
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
        sc = await _scope(state)
        refined = await ingest.refine_extraction(
            source_id,
            _extraction(state),
            _answers(state),
            ctx.settings,
            await ctx.tenants.for_family(sc.family_id),
            family_id=sc.family_id,
        )
    except NoCredentialsError as exc:
        return {"answers": state.get("answers", [])[:-1], "error": str(exc)}
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
        sc = await _scope(state)
        existing = [(t.pk, t.name) for t in await repo.active_schedules(sc.child_id)]

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
        sc = await _scope(state)
        corrected = await ingest.correct_extraction(
            source_id,
            _extraction(state),
            str(text),
            ctx.settings,
            await ctx.tenants.for_family(sc.family_id),
            family_id=sc.family_id,
        )
    except NoCredentialsError as exc:
        return {"error": str(exc)}
    except LLMError as exc:
        log.warning("graph_correction_failed", source_id=source_id, error=str(exc))
        return {"error": compose.CORRECTION_FAILED_TEXT}
    return {"extraction": corrected.model_dump(mode="json"), "error": None}


async def apply_photo(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    """Confirma la extracción. `replace_ids` sale del botón, no de una suposición."""
    source_id = state.get("source_id")
    if source_id is None:
        return {"error": "no hay foto pendiente"}
    extraction = _extraction(state)
    decision = state.get("decision") or {}
    replace_ids = decision.get("replace_ids", []) if isinstance(decision, dict) else []

    sc = await _scope(state)
    replaced = None
    if replace_ids:
        mine = await repo.active_schedules(sc.child_id)
        old = next((t for t in mine if t.pk in set(replace_ids)), None)
        replaced = old.name if old is not None else None
        # Un `replace_ids` que no sea del niño simplemente no reemplaza nada.
        replace_ids = [t.pk for t in mine if t.pk in set(replace_ids)]

    today = datetime.now(sc.zoneinfo).date()
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
    """Ejecuta la edición ya confirmada. Una rama explícita por acción.

    Antes era `if action == "add": ... else: <baja de agenda>`. Con dos acciones más, ese
    `else` habría aplicado una baja de agenda a un `remove_reminder`: lo que no se reconoce
    se responde, no se adivina.
    """
    edit = state.get("edit") or {}
    decision = state.get("decision") or {}
    user_id = state.get("user_id")
    # El id que eligió el usuario entre las candidatas. Qué significa lo dice `action`.
    chosen = decision.get("target_id") if isinstance(decision, dict) else None

    sc = await _scope(state)
    action = edit.get("action")
    if action == "add":
        return await _apply_add(sc, edit, user_id)
    if action == "remove":
        return await _apply_remove(sc, edit, chosen, user_id)
    if action == "add_recurring":
        return await _apply_add_recurring(sc, edit, user_id)
    if action == "remove_recurring":
        return await _apply_remove_recurring(sc, edit, chosen, user_id)
    if action == "edit_slot":
        return await _apply_edit_slot(sc, edit, chosen, user_id)
    if action == "add_reminder":
        return await _apply_add_reminder(sc, edit, state, user_id)
    if action == "remove_reminder":
        return await _apply_remove_reminder(sc, edit, chosen)
    log.warning("apply_edit_unknown_action", action=action, chat_id=state.get("chat_id"))
    return {"reply": "No sé qué hacer con eso. Vuelve a pedírmelo, por favor."}


async def _apply_add(
    sc: scope_service.Scope, edit: dict[str, Any], user_id: int | None
) -> dict[str, Any]:
    added = await agenda.add_entry(
        sc,
        date.fromisoformat(edit["entry_date"]),
        edit.get("kind") or "note",
        edit.get("text") or "",
        user_id,
    )
    return {"reply": compose.format_added(added)}


async def _apply_remove(
    sc: scope_service.Scope, edit: dict[str, Any], chosen: Any, user_id: int | None
) -> dict[str, Any]:
    target = chosen or edit.get("entry_id")
    if target is None:
        return {"reply": "No sé cuál quitar. Vuelve a pedírmelo, por favor."}
    found = await repo.get_entry(int(target), child_id=sc.child_id)
    if found is None or not found.is_active:
        return {"reply": "Esa entrada ya no está vigente."}
    await agenda.remove_entry(sc, found.pk, user_id)
    return {"reply": compose.format_removed(found)}


async def _apply_add_recurring(
    sc: scope_service.Scope, edit: dict[str, Any], user_id: int | None
) -> dict[str, Any]:
    """Guarda la regla semanal y deja abierta la oferta de aviso.

    El texto que se devuelve incluye ya la pregunta del aviso porque el nodo siguiente
    interrumpe: el runner solo manda el valor del `interrupt`, así que un `reply` suelto
    aquí no llegaría a verse.
    """
    weekdays = str(edit.get("weekdays") or "")
    text = str(edit.get("text") or "")
    if not weekdays or not text:
        return {"reply": "No sé qué apuntar. Vuelve a pedírmelo, por favor."}
    today = datetime.now(sc.zoneinfo).date()
    drop_ids = [int(i) for i in edit.get("drop_ids") or []]
    result = await agenda.add_recurring(
        sc, weekdays, text, today=today, user_id=user_id, drop_entry_ids=drop_ids
    )
    return {
        "reply": compose.format_recurring_added(
            weekdays, text, replaced=result.replaced, dropped=result.dropped
        ),
        "reminder_offer": {
            "edit_id": edit.get("edit_id"),
            "chat_id": edit.get("chat_id"),
            "weekdays": weekdays,
            "text": text,
        },
    }


async def _apply_remove_recurring(
    sc: scope_service.Scope, edit: dict[str, Any], chosen: Any, user_id: int | None
) -> dict[str, Any]:
    """Retira un horario vigente. El id llega de un botón, así que lo comprueba el repo."""
    target = chosen or edit.get("schedule_id")
    if target is None:
        return {"reply": "No sé cuál quitar. Vuelve a pedírmelo, por favor."}
    today = datetime.now(sc.zoneinfo).date()
    name = await agenda.remove_recurring(sc, int(target), today=today, user_id=user_id)
    if name is None:
        return {"reply": "Ese horario ya no está vigente."}
    return {"reply": compose.format_schedule_removed(name)}


async def _apply_edit_slot(
    sc: scope_service.Scope, edit: dict[str, Any], chosen: Any, user_id: int | None
) -> dict[str, Any]:
    """Cambia la materia de una franja, versionando la plantilla entera.

    La franja se relee de los horarios **vigentes de este niño** antes de tocar nada: eso
    valida de quién es el id del botón y, de paso, da el «antes» que se enseña después.
    """
    target = chosen or edit.get("slot_id")
    subject = str(edit.get("text") or "").strip()
    if target is None or not subject:
        return {"reply": "No sé qué franja cambiar. Vuelve a pedírmelo, por favor."}

    today = datetime.now(sc.zoneinfo).date()
    templates = await repo.active_schedules(sc.child_id, today)
    slots = await repo.slots_for_schedules([t.pk for t in templates])
    pair = next(
        ((t, s) for t in templates for s in slots[t.pk] if s.pk == int(target)),
        None,
    )
    if pair is None:
        return {"reply": "Esa franja ya no está vigente."}
    template, slot = pair
    place = compose.slot_place(slot, schedule=template.name, cycle_weeks=template.cycle_weeks)
    if schedule_service.same_subject(slot.subject, subject):
        # Versionar para dejarlo igual solo ensucia el histórico.
        return {"reply": f"Ya dice eso: {place} tiene «{slot.subject}»."}

    before = slot.subject
    if await agenda.edit_slot(sc, slot.pk, subject, today=today, user_id=user_id) is None:
        return {"reply": "Esa franja ya no está vigente."}
    return {"reply": compose.format_slot_changed(place, before, subject)}


async def offer_reminder(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    """Tras guardar una regla semanal: «¿te aviso a alguna hora esos días?».

    La hora se interpreta **en Python** (`reminders.parse_time_of_day`), no con el LLM:
    responder a una pregunta que el bot acaba de hacer tiene que funcionar también con el
    proveedor caído, igual que salir del interrogatorio. Lo entendido se repite en la
    confirmación, así que una lectura rara se ve.
    """
    offer = state.get("reminder_offer") or {}
    answer = interrupt(
        {
            "kind": OFFER_REMINDER,
            "text": state.get("reply") or "",
            "edit": {"edit_id": offer.get("edit_id") or 0},
        }
    )
    # Un dict es un botón (❌ Sin aviso, o /cancelar): la regla ya está guardada, así que
    # rechazar aquí solo significa «sin aviso», nunca deshacerla.
    if isinstance(answer, dict) or reminders.says_no(str(answer)):
        return {"reply": compose.NO_RECURRING_REMINDER_TEXT, "reminder_offer": None}

    moment = reminders.parse_time_of_day(str(answer))
    if moment is None:
        return {"reply": compose.RECURRING_REMINDER_UNCLEAR_TEXT, "reminder_offer": None}

    sc = await _scope(state)
    edit = {
        "edit_id": offer.get("edit_id"),
        "chat_id": offer.get("chat_id") or state.get("chat_id"),
        "action": "add_reminder",
        "text": str(offer.get("text") or ""),
        "time_of_day": compose.format_hhmm(moment),
        "repeat": "weekly",
        "weekdays": str(offer.get("weekdays") or ""),
        "on_date": None,
        # Es una actividad del colegio: en un festivo no hace falta el aviso.
        "only_school_days": True,
    }
    result = await _apply_add_reminder(sc, edit, state, state.get("user_id"))
    return {**result, "reminder_offer": None}


async def _apply_add_reminder(
    sc: scope_service.Scope, edit: dict[str, Any], state: GraphState, user_id: int | None
) -> dict[str, Any]:
    """Guarda el recordatorio y calcula cuándo suena por primera vez."""
    if len(await repo.reminders_of(sc.child_id)) >= reminders.MAX_PER_CHILD:
        return {"reply": compose.TOO_MANY_REMINDERS_TEXT}

    draft = reminders.draft_from_edit(edit)
    now = datetime.now(sc.zoneinfo)
    first = reminders.next_occurrence(
        repeat=draft.repeat,
        weekdays=draft.weekdays,
        time_of_day=draft.time_of_day,
        on_date=draft.on_date,
        only_school_days=draft.only_school_days,
        after=now,
        tz=sc.zoneinfo,
        exceptions=await repo.calendar_exceptions(sc.school_id),
        country=sc.country,
    )
    if first is None:
        # Una hora que ya pasó hoy con `once`, o unas condiciones que no casan nunca.
        return {"reply": compose.REMINDER_NEVER_FIRES_TEXT}

    saved = await repo.create_reminder(
        child_id=sc.child_id,
        # Al chat donde se pidió, que puede no ser el del niño (un privado, por ejemplo).
        chat_id=int(edit.get("chat_id") or state.get("chat_id") or 0),
        text=draft.text,
        time_of_day=draft.time_of_day,
        repeat=draft.repeat,
        weekdays=draft.weekdays,
        on_date=draft.on_date,
        only_school_days=draft.only_school_days,
        next_fire_at=first,
        created_by_id=user_id,
    )
    log.info("reminder_created", reminder_id=saved.pk, next_fire_at=first.isoformat())
    return {"reply": compose.format_reminder_added(saved)}


async def _apply_remove_reminder(
    sc: scope_service.Scope, edit: dict[str, Any], chosen: Any
) -> dict[str, Any]:
    target = chosen or edit.get("reminder_id")
    if target is None:
        return {"reply": "No sé cuál quitar. Vuelve a pedírmelo, por favor."}
    # El id llega de un botón: hay que comprobar de quién es antes de tocarlo.
    found = await repo.get_reminder(int(target), child_id=sc.child_id)
    if found is None or not found.is_active:
        return {"reply": "Ese recordatorio ya no está activo."}
    await repo.deactivate_reminder(found.pk, child_id=sc.child_id)
    return {"reply": compose.format_reminder_removed(found)}

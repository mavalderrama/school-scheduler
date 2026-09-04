"""Aislamiento entre familias. Es el test que de verdad sostiene el multi-inquilino.

Dos familias con datos en las mismas fechas y con los mismos textos: si una consulta
olvidara su filtro, aquí se ve. Y el guardián del final hace que **no se pueda añadir una
función al repositorio sin decir de qué ámbito es**, para que el aislamiento no dependa de
que alguien se acuerde.
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime

import pytest

from app.db import repo
from app.db.models import CalendarKind, NotificationKind, SourceKind
from app.llm.schemas import ExtractedEntry, ExtractionResult, ScheduleDraft, SlotDraft
from app.services import agenda, scope
from app.services.scope import Scope
from tests.conftest import TENANT, make_child

pytestmark = pytest.mark.django_db(transaction=True)

DAY = date(2026, 9, 8)
LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)


async def a_scope() -> Scope:
    found = await scope.for_child(TENANT.child_id)
    assert found is not None
    return found


async def other_scope() -> Scope:
    """Una segunda familia, completamente independiente de la del fixture."""
    child = await make_child("Otra", chat_id=-777001)
    found = await scope.for_child(child.pk)
    assert found is not None
    return found


async def seed(sc: Scope, text: str) -> int:
    """Una entrada de agenda y un horario para ese niño. Devuelve el id de la entrada."""
    source = await repo.create_source(SourceKind.MANUAL, child_id=sc.child_id)
    await agenda.apply_source(
        source.pk,
        ExtractionResult(
            entries=[ExtractedEntry(entry_date=DAY, kind="bring", text=text, confidence="high")],
            doubts=[],
            detected_language="es",
        ),
    )
    schedule_source = await repo.create_source(SourceKind.PHOTO, child_id=sc.child_id)
    await agenda.apply_source(
        schedule_source.pk,
        ExtractionResult(
            entries=[],
            doubts=[],
            detected_language="es",
            doc_type="schedule",
            schedule=ScheduleDraft(
                name=f"Horario de {text}",
                cycle_weeks=1,
                anchor_monday=date(2026, 8, 31),
                slots=[SlotDraft(week_label="A", weekday=2, subject=text)],
            ),
        ),
        today=date(2026, 9, 1),
    )
    entries = await repo.active_entries(sc.child_id, DAY, DAY)
    return entries[0].pk


# --- Lecturas ---------------------------------------------------------------------------


async def test_agenda_reads_never_cross_families() -> None:
    mine, theirs = await a_scope(), await other_scope()
    await seed(mine, "sudadera")
    await seed(theirs, "secreto")

    assert [e.text for e in await repo.active_entries(mine.child_id, DAY, DAY)] == ["sudadera"]
    assert await repo.active_dates(mine.child_id, DAY, DAY) == {DAY}
    found = await repo.find_active_entries(mine.child_id, DAY, DAY)
    assert [e.text for e in found] == ["sudadera"]


async def test_schedules_never_cross_families() -> None:
    mine, theirs = await a_scope(), await other_scope()
    await seed(mine, "sudadera")
    await seed(theirs, "secreto")

    names = [t.name for t in await repo.active_schedules(mine.child_id)]
    assert names == ["Horario de sudadera"]


async def test_the_calendar_is_per_school() -> None:
    """Dos colegios pueden cerrar el mismo día: antes el día era único globalmente."""
    mine, theirs = await a_scope(), await other_scope()
    await repo.add_calendar_exception(mine.school_id, DAY, CalendarKind.SCHOOL_CLOSED, "Receso")
    await repo.add_calendar_exception(theirs.school_id, DAY, CalendarKind.SCHOOL_CLOSED, "Otro")

    assert (await repo.calendar_exceptions(mine.school_id))[DAY][1] == "Receso"
    assert (await repo.calendar_exceptions(theirs.school_id))[DAY][1] == "Otro"


async def test_operational_reads_are_per_family() -> None:
    mine, theirs = await a_scope(), await other_scope()
    await seed(mine, "sudadera")
    await seed(theirs, "secreto")
    await repo.log_llm_call(
        task="vision",
        provider="p",
        ok=True,
        error=None,
        usage=None,
        duration_ms=1,
        family_id=theirs.family_id,
    )

    ours = await repo.recent_sources(mine.family_id)
    assert ours != []
    assert all(s.child_id == mine.child_id for s in ours)
    # La llamada al LLM la hizo la otra familia: ni aparece ni cuenta para nuestro gasto.
    assert await repo.llm_usage_by_provider(mine.family_id, LONG_AGO) == []
    assert await repo.last_call_by_provider(mine.family_id) == {}
    assert await repo.llm_usage_by_provider(theirs.family_id, LONG_AGO) != []


async def test_notifications_are_per_child() -> None:
    mine, theirs = await a_scope(), await other_scope()
    await repo.log_notification(
        NotificationKind.DAILY,
        DAY,
        theirs.chat_id or 0,
        ok=True,
        error=None,
        child_id=theirs.child_id,
    )
    # Que la otra familia ya tenga aviso no debe silenciar el nuestro.
    assert (
        await repo.notification_sent_ok(
            [NotificationKind.DAILY], DAY, mine.chat_id or 0, mine.child_id
        )
        is False
    )
    assert await repo.last_notification(mine.child_id) is None


# --- Escrituras: lo que sería destructivo -----------------------------------------------


async def test_confirming_a_photo_does_not_wipe_another_family() -> None:
    """El defecto más peligroso: el merge por fecha borraba globalmente.

    Sin el filtro por niño, confirmar una foto del día 8 desactivaría las entradas del día
    8 de todas las demás familias.
    """
    mine, theirs = await a_scope(), await other_scope()
    await seed(mine, "sudadera")
    await seed(theirs, "secreto")

    # La otra familia confirma una foto que cubre exactamente el mismo día.
    source = await repo.create_source(SourceKind.PHOTO, child_id=theirs.child_id)
    await agenda.apply_source(
        source.pk,
        ExtractionResult(
            entries=[
                ExtractedEntry(entry_date=DAY, kind="bring", text="lo suyo", confidence="high")
            ],
            doubts=[],
            detected_language="es",
        ),
    )

    assert [e.text for e in await repo.active_entries(mine.child_id, DAY, DAY)] == ["sudadera"]
    assert [e.text for e in await repo.active_entries(theirs.child_id, DAY, DAY)] == ["lo suyo"]


async def test_an_entry_id_from_another_family_is_not_reachable() -> None:
    """Los ids llegan de botones de Telegram: no basta con buscar por clave primaria."""
    mine, theirs = await a_scope(), await other_scope()
    await seed(mine, "sudadera")
    their_entry = await seed(theirs, "secreto")

    assert await repo.get_entry(their_entry, child_id=mine.child_id) is None
    source = await repo.create_source(SourceKind.TEXT_CORRECTION, child_id=mine.child_id)
    assert await repo.deactivate_entry(their_entry, source.pk, child_id=mine.child_id) is False
    # Y sigue viva para su dueño.
    assert await repo.get_entry(their_entry, child_id=theirs.child_id) is not None


async def test_a_schedule_from_another_family_cannot_be_replaced() -> None:
    mine, theirs = await a_scope(), await other_scope()
    await seed(mine, "sudadera")
    await seed(theirs, "secreto")
    their_schedule = (await repo.active_schedules(theirs.child_id))[0]

    source = await repo.create_source(SourceKind.PHOTO, child_id=mine.child_id)
    await repo.apply_schedule(
        source.pk,
        ScheduleDraft(
            name="Nuevo",
            cycle_weeks=1,
            anchor_monday=date(2026, 8, 31),
            slots=[SlotDraft(week_label="A", weekday=1, subject="x")],
        ),
        valid_from=date(2026, 9, 2),
        replace_ids=[their_schedule.pk],
    )

    still = await repo.active_schedules(theirs.child_id)
    assert [t.pk for t in still] == [their_schedule.pk], "no debería haberse reemplazado"


def _epoch() -> object:
    from django.utils import timezone

    return (
        timezone.now() - timezone.timedelta(days=1) if False else timezone.now().replace(year=2000)
    )


# --- El guardián --------------------------------------------------------------------------

SCOPED_BY_CHILD = {
    "active_entries",
    "active_dates",
    "find_active_entries",
    "get_entry",
    "deactivate_entry",
    "active_schedules",
    "active_schedule",
    "get_child",
    "create_reminder",
    "reminders_of",
    "find_active_reminders",
    "get_reminder",
    "deactivate_reminder",
}
SCOPED_BY_FAMILY = {
    "get_family",
    "update_family",
    "credentials_of",
    "upsert_credential",
    "calls_this_month",
    "recent_sources",
    "count_awaiting_extraction",
    "llm_usage_by_provider",
    "last_call_by_provider",
    "children_of",
    "create_school",
    "create_child",
}
SCOPED_BY_SCHOOL = {"calendar_exceptions", "add_calendar_exception"}
SCOPED_BY_CHAT = {
    "save_message",
    "recent_history",
    "notification_sent_ok",
    "log_notification",
    "child_for_chat",
}
INHERITS_SCOPE = {
    # Reciben una pk cuyo dueño ya se comprobó, o la heredan de la source.
    "apply_source_entries",
    "add_single_entry",
    "apply_schedule",
    "deactivate_schedule",
    "apply_slot_change",
    "entries_for_source",
    "slots_for_schedules",
    "schedule_slots",
    "create_source",
    "update_source",
    "get_source",
    "set_source_status",
    "clear_local_path",
    "log_llm_call",
    "last_notification",
    # Recibe el id que acaba de devolver el barrido, con su niño ya resuelto.
    "claim_reminder",
}
GLOBAL_ON_PURPOSE = {
    # Infraestructura, barridos programados y caché por hash de contenido.
    "check_connection",
    "close_old",
    "close_all",
    "ensure_superuser",
    "upsert_user",
    "get_user",
    "photos_awaiting_extraction",
    "abandon_stale_photos",
    "photos_to_purge",
    "get_cache_entry",
    "upsert_cache_entry",
    "touch_cache_entry",
    "delete_cache_entry",
    "purge_expired_cache",
    "cache_entries",
    "cache_stats",
    "llm_calls",
    "purge_llm_traces",
    "notifications",
    "vector_extension_installed",
    "table_constraints",
    "active_children",
    "due_reminders",
    "families_of",
    "is_member",
    "create_family",
}

CLASSIFIED = (
    SCOPED_BY_CHILD
    | SCOPED_BY_FAMILY
    | SCOPED_BY_SCHOOL
    | SCOPED_BY_CHAT
    | INHERITS_SCOPE
    | GLOBAL_ON_PURPOSE
)


def test_every_repo_function_declares_its_scope() -> None:
    """Añadir una función al repositorio sin clasificarla rompe la suite, a propósito.

    Es la diferencia entre que el aislamiento sea una propiedad del código y que sea una
    costumbre. Si esto falla, decide de qué ámbito es la función nueva y añádela arriba.
    """
    public = {
        name
        for name, obj in vars(repo).items()
        if not name.startswith("_")
        and inspect.iscoroutinefunction(obj)
        and getattr(obj, "__module__", "") == repo.__name__
    }
    assert public - CLASSIFIED == set(), "funciones de repo sin ámbito declarado"
    assert CLASSIFIED - public == set(), "clasificadas pero ya inexistentes"

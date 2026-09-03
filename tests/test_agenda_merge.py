"""Merge por fecha al confirmar una source (sección 5 del plan). Contra Postgres real."""

from __future__ import annotations

from datetime import date

import pytest

from app.db import repo
from app.db.models import SourceKind, SourceStatus
from app.llm.schemas import ExtractedEntry, ExtractionResult
from app.services import agenda
from tests.conftest import TENANT

pytestmark = pytest.mark.django_db(transaction=True)

D2, D3, D4 = date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)


def extraction(*entries: tuple[date, str, str]) -> ExtractionResult:
    return ExtractionResult(
        entries=[
            ExtractedEntry(entry_date=d, kind=kind, text=text, confidence="high")
            for d, kind, text in entries
        ],
        doubts=[],
        detected_language="es",
    )


async def new_source() -> int:
    source = await repo.create_source(
        SourceKind.PHOTO, telegram_file_id="f", child_id=TENANT.child_id
    )
    return source.pk


async def active_texts(day: date) -> list[str]:
    return [e.text for e in await repo.active_entries(TENANT.child_id, day, day)]


async def test_new_date_inserts_and_confirms() -> None:
    sid = await new_source()
    result = await agenda.apply_source(
        sid, extraction((D2, "bring", "sudadera"), (D2, "homework", "pág. 12"))
    )
    assert (result.inserted, result.superseded, result.dates) == (2, 0, [D2])
    assert await active_texts(D2) == ["sudadera", "pág. 12"]
    source = await repo.get_source(sid)
    assert source is not None and source.status == SourceStatus.CONFIRMED


async def test_existing_date_is_superseded_only_for_covered_dates() -> None:
    first = await new_source()
    await agenda.apply_source(first, extraction((D2, "bring", "sudadera"), (D3, "event", "izada")))
    second = await new_source()
    result = await agenda.apply_source(second, extraction((D2, "bring", "botella")))
    assert (result.inserted, result.superseded) == (1, 1)
    assert await active_texts(D2) == ["botella"]
    assert await active_texts(D3) == ["izada"]  # D3 no estaba cubierta: sigue vigente

    old = next(e for e in await repo.entries_for_source(first) if e.entry_date == D2)
    assert old.is_active is False and old.superseded_by_id == second


async def test_two_photos_in_a_row_chain_supersession() -> None:
    a, b, c = await new_source(), await new_source(), await new_source()
    await agenda.apply_source(a, extraction((D4, "note", "v1")))
    await agenda.apply_source(b, extraction((D4, "note", "v2")))
    await agenda.apply_source(c, extraction((D4, "note", "v3")))
    assert await active_texts(D4) == ["v3"]
    entry_a = (await repo.entries_for_source(a))[0]
    entry_b = (await repo.entries_for_source(b))[0]
    assert entry_a.superseded_by_id == b and entry_b.superseded_by_id == c


async def test_reject_touches_nothing() -> None:
    first = await new_source()
    await agenda.apply_source(first, extraction((D2, "bring", "sudadera")))
    second = await new_source()
    await agenda.reject_source(second)
    assert await active_texts(D2) == ["sudadera"]
    assert await repo.entries_for_source(second) == []
    source = await repo.get_source(second)
    assert source is not None and source.status == SourceStatus.REJECTED


async def test_empty_extraction_confirms_without_touching_entries() -> None:
    first = await new_source()
    await agenda.apply_source(first, extraction((D2, "bring", "sudadera")))
    empty = await new_source()
    result = await agenda.apply_source(empty, extraction())
    assert (result.inserted, result.superseded, result.dates) == (0, 0, [])
    assert await active_texts(D2) == ["sudadera"]

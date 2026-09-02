"""Caché de respuestas: clave, expiración, invalidación y bandera de apagado."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.utils import timezone

from app.config import Settings
from app.db import repo
from app.llm.schemas import ExtractedEntry, ExtractionResult
from app.services import cache

pytestmark = pytest.mark.django_db(transaction=True)

TODAY = date(2026, 9, 2)
TOMORROW = date(2026, 9, 3)

VALUE = ExtractionResult(
    entries=[ExtractedEntry(entry_date=TOMORROW, kind="bring", text="sudadera", confidence="high")],
    doubts=[],
    detected_language="es",
)


def key(**overrides: object) -> str:
    params: dict[str, object] = {
        "task": "vision",
        "today": TODAY,
        "tz": "America/Bogota",
        "inputs": ["abc"],
    }
    params.update(overrides)
    return cache.build_key(**params)  # type: ignore[arg-type]


# --- Clave (sin DB) -------------------------------------------------------------------


def test_key_is_stable_and_depends_on_every_part() -> None:
    assert key() == key()
    assert key(today=TOMORROW) != key()
    assert key(task="correction") != key()
    assert key(tz="UTC") != key()
    assert key(inputs=["xyz"]) != key()


def test_prompt_version_is_a_sha256_of_prompts_and_schemas() -> None:
    assert len(cache.PROMPT_VERSION) == 64
    assert cache._compute_prompt_version() == cache.PROMPT_VERSION


def test_hash_text_normalizes_whitespace_and_case() -> None:
    assert cache.hash_text("  ¿Qué   hay?  ") == cache.hash_text("¿qué hay?")
    assert cache.hash_text("a") != cache.hash_text("b")


# --- Con DB ----------------------------------------------------------------------------


async def test_put_then_get_roundtrip(settings: Settings) -> None:
    assert await cache.get(ExtractionResult, key(), settings) is None
    await cache.put(
        key(), task="vision", provider="claude_sdk", model="sonnet", value=VALUE, settings=settings
    )
    hit = await cache.get(ExtractionResult, key(), settings)
    assert hit is not None
    assert hit.value == VALUE
    assert (hit.provider, hit.model) == ("claude_sdk", "sonnet")


async def test_hit_counter_and_last_hit_are_updated(settings: Settings) -> None:
    await cache.put(key(), task="vision", provider="a", model=None, value=VALUE, settings=settings)
    entries = await repo.cache_entries()
    assert (entries[0].hits, entries[0].last_hit_at) == (0, None)

    await cache.get(ExtractionResult, key(), settings)
    await cache.get(ExtractionResult, key(), settings)
    entries = await repo.cache_entries()
    assert entries[0].hits == 2
    assert entries[0].last_hit_at is not None


async def test_another_day_is_a_miss(settings: Settings) -> None:
    """La fecha va en la clave: "¿qué hay mañana?" no se sirve al día siguiente."""
    await cache.put(key(), task="vision", provider="a", model=None, value=VALUE, settings=settings)
    assert await cache.get(ExtractionResult, key(today=TOMORROW), settings) is None


async def test_expired_entry_is_not_served(settings: Settings) -> None:
    await repo.upsert_cache_entry(
        key(),
        task="vision",
        prompt_version=cache.PROMPT_VERSION,
        provider="a",
        model=None,
        response=VALUE.model_dump(mode="json"),
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    assert await cache.get(ExtractionResult, key(), settings) is None


async def test_put_purges_expired_entries(settings: Settings) -> None:
    await repo.upsert_cache_entry(
        "vieja",
        task="vision",
        prompt_version=cache.PROMPT_VERSION,
        provider="a",
        model=None,
        response={},
        expires_at=timezone.now() - timedelta(days=1),
    )
    await cache.put(key(), task="vision", provider="a", model=None, value=VALUE, settings=settings)
    assert [e.key for e in await repo.cache_entries()] == [key()]


async def test_corrupt_response_is_dropped_not_raised(settings: Settings) -> None:
    await repo.upsert_cache_entry(
        key(),
        task="vision",
        prompt_version=cache.PROMPT_VERSION,
        provider="a",
        model=None,
        response={"entries": "no-es-una-lista"},
        expires_at=timezone.now() + timedelta(days=1),
    )
    assert await cache.get(ExtractionResult, key(), settings) is None
    assert await repo.cache_entries() == []


async def test_invalidate_removes_the_entry(settings: Settings) -> None:
    await cache.put(key(), task="vision", provider="a", model=None, value=VALUE, settings=settings)
    await cache.invalidate(key())
    assert await cache.get(ExtractionResult, key(), settings) is None
    await cache.invalidate(None)  # no explota con clave vacía


async def test_disabled_cache_never_stores_nor_serves(settings: Settings) -> None:
    off = settings.model_copy(update={"llm_cache_enabled": False})
    await cache.put(key(), task="vision", provider="a", model=None, value=VALUE, settings=off)
    assert await repo.cache_entries() == []

    await cache.put(key(), task="vision", provider="a", model=None, value=VALUE, settings=settings)
    assert await cache.get(ExtractionResult, key(), off) is None


async def test_zero_ttl_disables_storage(settings: Settings) -> None:
    no_ttl = settings.model_copy(update={"llm_cache_ttl_days": 0})
    await cache.put(key(), task="vision", provider="a", model=None, value=VALUE, settings=no_ttl)
    assert await repo.cache_entries() == []

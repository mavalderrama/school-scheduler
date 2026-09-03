"""Ingesta de una foto con proveedores falsos: fallback, llm_calls, source y corrección."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.config import Settings
from app.db import repo
from app.db.models import SourceStatus
from app.llm.provider import FallbackProvider, LLMProviders, LLMQuotaError, LLMUnavailableError
from app.llm.schemas import ExtractedEntry, ExtractionResult
from app.services import agenda, ingest
from tests.conftest import TENANT
from tests.test_provider import FakeProvider

pytestmark = pytest.mark.django_db(transaction=True)

STRONG = ExtractionResult(
    entries=[
        ExtractedEntry(
            entry_date=date(2026, 9, 2), kind="bring", text="sudadera", confidence="high"
        )
    ],
    doubts=[],
    detected_language="es",
)
WEAK = ExtractionResult(entries=[], doubts=["borroso"], detected_language="es")


def providers(primary: FakeProvider, fallback: FakeProvider | None = None) -> LLMProviders:
    vision = FallbackProvider("vision", primary, fallback, timeout_s=5)
    text = FallbackProvider("text", primary, fallback, timeout_s=5)
    return LLMProviders(vision=vision, text=text)


async def fake_download(file_id: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"\xff\xd8fake-jpeg")  # noqa: ASYNC240 (test)


async def run_ingest(settings: Settings, chain: LLMProviders) -> ingest.IngestResult:
    return await ingest.ingest_photo(
        file_id="file-1",
        user_id=111,
        display_name="Mamá",
        chat_id=-100,
        download=fake_download,
        settings=settings,
        providers=chain,
        child_id=TENANT.child_id,
    )


async def test_photo_is_stored_and_extracted(settings: Settings) -> None:
    result = await run_ingest(settings, providers(FakeProvider("claude_sdk", result=STRONG)))
    assert result.extraction == STRONG and result.provider == "claude_sdk"
    assert result.image_path == settings.photos_dir / f"{result.source_id}.jpg"
    assert result.image_path.is_file()

    source = await repo.get_source(result.source_id)
    assert source is not None
    assert source.status == SourceStatus.PENDING
    assert source.llm_provider == "claude_sdk"
    assert source.local_path == str(result.image_path)
    assert source.telegram_file_id == "file-1"
    assert source.submitted_by is not None and source.submitted_by.display_name == "Mamá"
    assert source.raw_llm_output == STRONG.model_dump(mode="json")

    calls = await repo.llm_calls("vision")
    assert [(c.provider, c.ok, c.input_tokens) for c in calls] == [("claude_sdk", True, 10)]


async def test_fallback_when_primary_fails_records_both_calls(settings: Settings) -> None:
    primary = FakeProvider("claude_sdk", fail=LLMUnavailableError("caído"))
    fallback = FakeProvider("ollama", result=STRONG)
    result = await run_ingest(settings, providers(primary, fallback))
    assert result.provider == "ollama"
    source = await repo.get_source(result.source_id)
    assert source is not None and source.llm_provider == "ollama"
    calls = await repo.llm_calls("vision")
    assert [(c.provider, c.ok, c.error) for c in calls] == [
        ("claude_sdk", False, "caído"),
        ("ollama", True, None),
    ]


async def test_weak_primary_result_triggers_fallback(settings: Settings) -> None:
    result = await run_ingest(
        settings, providers(FakeProvider("a", result=WEAK), FakeProvider("b", result=STRONG))
    )
    assert result.provider == "b" and result.extraction == STRONG


async def test_all_providers_failing_marks_source_failed(settings: Settings) -> None:
    primary = FakeProvider("a", fail=LLMUnavailableError("caído"))
    fallback = FakeProvider("b", fail=LLMUnavailableError("también"))
    with pytest.raises(ingest.IngestError, match="no respondió") as info:
        await run_ingest(settings, providers(primary, fallback))
    source = await repo.get_source(info.value.source_id)
    assert source is not None and source.status == SourceStatus.FAILED
    assert [c.ok for c in await repo.llm_calls("vision")] == [False, False]


async def test_quota_error_has_specific_message(settings: Settings) -> None:
    with pytest.raises(ingest.IngestError, match="límite de uso"):
        await run_ingest(settings, providers(FakeProvider("a", fail=LLMQuotaError("límite"))))


async def test_download_failure_marks_source_failed(settings: Settings) -> None:
    async def broken(file_id: str, destination: Path) -> None:
        raise OSError("telegram caído")

    with pytest.raises(ingest.IngestError, match="descargar") as info:
        await ingest.ingest_photo(
            file_id="f",
            user_id=111,
            display_name="Papá",
            chat_id=-100,
            download=broken,
            settings=settings,
            providers=providers(FakeProvider("a")),
            child_id=TENANT.child_id,
        )
    source = await repo.get_source(info.value.source_id)
    assert source is not None and source.status == SourceStatus.FAILED


async def test_correction_updates_source_and_logs_call(settings: Settings) -> None:
    corrected = ExtractionResult(
        entries=[
            ExtractedEntry(
                entry_date=date(2026, 9, 3), kind="bring", text="disfraz", confidence="high"
            )
        ],
        doubts=[],
        detected_language="es",
    )
    fake = FakeProvider("claude_sdk", result=corrected)
    chain = providers(fake)
    first = await run_ingest(settings, chain)
    fake.result = corrected
    result = await ingest.correct_extraction(
        first.source_id, first.extraction, "el disfraz es el jueves", settings, chain
    )
    assert result == corrected
    assert fake.corrections == ["el disfraz es el jueves"]
    source = await repo.get_source(first.source_id)
    assert source is not None and source.raw_llm_output == corrected.model_dump(mode="json")
    assert [c.task for c in await repo.llm_calls()] == ["vision", "correction"]


async def test_upsert_user_keeps_role() -> None:
    user = await repo.upsert_user(111, "Mamá")
    assert user.role == "parent"
    user.role = "admin"
    await user.asave(update_fields=["role"])
    again = await repo.upsert_user(111, "Mamá ✨")
    assert again.role == "admin" and again.display_name == "Mamá ✨"


# --- Caché de respuestas -------------------------------------------------------------------


async def test_same_photo_twice_hits_the_cache(settings: Settings) -> None:
    """La misma foto el mismo día no vuelve a llamar al proveedor (reenvío tras reinicio)."""
    fake = FakeProvider("claude_sdk", result=STRONG)
    chain = providers(fake)

    first = await run_ingest(settings, chain)
    assert fake.calls == 1

    second = await run_ingest(settings, chain)
    assert fake.calls == 1  # cero llamadas nuevas
    assert second.extraction == STRONG
    assert second.source_id != first.source_id

    source = await repo.get_source(second.source_id)
    assert source is not None
    assert source.llm_provider == "claude_sdk"  # conserva el proveedor original
    assert source.llm_cache_key is not None
    assert source.raw_llm_output == STRONG.model_dump(mode="json")

    calls = await repo.llm_calls("vision")
    assert [(c.provider, c.ok) for c in calls] == [("claude_sdk", True), ("cache", True)]
    assert calls[1].input_tokens is None and calls[1].output_tokens is None


async def test_reject_invalidates_the_cache_entry(settings: Settings) -> None:
    """Descartar borra la entrada: reenviar esa foto vuelve a leerla con el LLM."""
    fake = FakeProvider("claude_sdk", result=STRONG)
    chain = providers(fake)

    first = await run_ingest(settings, chain)
    await agenda.reject_source(first.source_id)

    await run_ingest(settings, chain)
    assert fake.calls == 2


async def test_cache_disabled_always_calls_the_provider(settings: Settings) -> None:
    off = settings.model_copy(update={"llm_cache_enabled": False})
    fake = FakeProvider("claude_sdk", result=STRONG)
    chain = providers(fake)
    await run_ingest(off, chain)
    await run_ingest(off, chain)
    assert fake.calls == 2


async def test_cache_entry_records_provider_and_model(settings: Settings) -> None:
    fake = FakeProvider("ollama", result=STRONG)
    await run_ingest(settings, providers(fake))
    entries = await repo.cache_entries()
    assert len(entries) == 1
    assert (entries[0].provider, entries[0].model, entries[0].task) == (
        "ollama",
        "fake-model",
        "vision",
    )


async def test_correction_is_cached(settings: Settings) -> None:
    corrected = ExtractionResult(
        entries=[
            ExtractedEntry(
                entry_date=date(2026, 9, 3), kind="bring", text="disfraz", confidence="high"
            )
        ],
        doubts=[],
        detected_language="es",
    )
    fake = FakeProvider("claude_sdk", result=STRONG)
    chain = providers(fake)
    first = await run_ingest(settings, chain)

    fake.result = corrected
    await ingest.correct_extraction(
        first.source_id, first.extraction, "el disfraz es el jueves", settings, chain
    )
    calls_before = fake.calls

    again = await ingest.correct_extraction(
        first.source_id, first.extraction, "  El disfraz ES el jueves  ", settings, chain
    )
    assert fake.calls == calls_before  # normaliza espacios y mayúsculas
    assert again == corrected
    assert [c.provider for c in await repo.llm_calls("correction")] == ["claude_sdk", "cache"]

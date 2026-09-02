"""Fase 4: reintento tras cuota, retención de fotos e informe de /estado."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from app.config import Settings
from app.db import repo
from app.db.models import LLMTask, NotificationKind, SourceKind, SourceStatus
from app.llm.provider import LLMQuotaError, LLMUnavailableError
from app.llm.schemas import ExtractedEntry, ExtractionResult, LLMUsage
from app.scheduler.jobs import purge_photos_job
from app.services import ingest, status
from tests.test_ingest import STRONG, fake_download, providers
from tests.test_provider import FakeProvider

pytestmark = pytest.mark.django_db(transaction=True)

CHAT = -100999


async def ingest_photo(settings: Settings, provider: FakeProvider) -> ingest.IngestResult:
    return await ingest.ingest_photo(
        file_id="f",
        user_id=111,
        display_name="Mamá",
        chat_id=CHAT,
        download=fake_download,
        settings=settings,
        providers=providers(provider),
    )


# --- Reintento tras cuota -------------------------------------------------------------------


async def test_quota_leaves_the_photo_retryable(settings: Settings) -> None:
    """Cuota agotada: la source queda pendiente y sin salida, que es lo que busca el job."""
    with pytest.raises(ingest.IngestError, match="límite de uso") as info:
        await ingest_photo(settings, FakeProvider("claude_sdk", fail=LLMQuotaError("límite")))

    source = await repo.get_source(info.value.source_id)
    assert source is not None
    assert source.status == SourceStatus.PENDING  # NO failed: se va a reintentar
    assert source.raw_llm_output is None
    assert source.local_path is not None
    assert source.chat_id == CHAT

    # Margen por el desfase de reloj entre el host y el contenedor de Postgres (`created_at`
    # lo pone la DB): sin él, la foto recién creada podría no contar como "anterior a ahora".
    now = timezone.now() + timedelta(minutes=1)
    pending = await repo.photos_awaiting_extraction(now, give_up_before=now - timedelta(hours=24))
    assert [s.pk for s in pending] == [source.pk]
    assert await repo.count_awaiting_extraction() == 1


async def test_other_llm_errors_mark_the_photo_failed(settings: Settings) -> None:
    with pytest.raises(ingest.IngestError) as info:
        await ingest_photo(settings, FakeProvider("a", fail=LLMUnavailableError("caído")))
    source = await repo.get_source(info.value.source_id)
    assert source is not None and source.status == SourceStatus.FAILED
    assert await repo.count_awaiting_extraction() == 0


async def test_a_photo_waiting_for_confirmation_is_not_retried(settings: Settings) -> None:
    """Ya extraída y esperando ✅ no es candidata a reintento (tiene raw_llm_output)."""
    await ingest_photo(settings, FakeProvider("claude_sdk", result=STRONG))
    now = timezone.now() + timedelta(minutes=1)  # ver el comentario del reloj más arriba
    assert (
        await repo.photos_awaiting_extraction(now, give_up_before=now - timedelta(hours=24)) == []
    )


async def test_stale_photos_are_abandoned(settings: Settings) -> None:
    with pytest.raises(ingest.IngestError):
        await ingest_photo(settings, FakeProvider("a", fail=LLMQuotaError("límite")))

    # `created_at` lo pone la DB con `Now()` y aquí el reloj es el del host: el contenedor
    # de Postgres va unos milisegundos por delante, así que un límite en "ahora mismo"
    # dejaría fuera la fila recién creada. El margen quita esa dependencia del reloj.
    future = timezone.now() + timedelta(minutes=1)

    # Todavía dentro de la ventana: no se abandona.
    assert await repo.abandon_stale_photos(timezone.now() - timedelta(hours=24)) == []
    # Pasada la ventana: se marca failed y se devuelve para avisar al chat.
    abandoned = await repo.abandon_stale_photos(future)
    assert len(abandoned) == 1 and abandoned[0].chat_id == CHAT
    source = await repo.get_source(abandoned[0].pk)
    assert source is not None and source.status == SourceStatus.FAILED
    # Idempotente: ya no queda nada que abandonar.
    assert await repo.abandon_stale_photos(future) == []


async def test_retry_after_quota_succeeds(settings: Settings) -> None:
    """El segundo intento sí lee la foto y la deja lista para confirmar."""
    provider = FakeProvider("claude_sdk", fail=LLMQuotaError("límite"))
    with pytest.raises(ingest.IngestError) as info:
        await ingest_photo(settings, providers(provider).vision.primary)  # type: ignore[arg-type]

    provider.fail = None
    provider.result = STRONG
    source = await repo.get_source(info.value.source_id)
    assert source is not None and source.local_path is not None

    extraction, _ = await ingest.extract_photo(
        source.pk, Path(source.local_path), settings, providers(provider)
    )
    assert extraction == STRONG
    refreshed = await repo.get_source(source.pk)
    assert refreshed is not None and refreshed.raw_llm_output is not None
    assert await repo.count_awaiting_extraction() == 0


# --- Retención de fotos ----------------------------------------------------------------------


async def test_purge_removes_old_files_but_keeps_the_row(settings: Settings) -> None:
    result = await ingest_photo(settings, FakeProvider("a", result=STRONG))
    await repo.set_source_status(result.source_id, SourceStatus.CONFIRMED)
    assert result.image_path.is_file()

    # Todavía reciente: no se toca.
    await purge_photos_job(settings)
    assert result.image_path.is_file()

    old = timezone.now() - timedelta(days=settings.photo_retention_days + 1)
    await repo.update_source(result.source_id, created_at=old)
    await purge_photos_job(settings)

    assert not result.image_path.exists()
    source = await repo.get_source(result.source_id)
    assert source is not None
    assert source.local_path is None
    assert source.raw_llm_output is not None  # la fila y la auditoría se conservan


async def test_purge_ignores_photos_still_pending(settings: Settings) -> None:
    result = await ingest_photo(settings, FakeProvider("a", result=STRONG))
    old = timezone.now() - timedelta(days=settings.photo_retention_days + 1)
    await repo.update_source(result.source_id, created_at=old)

    await purge_photos_job(settings)
    assert result.image_path.is_file()  # sigue pendiente de confirmar: no se borra


# --- /estado ---------------------------------------------------------------------------------


async def test_status_report_covers_the_operational_facts(settings: Settings) -> None:
    await ingest_photo(settings, FakeProvider("claude_sdk", result=STRONG))
    await repo.log_notification(NotificationKind.DAILY, date(2026, 9, 3), CHAT, ok=True, error=None)
    await repo.log_llm_call(
        task=LLMTask.VISION,
        provider="claude_sdk",
        ok=True,
        error=None,
        usage=LLMUsage("claude_sdk", "sonnet", 100, 20, 0.01, 900, cache_read_tokens=50),
        duration_ms=900,
    )

    report = await status.build_status(settings, providers(FakeProvider("claude_sdk")))
    assert "Estado del bot" in report
    assert "claude_sdk" in report
    assert "Última notificación" in report
    assert "Últimas fuentes" in report
    assert "Consumo desde" in report
    assert "caché:" in report


async def test_status_warns_about_an_expiring_token(settings: Settings) -> None:
    today = date.today()
    expiring = settings.model_copy(update={"claude_token_issued_at": today - timedelta(days=350)})
    report = await status.build_status(expiring, providers(FakeProvider("claude_sdk")))
    assert "⚠️" in report and "caduca" in report

    fresh = settings.model_copy(update={"claude_token_issued_at": today})
    assert "🔑 Token" in await status.build_status(fresh, providers(FakeProvider("claude_sdk")))


async def test_status_mentions_photos_waiting_for_quota(settings: Settings) -> None:
    with pytest.raises(ingest.IngestError):
        await ingest_photo(settings, FakeProvider("a", fail=LLMQuotaError("límite")))
    report = await status.build_status(settings, providers(FakeProvider("a")))
    assert "esperando a que haya cuota" in report


async def test_status_without_any_activity(settings: Settings) -> None:
    report = await status.build_status(settings, providers(FakeProvider("a")))
    assert "Todavía no he enviado ninguna notificación" in report
    assert "(sin llamadas este mes)" in report


async def test_check_providers_runs_healthchecks() -> None:
    chain = providers(FakeProvider("a"), FakeProvider("b"))
    report = await status.check_providers(chain)
    assert "✅ a" in report and "✅ b" in report


async def test_entries_survive_a_purged_photo(settings: Settings) -> None:
    """Borrar el archivo no toca la agenda: la foto es material, los datos no."""
    from app.services import agenda

    result = await ingest_photo(settings, FakeProvider("a", result=STRONG))
    await agenda.apply_source(
        result.source_id,
        ExtractionResult(
            entries=[
                ExtractedEntry(
                    entry_date=date(2026, 9, 3), kind="bring", text="sudadera", confidence="high"
                )
            ],
            doubts=[],
            detected_language="es",
        ),
    )
    await repo.update_source(result.source_id, created_at=timezone.now() - timedelta(days=200))
    await purge_photos_job(settings)

    entries = await repo.active_entries(date(2026, 9, 3), date(2026, 9, 3))
    assert [e.text for e in entries] == ["sudadera"]


async def test_source_kind_is_recorded_for_text_corrections() -> None:
    source = await repo.create_source(SourceKind.TEXT_CORRECTION, chat_id=CHAT)
    assert source.chat_id == CHAT
    assert source.kind == SourceKind.TEXT_CORRECTION

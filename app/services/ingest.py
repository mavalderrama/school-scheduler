"""Ingesta de fotos (flujo 7.1 del plan): source → descarga → extracción con fallback → registro.

No habla con Telegram: recibe la función de descarga y devuelve datos; el handler compone
los mensajes. Todo intento contra un proveedor queda en `llm_calls`, y `sources.llm_provider`
refleja el proveedor que produjo la extracción aceptada.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.config import Settings
from app.db import repo
from app.db.models import SourceKind, SourceStatus
from app.llm.provider import LLMAttempt, LLMError, LLMProviders, LLMQuotaError
from app.llm.schemas import ExtractionResult
from app.log import get_logger
from app.services import cache

log = get_logger(__name__)

Downloader = Callable[[str, Path], Awaitable[None]]
"""Descarga el `file_id` de Telegram en la ruta indicada."""


class IngestError(Exception):
    """La foto no pudo procesarse; `user_message` es apto para enviar al chat."""

    def __init__(self, user_message: str, source_id: int) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.source_id = source_id


@dataclass(frozen=True)
class IngestResult:
    source_id: int
    image_path: Path
    extraction: ExtractionResult
    provider: str


def is_weak(result: ExtractionResult) -> bool:
    """Sin entradas, o todas `low`: vale la pena intentar el fallback si existe."""
    return not result.entries or all(e.confidence == "low" for e in result.entries)


def _hash_file(path: Path) -> str:
    return cache.hash_bytes(path.read_bytes())


def _model_of(attempts: list[LLMAttempt]) -> str | None:
    """Modelo del último intento correcto, para guardarlo junto a la respuesta."""
    for attempt in reversed(attempts):
        if attempt.ok and attempt.usage is not None:
            return attempt.usage.model
    return None


async def _log_attempts(task: str, attempts: list[LLMAttempt]) -> None:
    for attempt in attempts:
        await repo.log_llm_call(
            task=task,
            provider=attempt.provider,
            ok=attempt.ok,
            error=attempt.error,
            usage=attempt.usage,
            duration_ms=attempt.duration_ms,
        )


async def _log_cache_hit(
    task: str, hit: cache.CacheHit[ExtractionResult], duration_ms: int
) -> None:
    """Un acierto también se registra: gasta cero tokens, pero cuenta como llamada."""
    await repo.log_llm_call(
        task=task,
        provider=cache.CACHE_PROVIDER,
        ok=True,
        error=None,
        usage=None,
        duration_ms=duration_ms,
        model=hit.model,
    )


async def extract_photo(
    source_id: int, image_path: Path, settings: Settings, providers: LLMProviders
) -> tuple[ExtractionResult, str]:
    """Extracción con la cadena de visión; registra intentos y actualiza la source.

    Antes de llamar al proveedor consulta la caché: la misma foto el mismo día devuelve
    la extracción guardada sin gastar tokens (reenvío tras un reinicio, o la misma foto
    mandada por los dos padres).
    """
    today = datetime.now(settings.zoneinfo).date()
    image_sha = await asyncio.to_thread(_hash_file, image_path)
    key = cache.build_key(task="vision", today=today, tz=settings.tz, inputs=[image_sha])

    started = time.monotonic()
    hit = await cache.get(ExtractionResult, key, settings)
    if hit is not None:
        await _log_cache_hit("vision", hit, int((time.monotonic() - started) * 1000))
        await repo.update_source(
            source_id,
            raw_llm_output=hit.value.model_dump(mode="json"),
            llm_provider=hit.provider,
            llm_cache_key=key,
        )
        log.info("photo_extracted", source_id=source_id, provider=hit.provider, from_cache=True)
        return hit.value, hit.provider

    try:
        run = await providers.vision.run(
            lambda p: p.extract_from_image(image_path, today),
            accept=lambda result: not is_weak(result),
        )
    except LLMQuotaError as exc:
        # La source queda `pending` a propósito: `retry_photos_job` la reintenta sola.
        await _log_attempts("vision", exc.attempts)
        raise
    except LLMError as exc:
        await _log_attempts("vision", exc.attempts)
        await repo.update_source(source_id, status=SourceStatus.FAILED)
        raise
    await _log_attempts("vision", run.attempts)
    await cache.put(
        key,
        task="vision",
        provider=run.provider,
        model=_model_of(run.attempts),
        value=run.value,
        settings=settings,
    )
    await repo.update_source(
        source_id,
        raw_llm_output=run.value.model_dump(mode="json"),
        llm_provider=run.provider,
        llm_cache_key=key,
    )
    log.info(
        "photo_extracted",
        source_id=source_id,
        provider=run.provider,
        entries=len(run.value.entries),
        doubts=len(run.value.doubts),
        weak=is_weak(run.value),
        from_cache=False,
    )
    return run.value, run.provider


async def ingest_photo(
    *,
    file_id: str,
    user_id: int,
    display_name: str,
    chat_id: int,
    download: Downloader,
    settings: Settings,
    providers: LLMProviders,
) -> IngestResult:
    """Flujo completo de una foto. Lanza `IngestError` con un mensaje apto para el usuario."""
    user = await repo.upsert_user(user_id, display_name)
    source = await repo.create_source(
        SourceKind.PHOTO, telegram_file_id=file_id, submitted_by=user, chat_id=chat_id
    )
    image_path = settings.photos_dir / f"{source.pk}.jpg"
    try:
        await download(file_id, image_path)
    except Exception as exc:
        log.exception("photo_download_failed", source_id=source.pk)
        await repo.update_source(source.pk, status=SourceStatus.FAILED)
        raise IngestError(
            "No pude descargar la foto de Telegram. ¿Me la mandas otra vez?", source.pk
        ) from exc
    await repo.update_source(source.pk, local_path=str(image_path))

    try:
        extraction, provider = await extract_photo(source.pk, image_path, settings, providers)
    except LLMQuotaError as exc:
        # Cuota agotada: la source queda `pending` sin `raw_llm_output`, que es justo lo que
        # busca `retry_photos_job`. No se reintenta en bucle aquí (regla del plan).
        log.warning("photo_quota", source_id=source.pk, error=str(exc))
        raise IngestError(
            "⏳ El proveedor de IA está en límite de uso. Guardé la foto y la leo yo solo "
            f"en ~{settings.llm_retry_after_min} min; no hace falta que hagas nada.",
            source.pk,
        ) from exc
    except LLMError as exc:
        log.warning("photo_extract_failed", source_id=source.pk, error=str(exc))
        raise IngestError(
            "No pude leer la agenda ahora mismo (el proveedor de IA no respondió). "
            "Inténtalo de nuevo más tarde.",
            source.pk,
        ) from exc
    return IngestResult(source.pk, image_path, extraction, provider)


async def correct_extraction(
    source_id: int,
    extraction: ExtractionResult,
    correction: str,
    settings: Settings,
    providers: LLMProviders,
) -> ExtractionResult:
    """Aplica una corrección en texto libre a la extracción pendiente (cadena de texto)."""
    today = datetime.now(settings.zoneinfo).date()
    key = cache.build_key(
        task="correction",
        today=today,
        tz=settings.tz,
        inputs=[
            cache.hash_text(extraction.model_dump_json()),
            cache.hash_text(correction),
        ],
    )

    started = time.monotonic()
    hit = await cache.get(ExtractionResult, key, settings)
    if hit is not None:
        await _log_cache_hit("correction", hit, int((time.monotonic() - started) * 1000))
        await repo.update_source(source_id, raw_llm_output=hit.value.model_dump(mode="json"))
        log.info(
            "extraction_corrected", source_id=source_id, provider=hit.provider, from_cache=True
        )
        return hit.value

    try:
        run = await providers.text.run(
            lambda p: p.correct_extraction(extraction, correction, today)
        )
    except LLMError as exc:
        await _log_attempts("correction", exc.attempts)
        raise
    await _log_attempts("correction", run.attempts)
    await cache.put(
        key,
        task="correction",
        provider=run.provider,
        model=_model_of(run.attempts),
        value=run.value,
        settings=settings,
    )
    await repo.update_source(
        source_id, raw_llm_output=run.value.model_dump(mode="json"), llm_provider=run.provider
    )
    log.info("extraction_corrected", source_id=source_id, provider=run.provider, from_cache=False)
    return run.value

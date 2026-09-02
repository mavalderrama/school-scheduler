"""Caché de respuestas del LLM por coincidencia exacta.

Vive en la capa de servicios, no dentro de los proveedores: así el resultado de
cualquier proveedor es reutilizable por los demás y la decisión "caché vs. LLM" queda
visible en el servicio. Un acierto no gasta ni un token.

La clave incluye la fecha de hoy y la versión de los prompts:

- La fecha evita la trampa clásica de cachear prompts con fechas relativas: "¿qué hay
  mañana?" preguntado hoy y mañana son claves distintas.
- La versión de prompts (hash de los `.md` + los JSON schema) invalida todo sola en
  cuanto se edita un prompt o un contrato pydantic.

Nota: el prompt caching de Anthropic no aplica a este bot (llamadas separadas por horas
contra un TTL de 5 minutos, y prefijo por debajo del mínimo cacheable del modelo). Ver
`docs/PLAN.md` sección 3.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from django.utils import timezone
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.db import repo
from app.llm.schemas import ExtractionResult, Intent
from app.log import get_logger

log = get_logger(__name__)

CACHE_PROVIDER = "cache"
"""Valor de `llm_calls.provider` en un acierto, para que las estadísticas no mientan."""

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "llm" / "prompts"


def _compute_prompt_version() -> str:
    """Hash de todos los prompts y contratos: cambia uno, se invalida la caché."""
    digest = hashlib.sha256()
    for path in sorted(_PROMPTS_DIR.glob("*.md")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    for model_cls in (ExtractionResult, Intent):
        digest.update(json.dumps(model_cls.model_json_schema(), sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


PROMPT_VERSION = _compute_prompt_version()


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    """Normaliza espacios y mayúsculas antes de hashear: "¿Qué hay?" == "  qué hay? "."""
    return hashlib.sha256(" ".join(text.split()).casefold().encode("utf-8")).hexdigest()


def build_key(*, task: str, today: date, tz: str, inputs: list[str]) -> str:
    payload = {
        "task": task,
        "prompt_version": PROMPT_VERSION,
        "today": today.isoformat(),
        "tz": tz,
        "inputs": inputs,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CacheHit[M: BaseModel]:
    value: M
    provider: str
    model: str | None


async def get[M: BaseModel](model_cls: type[M], key: str, settings: Settings) -> CacheHit[M] | None:
    """Entrada vigente para la clave, o None. Nunca lanza: un fallo es un miss."""
    if not settings.llm_cache_enabled:
        return None
    entry = await repo.get_cache_entry(key, now=timezone.now())
    if entry is None:
        return None
    try:
        value = model_cls.model_validate(entry.response)
    except ValidationError as exc:
        # La respuesta guardada ya no valida (cambió el contrato sin cambiar el prompt).
        log.warning("llm_cache_invalid", key=key[:12], error=str(exc)[:200])
        await repo.delete_cache_entry(key)
        return None
    await repo.touch_cache_entry(key, when=timezone.now())
    log.info("llm_cache_hit", task=entry.task, provider=entry.provider, key=key[:12])
    return CacheHit(value=value, provider=entry.provider, model=entry.model)


async def put(
    key: str,
    *,
    task: str,
    provider: str,
    model: str | None,
    value: BaseModel,
    settings: Settings,
) -> None:
    if not settings.llm_cache_enabled or settings.llm_cache_ttl_days <= 0:
        return
    now = timezone.now()
    await repo.upsert_cache_entry(
        key,
        task=task,
        prompt_version=PROMPT_VERSION,
        provider=provider,
        model=model,
        response=value.model_dump(mode="json"),
        expires_at=now + timedelta(days=settings.llm_cache_ttl_days),
    )
    purged = await repo.purge_expired_cache(now)
    log.info("llm_cache_store", task=task, provider=provider, key=key[:12], purged=purged)


async def invalidate(key: str | None) -> None:
    """Borra la entrada (al descartar una foto: reenviarla debe volver a leerla)."""
    if not key:
        return
    if await repo.delete_cache_entry(key):
        log.info("llm_cache_invalidated", key=key[:12])

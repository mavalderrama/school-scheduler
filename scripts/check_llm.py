"""Verifica los proveedores de LLM configurados.

Para cada proveedor referenciado en la config (principal o fallback, visión o texto):
1. `healthcheck()`: para claude_sdk es una llamada real (token + subproceso + JSON);
   para ollama y anthropic_api comprueba conexión y que el modelo existe.
2. Una llamada mínima de texto y otra de visión sobre `tests/fixtures/agenda_sample.jpg`.
   Mientras esos métodos sean stubs (Fase 1 y 3) se reporta como pendiente.

Uso: `python scripts/check_llm.py` (o `make check-llm` dentro del contenedor).
Sale con código 1 si algún healthcheck falla.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import (
    ConfigError,
    harden_environment,
    load_settings,
    startup_warnings,
)
from app.llm.provider import LLMError, LLMProvider, build_provider
from app.log import configure_logging

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "agenda_sample.jpg"


def _mark(ok: bool) -> str:
    return "OK " if ok else "FALLA"


async def _probe_text(provider: LLMProvider, today: datetime) -> str:
    try:
        intent = await provider.classify_intent("responde ok", [], today.date(), False)
    except NotImplementedError as exc:
        return f"pendiente ({exc})"
    except LLMError as exc:
        return f"FALLA: {exc}"
    return f"OK action={intent.action}"


async def _probe_vision(provider: LLMProvider, today: datetime) -> str:
    if not FIXTURE.is_file():
        return f"sin fixture {FIXTURE}"
    try:
        result = await provider.extract_from_image(FIXTURE, today.date())
    except NotImplementedError as exc:
        return f"pendiente ({exc})"
    except LLMError as exc:
        return f"FALLA: {exc}"
    return f"OK entries={len(result.entries)} doubts={len(result.doubts)}"


async def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    configure_logging(settings)
    for warning in startup_warnings(settings):
        print(f"AVISO: {warning}")
    harden_environment(settings)

    today = datetime.now(settings.zoneinfo)
    print(
        f"Visión: {settings.llm_vision_provider} (fallback {settings.llm_vision_fallback}) | "
        f"Texto: {settings.llm_text_provider} (fallback {settings.llm_text_fallback})"
    )
    all_ok = True
    for name in settings.providers_in_use:
        print(f"\n== {name} ==")
        try:
            provider = build_provider(name, settings)
        except Exception as exc:
            print(f"  healthcheck: FALLA al construir el proveedor: {exc}")
            all_ok = False
            continue
        health = await provider.healthcheck()
        all_ok &= health.ok
        latency = f" {health.latency_ms} ms" if health.latency_ms is not None else ""
        model = f" modelo={health.model}" if health.model else ""
        print(f"  healthcheck: {_mark(health.ok)}{model}{latency}")
        if health.detail:
            print(f"    {health.detail}")
        if not health.ok:
            continue
        print(f"  texto:  {await _probe_text(provider, today)}")
        print(f"  visión: {await _probe_vision(provider, today)}")

    print("\nResultado:", "todo OK" if all_ok else "hay proveedores fallando")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

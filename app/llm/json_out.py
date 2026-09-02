"""Salida JSON de los modelos: parseo tolerante y validación con un reintento.

Regla (sección 6 del plan): si el JSON no valida, un reintento con el error en el prompt;
si vuelve a fallar, `LLMOutputError`. Compartido por los tres proveedores.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from app.llm.provider import LLMOutputError
from app.log import get_logger

log = get_logger(__name__)

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse_json_text(text: str) -> Any:
    """Parsea la respuesta de un modelo: quita cercas ``` y recorta al primer objeto JSON."""
    stripped = text.strip()
    match = _FENCE.match(stripped)
    if match:
        stripped = match.group(1)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(stripped[start : end + 1])


def retry_hint(error: Exception) -> str:
    """Texto que se añade al prompt en el reintento."""
    return (
        "\n\nTu respuesta anterior no cumplió el schema. Error de validación:\n"
        f"{error}\n"
        "Corrige el JSON y responde únicamente con el JSON válido."
    )


async def validate_with_retry[M: BaseModel](
    model_cls: type[M],
    call: Callable[[str | None], Awaitable[Any]],
    *,
    provider: str,
) -> M:
    """Ejecuta `call(None)`, valida; si falla, `call(hint)` una vez; si falla, LLMOutputError.

    `call` recibe el texto extra para el prompt (None en el primer intento) y devuelve el
    objeto ya parseado (dict) o el texto crudo del modelo.
    """
    hint: str | None = None
    last_error: Exception | None = None
    for attempt in (1, 2):
        raw = await call(hint)
        try:
            data = parse_json_text(raw) if isinstance(raw, str) else raw
            return model_cls.model_validate(data)
        except (ValidationError, json.JSONDecodeError, TypeError) as exc:
            last_error = exc
            hint = retry_hint(exc)
            log.warning(
                "llm_output_invalid", provider=provider, attempt=attempt, error=str(exc)[:500]
            )
    raise LLMOutputError(f"{provider}: JSON inválido tras reintento: {last_error}")

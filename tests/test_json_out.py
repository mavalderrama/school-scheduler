"""Parseo tolerante y reintento único de validación."""

from __future__ import annotations

from typing import Any

import pytest

from app.llm.json_out import parse_json_text, validate_with_retry
from app.llm.provider import LLMOutputError
from app.llm.schemas import ExtractionResult, OkProbe


def test_parse_json_text_strips_fences_and_prose() -> None:
    assert parse_json_text('```json\n{"ok": true}\n```') == {"ok": True}
    assert parse_json_text('Aquí tienes:\n{"ok": false}\nSaludos') == {"ok": False}
    assert parse_json_text('{"a": 1}') == {"a": 1}


def test_parse_json_text_raises_without_object() -> None:
    with pytest.raises(ValueError):
        parse_json_text("sin json")


async def test_validate_with_retry_retries_once_with_hint() -> None:
    hints: list[str | None] = []

    async def call(hint: str | None) -> Any:
        hints.append(hint)
        return (
            {"entries": "no-es-lista"}
            if hint is None
            else ('{"entries": [], "doubts": [], "detected_language": "es"}')
        )

    result = await validate_with_retry(ExtractionResult, call, provider="fake")
    assert result.entries == []
    assert hints[0] is None and hints[1] is not None and "schema" in hints[1]


async def test_validate_with_retry_gives_up_after_second_failure() -> None:
    async def call(hint: str | None) -> Any:
        return "nada de json"

    with pytest.raises(LLMOutputError, match="fake"):
        await validate_with_retry(OkProbe, call, provider="fake")

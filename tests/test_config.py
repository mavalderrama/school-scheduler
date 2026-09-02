"""Validación de configuración y reglas de credenciales."""

from __future__ import annotations

import os
from datetime import date

import pytest
from pydantic import ValidationError

from app.config import harden_environment, startup_warnings
from tests.conftest import make_settings


def test_defaults_use_claude_subscription() -> None:
    s = make_settings()
    assert s.llm_vision_provider == "claude_sdk"
    assert s.llm_text_provider == "claude_sdk"
    assert s.llm_vision_fallback == "none"
    assert s.providers_in_use == ["claude_sdk"]


def test_id_lists_are_parsed_from_csv() -> None:
    s = make_settings()
    assert s.allowed_user_ids == [111, 222]
    assert s.allowed_chat_ids == [-100999, 111, 222]
    assert s.notify_chat_ids == [-100999]


def test_claude_sdk_requires_oauth_token() -> None:
    with pytest.raises(ValidationError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        make_settings(claude_code_oauth_token=None)


def test_ollama_requires_base_url() -> None:
    with pytest.raises(ValidationError, match="OLLAMA_BASE_URL"):
        make_settings(llm_text_provider="ollama")


def test_anthropic_api_requires_key_even_as_fallback() -> None:
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        make_settings(llm_vision_fallback="anthropic_api")


def test_fallback_equal_to_primary_is_rejected() -> None:
    with pytest.raises(ValidationError, match="LLM_TEXT_FALLBACK"):
        make_settings(llm_text_fallback="claude_sdk")


def test_switching_provider_is_only_config() -> None:
    s = make_settings(
        llm_vision_provider="ollama",
        llm_text_provider="ollama",
        ollama_base_url="http://10.0.0.20:11434",
        claude_code_oauth_token=None,
    )
    assert s.providers_in_use == ["ollama"]


def test_providers_in_use_keeps_order_without_duplicates() -> None:
    s = make_settings(
        llm_vision_provider="claude_sdk",
        llm_vision_fallback="ollama",
        llm_text_provider="ollama",
        llm_text_fallback="anthropic_api",
        ollama_base_url="http://ollama:11434",
        anthropic_api_key="sk-ant-api-test",
    )
    assert s.providers_in_use == ["claude_sdk", "ollama", "anthropic_api"]


def test_invalid_time_and_tz() -> None:
    with pytest.raises(ValidationError, match="HH:MM"):
        make_settings(daily_notify_time="7pm")
    with pytest.raises(ValidationError, match="zona horaria"):
        make_settings(tz="Marte/Olympus")


def test_warning_when_both_claude_credentials_present() -> None:
    s = make_settings(anthropic_api_key="sk-ant-api-test", claude_token_issued_at=date(2026, 9, 1))
    warnings = startup_warnings(s)
    assert len(warnings) == 1
    assert "ANTHROPIC_API_KEY" in warnings[0]


def test_warning_when_token_issue_date_missing() -> None:
    assert any("CLAUDE_TOKEN_ISSUED_AT" in w for w in startup_warnings(make_settings()))
    assert startup_warnings(make_settings(claude_token_issued_at=date(2026, 9, 1))) == []


def test_harden_environment_removes_api_key_when_claude_sdk_in_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-test")
    harden_environment(make_settings())
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_harden_environment_keeps_api_key_without_claude_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-test")
    s = make_settings(
        llm_vision_provider="anthropic_api",
        llm_text_provider="anthropic_api",
        anthropic_api_key="sk-ant-api-test",
        claude_code_oauth_token=None,
    )
    harden_environment(s)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-api-test"

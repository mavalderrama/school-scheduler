"""Fixtures compartidas. Ningún test de Fase 0 necesita Postgres, Telegram ni LLM reales."""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings

BASE_ENV: dict[str, Any] = {
    "telegram_bot_token": "123456:TEST",
    "allowed_user_ids": "111,222",
    "allowed_chat_ids": "-100999,111,222",
    "notify_chat_ids": "-100999",
    "database_url": "postgresql+asyncpg://agenda:agenda@localhost:5432/agenda",
    "claude_code_oauth_token": "sk-ant-oat-test",
    "data_dir": "/tmp/agenda-test",
}


def make_settings(**overrides: Any) -> Settings:
    """Construye Settings sin leer `.env`, con valores mínimos válidos por defecto."""
    values = {**BASE_ENV, **overrides}
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    return make_settings(data_dir=str(tmp_path))

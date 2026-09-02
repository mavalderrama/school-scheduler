"""Fixtures compartidas.

Los tests de Fase 0 no necesitan Telegram ni LLM reales. Los que tocan la DB llevan el marker
`django_db` y usan el Postgres desechable de `docker-compose.test.yml` (`make test`); el resto
corre sin Docker (`make test-unit`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.config import Settings

TEST_DATABASE_URL = "postgresql://agenda:agenda@127.0.0.1:5533/agenda"

BASE_ENV: dict[str, Any] = {
    "telegram_bot_token": "123456:TEST",
    "allowed_user_ids": "111,222",
    "allowed_chat_ids": "-100999,111,222",
    "notify_chat_ids": "-100999",
    "database_url": TEST_DATABASE_URL,
    "django_secret_key": "test-secret-key",
    "claude_code_oauth_token": "sk-ant-oat-test",
    "data_dir": "/tmp/agenda-test",
}


def make_settings(**overrides: Any) -> Settings:
    """Construye Settings sin leer `.env`, con valores mínimos válidos por defecto."""
    values = {**BASE_ENV, **overrides}
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    """Settings del bot para tests (sombrea a propósito la fixture `settings` de pytest-django)."""
    return make_settings(data_dir=str(tmp_path))


@pytest.fixture(autouse=True)
async def _close_worker_connections(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """Cierra la conexión del hilo de `sync_to_async` al terminar cada test con DB.

    Fuera de `DjangoDBMiddleware` todas las llamadas async al ORM comparten un hilo de trabajo
    cuya conexión pytest-django no ve; sin esto no puede borrar la DB de test.
    """
    yield
    if request.node.get_closest_marker("django_db") is not None:
        from app.db import repo

        await repo.close_all()

"""Servidor HTTP embebido para el admin de Django (uvicorn como tarea del event loop).

El proceso principal gestiona SIGINT/SIGTERM (app/main.py); uvicorn no debe tocar los
manejadores de señal. Se para poniendo `server.should_exit = True`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import uvicorn

from app.config import DjangoSettings


class _EmbeddedServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def build_admin_server(settings: DjangoSettings) -> uvicorn.Server:
    from app.asgi import application  # importa tras django.setup()

    config = uvicorn.Config(
        application,
        host=settings.admin_host,
        port=settings.admin_port,
        lifespan="off",  # la app ASGI de Django no habla lifespan
        log_config=None,  # structlog ya configuró el logging estándar
        access_log=False,
        timeout_graceful_shutdown=5,
    )
    return _EmbeddedServer(config)


def admin_url(settings: DjangoSettings) -> str:
    host = "localhost" if settings.admin_host == "0.0.0.0" else settings.admin_host
    return f"http://{host}:{settings.admin_port}/admin/"

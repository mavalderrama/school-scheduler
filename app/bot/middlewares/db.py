"""Middleware de base de datos: hilo y conexión propios por update, y limpieza al terminar.

Replica lo que `ASGIHandler` hace por request: `ThreadSensitiveContext` da a cada update su
propio hilo de `sync_to_async` (y por tanto su propia conexión), y `close_old_connections`
antes y después evita conexiones colgadas si Postgres se reinicia. Va como outer middleware
**después** de `AuthMiddleware`, para que los updates rechazados no toquen la DB.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from asgiref.sync import ThreadSensitiveContext

from app.db import repo


class DjangoDBMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with ThreadSensitiveContext():  # type: ignore[no-untyped-call]
            await repo.close_old()
            try:
                return await handler(event, data)
            finally:
                await repo.close_old()

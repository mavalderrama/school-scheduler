"""Repositorio: todas las queries viven aquí. Nada de SQL fuera de este módulo.

Convención con el ORM async de Django:

- Lecturas y escrituras simples: métodos async del ORM (`aget`, `acreate`, `async for`),
  con `select_related` antes de tocar una FK desde código async.
- Escrituras multi-sentencia (p. ej. `apply_source` en Fase 1): función **sync** con
  `transaction.atomic()` envuelta en `sync_to_async` (las transacciones no son async).
- Nunca `DJANGO_ALLOW_ASYNC_UNSAFE`.

Fase 0: solo utilidades de arranque y de diagnóstico.
"""

from __future__ import annotations

from typing import Any

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections


async def check_connection() -> None:
    """Falla rápido en arranque si Postgres no responde."""
    await sync_to_async(connection.ensure_connection)()


async def close_old() -> None:
    """Cierra conexiones obsoletas del hilo actual (por update y por job)."""
    await sync_to_async(close_old_connections)()


async def close_all() -> None:
    await sync_to_async(connections.close_all)()


async def ensure_superuser(username: str, password: str, email: str = "") -> bool:
    """Crea el superusuario del admin si no existe. No reescribe la contraseña si ya existe."""
    user_model = get_user_model()
    if await user_model.objects.filter(username=username).aexists():
        return False
    await user_model.objects.acreate_superuser(username, email or None, password)
    return True


def _vector_extension_installed() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        return cursor.fetchone() is not None


async def vector_extension_installed() -> bool:
    return await sync_to_async(_vector_extension_installed)()


def _table_constraints(table: str) -> dict[str, Any]:
    with connection.cursor() as cursor:
        return dict(connection.introspection.get_constraints(cursor, table))


async def table_constraints(table: str) -> dict[str, Any]:
    """Índices y constraints de una tabla, por nombre (para tests y /estado)."""
    return await sync_to_async(_table_constraints)(table)

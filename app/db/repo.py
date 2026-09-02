"""Repositorio: todas las queries viven aquí. Nada de SQL fuera de este módulo.

Fase 0: solo utilidades de arranque. Las consultas de agenda llegan en Fase 1.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def ping(session: AsyncSession) -> bool:
    result = await session.execute(text("SELECT 1"))
    return bool(result.scalar_one() == 1)

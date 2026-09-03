"""El ámbito de una conversación: de qué niño, familia y colegio estamos hablando.

Se resuelve una vez desde el `chat_id` —un grupo por niño— y viaja junto en vez de pasar
tres enteros sueltos por toda la aplicación. Que sea un objeto y no parámetros dispersos
hace que olvidarse del ámbito sea un error de tipos, no un fallo silencioso.

Lleva también `country` y `timezone` porque salen del colegio, no de la configuración
global: dos familias pueden estar en ciudades o países distintos.
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from app.db import repo
from app.db.models import Child


@dataclass(frozen=True, slots=True)
class Scope:
    child_id: int
    family_id: int
    school_id: int
    child_name: str
    country: str
    timezone: str
    chat_id: int | None = None

    @property
    def zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def of(child: Child) -> Scope:
    """Ámbito a partir de un niño ya cargado (con `school` en `select_related`)."""
    return Scope(
        child_id=child.pk,
        family_id=child.family_id,
        school_id=child.school_id,
        child_name=child.name,
        country=child.school.country,
        timezone=child.school.timezone,
        chat_id=child.chat_id,
    )


async def for_chat(chat_id: int) -> Scope | None:
    """Ámbito del chat, o None si el chat no está vinculado a ningún niño.

    None no es un error: es el estado normal de un chat que aún no se ha vinculado, y quien
    llama decide si eso significa «ignora» o «pide que se vincule».
    """
    child = await repo.child_for_chat(chat_id)
    return of(child) if child is not None else None


async def for_child(child_id: int) -> Scope | None:
    child = await repo.get_child(child_id)
    return of(child) if child is not None else None

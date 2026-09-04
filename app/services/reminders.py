"""Cuándo vuelve a sonar un recordatorio. Sin LLM, sin Django y sin reloj propio.

Todo el módulo es determinista: `after` entra por parámetro, igual que en el resto del
proyecto, para que los tests fijen el instante en vez de depender de la hora de la máquina.

La hora que guarda un recordatorio es **local del colegio del niño** (`Scope.timezone`), no
UTC: «a las 7» significa las 7 de donde vive, y tiene que seguir significando eso aunque
cambie el huso o el horario de verano. Por eso la ocurrencia se compone siempre como
`fecha + hora local` y solo al final resulta un instante absoluto.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.db.models import RepeatKind
from app.services import schoolcal

# Cuánto se tolera llegar tarde. Si el bot estuvo caído media mañana, un recordatorio de las
# 7:00 ya no sirve a la 13:00: avisa de algo que ya pasó y encima a deshora.
GRACE = timedelta(minutes=30)

# Un tope por niño. Cada recordatorio activo es una fila que el barrido mira cada minuto y un
# mensaje que alguien recibe; sin límite, un malentendido con el LLM llena el chat.
MAX_PER_CHILD = 20

# Un tope por si la configuración fuera imposible (un `weekly` sin días, o `only_school_days`
# sobre un colegio con el calendario entero cerrado): se busca dentro de un año y se rinde.
MAX_LOOKAHEAD_DAYS = 366


def parse_weekdays(value: str) -> list[int]:
    """`"135"` → `[1, 3, 5]` (ISO: 1 lunes … 7 domingo)."""
    return [int(char) for char in value]


def format_weekdays(days: list[int]) -> str:
    """`[3, 1, 1]` → `"13"`. Ordenados y sin repetir, que es como se guardan."""
    return "".join(str(day) for day in sorted(set(days)))


@dataclass(frozen=True, slots=True)
class Draft:
    """Un recordatorio todavía sin guardar, con la forma que sabe describir `compose`.

    Existe porque entre que el usuario lo pide y lo confirma, el recordatorio vive como un
    `dict` en el estado del grafo (que se guarda como JSON), y tanto el eco de la
    confirmación como el alta necesitan leerlo con tipos de verdad.
    """

    text: str
    time_of_day: time
    repeat: str
    weekdays: str
    on_date: date | None
    only_school_days: bool


def draft_from_edit(edit: dict[str, Any]) -> Draft:
    raw_date = edit.get("on_date")
    return Draft(
        text=str(edit.get("text") or ""),
        time_of_day=time.fromisoformat(str(edit["time_of_day"])),
        repeat=str(edit.get("repeat") or RepeatKind.ONCE),
        weekdays=str(edit.get("weekdays") or ""),
        on_date=date.fromisoformat(raw_date) if raw_date else None,
        only_school_days=bool(edit.get("only_school_days")),
    )


def due_action(repeat: str, fire_at: datetime, now: datetime) -> str:
    """Qué hacer con una ocurrencia que ya venció: `send`, `late` o `skip`.

    Dentro del plazo, se manda y ya está. Fuera de plazo —el bot estuvo caído— la respuesta
    depende de si el recordatorio se repite:

    - **Repetido**: `skip`. Un aviso diario se cura solo; el de mañana es el bueno, y soltar
      el de hoy a las 13:00 avisa de algo que ya pasó.
    - **Una vez**: `late`. Esa promesa se hizo una sola vez y saltársela es perderla para
      siempre, que es peor que llegar tarde. Se manda diciendo que va con retraso.
    """
    if now - fire_at <= GRACE:
        return "send"
    return "skip" if repeat != RepeatKind.ONCE else "late"


def next_occurrence(
    *,
    repeat: str,
    weekdays: str,
    time_of_day: time,
    on_date: date | None,
    only_school_days: bool,
    after: datetime,
    tz: ZoneInfo,
    exceptions: dict[date, tuple[str, str]],
    country: str,
) -> datetime | None:
    """El próximo instante en que debe sonar, **estrictamente después** de `after`.

    `None` significa que ya no vuelve a sonar: un `once` que ya pasó, o una configuración
    que no encuentra ningún día válido en un año.
    """
    local = after.astimezone(tz)

    if repeat == RepeatKind.ONCE:
        if on_date is None:
            return None
        moment = _at(on_date, time_of_day, tz)
        return moment if moment > local else None

    allowed = parse_weekdays(weekdays) if repeat == RepeatKind.WEEKLY else []
    if repeat == RepeatKind.WEEKLY and not allowed:
        return None

    # Se empieza por hoy: un recordatorio creado a las 6:00 para las 7:00 suena hoy mismo.
    day = local.date()
    for _ in range(MAX_LOOKAHEAD_DAYS):
        moment = _at(day, time_of_day, tz)
        if moment > local and _fits(
            day,
            allowed=allowed,
            only_school_days=only_school_days,
            exceptions=exceptions,
            country=country,
        ):
            return moment
        day += timedelta(days=1)
    return None


def _at(day: date, moment: time, tz: ZoneInfo) -> datetime:
    """Fecha + hora local como instante absoluto, con el horario de verano resuelto.

    América/Bogotá no cambia la hora, pero `School.timezone` es de cada colegio y otra
    familia sí puede estar en una zona que lo haga:

    - **La hora no existe** (el reloj salta de 2:00 a 3:00): `combine` no lanza, devuelve un
      instante que al volver a hora local ya no marca lo pedido. Se detecta con esa ida y
      vuelta y se corre hacia adelante, porque perder el aviso del día es peor que darlo una
      hora después, una vez al año.
    - **La hora ocurre dos veces** (el reloj retrocede): se toma la primera (`fold=0`, que es
      el valor por defecto). La reserva de `next_fire_at` impide que la segunda vuelva a
      sonar.
    """
    combined = datetime.combine(day, moment, tzinfo=tz)
    # La ida y vuelta tiene que pasar por UTC: `astimezone` a la misma zona no normaliza y
    # devolvería la hora inexistente tal cual.
    if combined.astimezone(UTC).astimezone(tz).time() != moment:
        return combined + timedelta(hours=1)
    return combined


def _fits(
    day: date,
    *,
    allowed: list[int],
    only_school_days: bool,
    exceptions: dict[date, tuple[str, str]],
    country: str,
) -> bool:
    if allowed and day.isoweekday() not in allowed:
        return False
    if not only_school_days:
        return True
    return schoolcal.is_school_day(day, exceptions=exceptions, country=country)

"""Aviso por Home Assistant cuando Telegram falla (Fase 5, opcional).

La red de seguridad del bot es que la notificación de las 19:00 **llegue**. Si Telegram no
responde, hoy solo queda una fila con `ok=false` en `notifications_log` que nadie mira: este
módulo añade una segunda vía por la instancia de Home Assistant de casa.

Se activa solo si están las tres variables. Sin ellas es un no-op silencioso, así que el
comportamiento por defecto no cambia.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from app.config import Settings
from app.log import get_logger

log = get_logger(__name__)

TIMEOUT_S = 10


def configured(settings: Settings) -> bool:
    return bool(settings.ha_url and settings.ha_token and settings.ha_notify_service)


def _post(url: str, token: str, payload: dict[str, str]) -> int:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return int(response.status)


async def notify(settings: Settings, message: str, *, title: str = "Agenda escolar") -> bool:
    """Manda el aviso por Home Assistant. Devuelve si salió bien; nunca lanza.

    No lanza a propósito: esto es el plan B de una notificación que ya falló una vez, y un
    error aquí no puede tumbar el job de las 19:00.
    """
    base, token, service = settings.ha_url, settings.ha_token, settings.ha_notify_service
    if not base or not token or not service:
        return False
    url = f"{base.rstrip('/')}/api/services/notify/{service}"
    try:
        status = await asyncio.to_thread(_post, url, token, {"message": message, "title": title})
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("ha_notify_failed", error=f"{type(exc).__name__}: {exc}")
        return False
    ok = 200 <= status < 300
    log.info("ha_notify", status=status, ok=ok)
    return ok

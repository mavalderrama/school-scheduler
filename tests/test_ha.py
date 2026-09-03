"""Fase 5: aviso por Home Assistant cuando Telegram falla.

Dos propiedades que importan más que la funcionalidad en sí: sin configurar, el
comportamiento no cambia en absoluto; y un fallo de HA no puede tumbar el job de las 19:00,
porque esto ya es el plan B de un aviso que falló una vez.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.config import Settings
from app.db import repo
from app.db.models import NotificationKind
from app.services import ha, notify
from tests.test_notify import FakeSender, seed

pytestmark = pytest.mark.django_db(transaction=True)

MON, TUE = date(2026, 9, 7), date(2026, 9, 8)


def with_ha(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "ha_url": "http://10.70.70.55:8123/",
            "ha_token": "ha-token",
            "ha_notify_service": "mobile_app_telefono",
        }
    )


def test_it_is_off_unless_all_three_variables_are_set(settings: Settings) -> None:
    assert ha.configured(settings) is False
    assert ha.configured(settings.model_copy(update={"ha_url": "http://x"})) is False
    assert ha.configured(with_ha(settings)) is True


async def test_without_configuration_it_does_nothing(settings: Settings) -> None:
    assert await ha.notify(settings, "hola") is False


async def test_it_posts_to_the_notify_service(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_post(url: str, token: str, payload: dict[str, str]) -> int:
        seen.update(url=url, token=token, payload=payload)
        return 200

    monkeypatch.setattr(ha, "_post", fake_post)
    assert await ha.notify(with_ha(settings), "mañana toca natación") is True
    # La barra final de HA_URL no debe duplicarse.
    assert seen["url"] == "http://10.70.70.55:8123/api/services/notify/mobile_app_telefono"
    assert seen["token"] == "ha-token"
    assert seen["payload"]["message"] == "mañana toca natación"


async def test_a_failure_never_raises(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, token: str, payload: dict[str, str]) -> int:
        raise OSError("sin ruta al host")

    monkeypatch.setattr(ha, "_post", boom)
    assert await ha.notify(with_ha(settings), "hola") is False


# --- Integración con la notificación diaria --------------------------------------------------


async def test_telegram_failing_falls_back_to_ha(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    def fake_post(url: str, token: str, payload: dict[str, str]) -> int:
        sent.append(payload["message"])
        return 200

    monkeypatch.setattr(ha, "_post", fake_post)
    await seed((TUE, "bring", "sudadera"))
    send = FakeSender(fail_for={-100999})

    outcomes = await notify.send_daily(send, with_ha(settings), MON)

    assert [o.sent for o in outcomes] == [False]  # Telegram falló
    assert sent, "debería haberse avisado por Home Assistant"
    # Al aviso de HA no le llega el HTML de Telegram.
    assert "<b>" not in sent[0] and "sudadera" in sent[0]

    logged = await repo.notifications(NotificationKind.DAILY)
    assert logged and "HA: enviado" in (logged[0].error or "")


async def test_without_ha_configured_nothing_changes(settings: Settings) -> None:
    """Regresión: el camino de siempre no se toca."""
    await seed((TUE, "bring", "sudadera"))
    send = FakeSender(fail_for={-100999})
    outcomes = await notify.send_daily(send, settings, MON)

    assert [o.sent for o in outcomes] == [False]
    logged = await repo.notifications(NotificationKind.DAILY)
    assert logged and "HA" not in (logged[0].error or "")


async def test_ha_failing_too_does_not_break_the_job(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(url: str, token: str, payload: dict[str, str]) -> int:
        raise OSError("HA caído")

    monkeypatch.setattr(ha, "_post", boom)
    await seed((TUE, "bring", "sudadera"))
    send = FakeSender(fail_for={-100999})

    outcomes = await notify.send_daily(send, with_ha(settings), MON)  # no lanza
    assert [o.sent for o in outcomes] == [False]
    logged = await repo.notifications(NotificationKind.DAILY)
    assert logged and "HA: también falló" in (logged[0].error or "")

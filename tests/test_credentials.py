"""Fase 9.2: cada familia trae su clave, cifrada, y su propia cadena de proveedores.

Lo que importa aquí no es que funcione, es que **no se cruce**: que la clave de una familia
no acabe en la llamada de otra, que el secreto no se guarde en claro, y que la caché no
haga que una familia pague la extracción de la siguiente.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.db import repo
from app.db.models import CredentialProvider
from app.llm.tenant import NoCredentialsError, TenantProviders
from app.services import credentials
from tests.conftest import TENANT, make_child

pytestmark = pytest.mark.django_db(transaction=True)

KEY = Fernet.generate_key().decode()
SECRET = "sk-ant-api-de-la-familia-1234"


def with_key(settings: Settings) -> Settings:
    return settings.model_copy(update={"credentials_key": KEY})


# --- Cifrado ----------------------------------------------------------------------------


def test_the_secret_is_not_stored_in_clear(settings: Settings) -> None:
    stored = credentials.encrypt(SECRET, with_key(settings))
    assert SECRET not in stored
    assert credentials.decrypt(stored, with_key(settings)) == SECRET


def test_another_key_cannot_read_it(settings: Settings) -> None:
    stored = credentials.encrypt(SECRET, with_key(settings))
    other = settings.model_copy(update={"credentials_key": Fernet.generate_key().decode()})
    with pytest.raises(credentials.CredentialError):
        credentials.decrypt(stored, other)


def test_without_a_key_it_refuses_rather_than_storing_in_clear(settings: Settings) -> None:
    assert settings.credentials_key is None
    with pytest.raises(credentials.CredentialError):
        credentials.encrypt(SECRET, settings)


def test_a_bad_key_says_how_to_generate_one(settings: Settings) -> None:
    bad = settings.model_copy(update={"credentials_key": "no-es-fernet"})
    with pytest.raises(credentials.CredentialError, match=r"Fernet\.generate_key"):
        credentials.encrypt(SECRET, bad)


def test_the_mask_never_shows_the_whole_secret() -> None:
    masked = credentials.mask(SECRET)
    assert SECRET not in masked
    assert masked.endswith("1234")
    assert credentials.mask("") == "(sin clave)"


# --- Resolución por familia ---------------------------------------------------------------


async def test_each_family_gets_its_own_key(settings: Settings) -> None:
    """El fallo que esto evita: la clave de una familia usada en la llamada de otra."""
    settings = with_key(settings)
    other = await make_child("Otra", chat_id=-777002)

    await repo.upsert_credential(
        TENANT.family_id,
        CredentialProvider.ANTHROPIC_API,
        secret=credentials.encrypt("sk-ant-primera", settings),
    )
    await repo.upsert_credential(
        other.family_id,
        CredentialProvider.ANTHROPIC_API,
        secret=credentials.encrypt("sk-ant-segunda", settings),
    )

    tenants = TenantProviders(settings)
    mine = await tenants.for_family(TENANT.family_id)
    theirs = await tenants.for_family(other.family_id)

    assert mine.vision.primary._client.api_key == "sk-ant-primera"  # type: ignore[attr-defined]
    assert theirs.vision.primary._client.api_key == "sk-ant-segunda"  # type: ignore[attr-defined]


async def test_a_family_without_a_key_is_told_so(settings: Settings) -> None:
    tenants = TenantProviders(with_key(settings))
    with pytest.raises(NoCredentialsError, match="/clave"):
        await tenants.for_family(TENANT.family_id)


async def test_the_host_subscription_is_not_lent_out(settings: Settings) -> None:
    """Una familia normal no hereda la suscripción del `.env`, aunque esté configurada."""
    settings = with_key(settings)
    assert settings.claude_code_oauth_token  # el anfitrión sí la tiene
    await repo.upsert_credential(
        TENANT.family_id,
        CredentialProvider.OPENAI,
        secret=credentials.encrypt("sk-openai", settings),
    )
    family = await repo.get_family(TENANT.family_id)
    assert family is not None
    await repo.update_family(family.pk, vision_provider="openai", text_provider="openai")

    tenants = TenantProviders(settings)
    chain = await tenants.for_family(TENANT.family_id)
    assert chain.vision.primary.name == "openai"


async def test_changing_a_key_rebuilds_the_chain(settings: Settings) -> None:
    """El admin cambia una clave y no hace falta reiniciar el bot."""
    settings = with_key(settings)
    await repo.upsert_credential(
        TENANT.family_id,
        CredentialProvider.ANTHROPIC_API,
        secret=credentials.encrypt("sk-vieja", settings),
    )
    tenants = TenantProviders(settings)
    first = await tenants.for_family(TENANT.family_id)
    assert first.vision.primary._client.api_key == "sk-vieja"  # type: ignore[attr-defined]

    await repo.upsert_credential(
        TENANT.family_id,
        CredentialProvider.ANTHROPIC_API,
        secret=credentials.encrypt("sk-nueva", settings),
    )
    second = await tenants.for_family(TENANT.family_id)
    assert second.vision.primary._client.api_key == "sk-nueva"  # type: ignore[attr-defined]


async def test_the_chain_is_cached_between_calls(settings: Settings) -> None:
    settings = with_key(settings)
    await repo.upsert_credential(
        TENANT.family_id,
        CredentialProvider.ANTHROPIC_API,
        secret=credentials.encrypt(SECRET, settings),
    )
    tenants = TenantProviders(settings)
    assert await tenants.for_family(TENANT.family_id) is await tenants.for_family(TENANT.family_id)


# --- Cuota ---------------------------------------------------------------------------------


async def test_calls_are_counted_per_family(settings: Settings) -> None:
    """Aunque cada familia pague su API, el disco y la CPU son del anfitrión."""
    other = await make_child("Otra", chat_id=-777003)
    for family_id in (TENANT.family_id, TENANT.family_id, other.family_id):
        await repo.log_llm_call(
            task="vision",
            provider="p",
            ok=True,
            error=None,
            usage=None,
            duration_ms=1,
            family_id=family_id,
        )
    # Un acierto de caché no gasta cuota de nadie.
    await repo.log_llm_call(
        task="vision",
        provider="cache",
        ok=True,
        error=None,
        usage=None,
        duration_ms=1,
        family_id=TENANT.family_id,
    )

    since = datetime(2000, 1, 1, tzinfo=UTC)
    assert await repo.calls_this_month(TENANT.family_id, since) == 2
    assert await repo.calls_this_month(other.family_id, since) == 1


# --- Que la falta de clave no se convierta en una traza --------------------------------------


async def test_a_family_without_a_key_gets_a_message_not_a_crash(settings: Settings) -> None:
    """El fallo que se coló al desplegar 9.2: nadie atrapaba `NoCredentialsError`.

    La familia del operador quedó sin `uses_host_llm` y sin credenciales, y el nodo la dejaba
    escapar: traza en el log y silencio absoluto en el chat. El mensaje ya viene escrito para
    el chat; solo faltaba que alguien lo recogiera.
    """
    from dataclasses import dataclass

    from app.graph import nodes
    from app.graph.state import GraphContext, GraphState

    tenants = TenantProviders(settings)

    async def no_key(family_id: int) -> object:
        raise NoCredentialsError("falta configurar la clave de «openai» para esta familia.")

    tenants.for_family = no_key  # type: ignore[method-assign, assignment]

    async def download(file_id: str, destination: object) -> None: ...

    @dataclass
    class FakeRuntime:
        context: GraphContext

    state: GraphState = {
        "chat_id": -1,
        "child_id": TENANT.child_id,
        "flow": "photo",
        "photo": {"file_id": "f", "user_id": 1, "display_name": "x", "caption": None},
        "queue": [],
        "questions": [],
        "answers": [],
        "attempts": 0,
    }
    context = GraphContext(settings=settings, tenants=tenants, download=download)
    result = await nodes.extract(state, FakeRuntime(context))  # type: ignore[arg-type]

    assert "clave" in (result.get("error") or "")

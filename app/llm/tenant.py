"""Los proveedores de LLM de **una familia**, no del proceso.

Hasta la Fase 9.2 se construía una sola cadena al arrancar y la usaban todos. Con cada
familia trayendo su clave, la cadena depende de quién pregunta.

El truco para no reescribir los tres proveedores: se parte del `Settings` global y se hace
una copia con las claves y los modelos de la familia encima. Así `build_providers` sigue
siendo el mismo código y los proveedores siguen recibiendo un `Settings`, que es lo que
esperan.

Se cachea por familia y por marca de tiempo de sus credenciales: cambiar una clave en el
admin invalida la cadena sin reiniciar nada.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.config import ProviderName, Settings
from app.db import repo
from app.db.models import Credential, CredentialProvider, Family
from app.llm.provider import LLMProviders, build_providers
from app.log import get_logger
from app.services import credentials

log = get_logger(__name__)


class NoCredentialsError(RuntimeError):
    """La familia no tiene una clave utilizable. El mensaje es apto para el chat."""


@dataclass
class TenantProviders:
    """Resuelve y cachea la cadena de proveedores de cada familia."""

    settings: Settings
    _cache: dict[int, tuple[str, LLMProviders]] | None = None

    def __post_init__(self) -> None:
        self._cache = {}

    async def for_family(self, family_id: int) -> LLMProviders:
        family = await repo.get_family(family_id)
        if family is None:
            raise NoCredentialsError("esta familia ya no existe")

        rows = await repo.credentials_of(family_id)
        stamp = _stamp(family, [row.updated_at for row in rows])
        cached = (self._cache or {}).get(family_id)
        if cached is not None and cached[0] == stamp:
            return cached[1]

        chain = build_providers(self._settings_for(family, rows))
        assert self._cache is not None
        self._cache[family_id] = (stamp, chain)
        log.info("tenant_providers_built", family=family_id, vision=chain.vision.name)
        return chain

    def _settings_for(self, family: Family, rows: list[Credential]) -> Settings:
        """`Settings` de la familia: el global con sus claves y modelos encima."""
        if family.uses_host_llm:
            # La familia del operador sigue con lo del `.env` (su suscripción incluida).
            return self.settings

        by_provider = {row.provider: row for row in rows if row.is_active}
        vision = str(family.vision_provider)
        text = str(family.text_provider)
        update: dict[str, object] = {
            "llm_vision_provider": vision,
            "llm_text_provider": text,
            "llm_vision_fallback": "none",
            "llm_text_fallback": "none",
            # Ninguna clave del anfitrión se hereda: si la familia no la trae, no hay.
            "claude_code_oauth_token": None,
            "anthropic_api_key": None,
            "openai_api_key": None,
            "ollama_base_url": None,
        }

        for name in {vision, text}:
            row = by_provider.get(name)
            if row is None:
                raise NoCredentialsError(
                    f"falta configurar la clave de «{name}» para esta familia. "
                    "Escríbeme por privado con /clave para añadirla."
                )
            update.update(self._for_provider(name, row))

        return self.settings.model_copy(update=update)

    def _for_provider(self, name: str, row: Credential) -> dict[str, object]:
        secret = credentials.decrypt(row.secret, self.settings)
        vision_model = row.vision_model
        text_model = row.text_model
        if name == CredentialProvider.ANTHROPIC_API:
            out: dict[str, object] = {"anthropic_api_key": secret}
            if text_model:
                out["anthropic_api_model"] = text_model
            return out
        if name == CredentialProvider.OPENAI:
            out = {"openai_api_key": secret}
            if row.base_url:
                out["openai_base_url"] = row.base_url
            if vision_model:
                out["openai_vision_model"] = vision_model
            if text_model:
                out["openai_text_model"] = text_model
            return out
        if name == CredentialProvider.OLLAMA:
            out = {"ollama_base_url": row.base_url}
            if vision_model:
                out["ollama_vision_model"] = vision_model
            if text_model:
                out["ollama_text_model"] = text_model
            return out
        raise NoCredentialsError(f"proveedor no soportado para una familia: {name}")

    def forget(self, family_id: int) -> None:
        """Olvida la cadena de una familia (cambio de clave, baja)."""
        if self._cache is not None:
            self._cache.pop(family_id, None)


def _stamp(family: Family, updated: list[datetime]) -> str:
    """Huella de la configuración: cambia al tocar una clave o el proveedor elegido."""
    newest = max(updated).isoformat() if updated else "-"
    return f"{family.vision_provider}|{family.text_provider}|{family.uses_host_llm}|{newest}"


PROVIDER_NAMES: tuple[ProviderName, ...] = ("anthropic_api", "openai", "ollama")

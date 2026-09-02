"""Configuración de la aplicación.

Se lee de variables de entorno y de `.env` con pydantic-settings. La validación
de arranque falla con un mensaje claro si un proveedor de LLM seleccionado no
tiene sus variables, y avisa si hay credenciales de Claude en conflicto.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ProviderName = Literal["ollama", "claude_sdk", "anthropic_api"]
FallbackName = Literal["none", "ollama", "claude_sdk", "anthropic_api"]

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ConfigError(RuntimeError):
    """Configuración inválida; el mensaje explica qué falta."""


def _parse_int_list(value: object) -> object:
    """Convierte "1,2,3" en [1, 2, 3]; deja pasar listas ya construidas."""
    if isinstance(value, str):
        return [int(part) for part in value.split(",") if part.strip()]
    return value


IntList = Annotated[list[int], NoDecode]


class Settings(BaseSettings):
    """Todas las variables de entorno del bot (ver sección 8 del plan)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    telegram_bot_token: str
    allowed_user_ids: IntList
    allowed_chat_ids: IntList
    notify_chat_ids: IntList

    # --- Selección de proveedor por tarea ---
    llm_vision_provider: ProviderName = "claude_sdk"
    llm_text_provider: ProviderName = "claude_sdk"
    llm_vision_fallback: FallbackName = "none"
    llm_text_fallback: FallbackName = "none"
    llm_vision_timeout: int = 180
    llm_text_timeout: int = 60
    llm_retry_after_min: int = 60

    # --- Ollama ---
    ollama_base_url: str | None = None
    ollama_vision_model: str = "qwen3-vl:8b"
    ollama_text_model: str = "qwen3:8b"

    # --- Claude vía suscripción (Agent SDK) ---
    claude_code_oauth_token: str | None = None
    claude_sdk_model: str = "sonnet"
    claude_sdk_max_turns: int = 4
    claude_token_issued_at: date | None = None

    # --- Claude por API key ---
    anthropic_api_key: str | None = None
    anthropic_api_model: str = "claude-sonnet-4-6"

    # --- Infra ---
    database_url: str
    data_dir: Path = Path("/data")
    tz: str = "America/Bogota"
    daily_notify_time: str = "19:00"
    gap_check_time: str = "18:00"
    skip_weekend: bool = True
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    @field_validator("allowed_user_ids", "allowed_chat_ids", "notify_chat_ids", mode="before")
    @classmethod
    def _split_ids(cls, value: object) -> object:
        return _parse_int_list(value)

    @field_validator("daily_notify_time", "gap_check_time")
    @classmethod
    def _check_hhmm(cls, value: str) -> str:
        if not _HHMM.match(value):
            raise ValueError(f"hora inválida {value!r}; usar formato HH:MM")
        return value

    @field_validator("tz")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"zona horaria desconocida {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _check_providers(self) -> Settings:
        problems: list[str] = []
        if self.llm_vision_fallback == self.llm_vision_provider:
            problems.append("LLM_VISION_FALLBACK no puede ser igual a LLM_VISION_PROVIDER")
        if self.llm_text_fallback == self.llm_text_provider:
            problems.append("LLM_TEXT_FALLBACK no puede ser igual a LLM_TEXT_PROVIDER")

        for name in self.providers_in_use:
            if name == "claude_sdk" and not self.claude_code_oauth_token:
                problems.append(
                    "el proveedor claude_sdk está seleccionado pero falta CLAUDE_CODE_OAUTH_TOKEN "
                    "(generarlo con `claude setup-token`)"
                )
            elif name == "ollama" and not self.ollama_base_url:
                problems.append("el proveedor ollama está seleccionado pero falta OLLAMA_BASE_URL")
            elif name == "anthropic_api" and not self.anthropic_api_key:
                problems.append(
                    "el proveedor anthropic_api está seleccionado pero falta ANTHROPIC_API_KEY"
                )
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def providers_in_use(self) -> list[ProviderName]:
        """Proveedores distintos referenciados por config (principales y fallbacks), en orden."""
        seen: list[ProviderName] = []
        candidates: list[str] = [
            self.llm_vision_provider,
            self.llm_vision_fallback,
            self.llm_text_provider,
            self.llm_text_fallback,
        ]
        for candidate in candidates:
            if candidate != "none" and candidate not in seen:
                seen.append(candidate)  # type: ignore[arg-type]
        return seen

    @property
    def zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"


def load_settings() -> Settings:
    """Carga la configuración o lanza ConfigError con un mensaje legible."""
    try:
        return Settings()  # los campos requeridos vienen del entorno
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"]) or "config"
            lines.append(f"  - {loc.upper()}: {err['msg']}")
        raise ConfigError("Configuración inválida:\n" + "\n".join(lines)) from exc


def startup_warnings(settings: Settings) -> list[str]:
    """Advertencias que no impiden arrancar pero conviene ver en rojo."""
    warnings: list[str] = []
    if settings.claude_code_oauth_token and settings.anthropic_api_key:
        warnings.append(
            "CLAUDE_CODE_OAUTH_TOKEN y ANTHROPIC_API_KEY están definidas a la vez. "
            "La API key tiene precedencia en Claude Code: si se filtra al entorno del "
            "subproceso, el proveedor claude_sdk pasaría a cobrar por uso. "
            "Se retira ANTHROPIC_API_KEY del entorno del proceso; solo la usa anthropic_api."
        )
    if "claude_sdk" in settings.providers_in_use and settings.claude_token_issued_at is None:
        warnings.append(
            "CLAUDE_TOKEN_ISSUED_AT no está definida; el token de suscripción caduca al año "
            "y /estado no podrá avisar."
        )
    return warnings


def harden_environment(settings: Settings) -> None:
    """Evita que la API key gane sobre la suscripción en el subproceso de Claude Code.

    El Agent SDK hereda el entorno del proceso. Si ANTHROPIC_API_KEY está ahí, tiene
    precedencia sobre CLAUDE_CODE_OAUTH_TOKEN y se pasaría a pagar por uso sin querer.
    El proveedor anthropic_api recibe la key explícitamente desde `settings`.
    """
    if "claude_sdk" in settings.providers_in_use:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

"""Configuración de la aplicación.

Se lee de variables de entorno y de `.env` con pydantic-settings. Hay dos clases:

- `DjangoSettings`: el subconjunto que Django necesita (DB, admin, zona horaria), con
  valores por defecto seguros para que `manage.py`, `makemigrations` y el plugin de mypy
  puedan importar `app/django_settings.py` sin un `.env` completo.
- `Settings(DjangoSettings)`: todo lo del bot. Falla en arranque con un mensaje claro si
  un proveedor de LLM seleccionado no tiene sus variables, si el admin está habilitado
  con la clave secreta insegura, o si hay credenciales de Claude en conflicto.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ProviderName = Literal["ollama", "claude_sdk", "anthropic_api", "openai"]
FallbackName = Literal["none", "ollama", "claude_sdk", "anthropic_api", "openai"]

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

INSECURE_SECRET_KEY = "insecure-dev-key-change-me"
"""Valor por defecto de DJANGO_SECRET_KEY; el bot se niega a arrancar el admin con él."""


class ConfigError(RuntimeError):
    """Configuración inválida; el mensaje explica qué falta."""


def _parse_int_list(value: object) -> object:
    """Convierte "1,2,3" en [1, 2, 3]; deja pasar listas ya construidas."""
    if isinstance(value, str):
        return [int(part) for part in value.split(",") if part.strip()]
    return value


def _parse_str_list(value: object) -> object:
    """Convierte "a,b" en ["a", "b"]; deja pasar listas ya construidas."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


IntList = Annotated[list[int], NoDecode]
StrList = Annotated[list[str], NoDecode]


class DjangoSettings(BaseSettings):
    """Lo que Django necesita. Importable sin `.env` (manage.py, mypy, makemigrations)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql://agenda:agenda@localhost:5432/agenda"
    tz: str = "America/Bogota"

    # --- Admin web (Django, solo LAN) ---
    django_secret_key: str = INSECURE_SECRET_KEY
    django_debug: bool = False
    django_allowed_hosts: StrList = ["*"]
    django_csrf_trusted_origins: StrList = []
    admin_enabled: bool = True
    admin_host: str = "0.0.0.0"
    admin_port: int = 8000
    django_superuser_username: str | None = None
    django_superuser_password: str | None = None
    django_superuser_email: str = ""

    @field_validator("django_allowed_hosts", "django_csrf_trusted_origins", mode="before")
    @classmethod
    def _split_strs(cls, value: object) -> object:
        return _parse_str_list(value)

    @field_validator("tz")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"zona horaria desconocida {value!r}") from exc
        return value

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        scheme = urlsplit(value).scheme
        if "+" in scheme:
            raise ValueError(
                "DATABASE_URL ya no lleva dialecto de SQLAlchemy (p. ej. '+asyncpg'); "
                "usar postgresql://user:pass@host:5432/db"
            )
        if scheme not in {"postgresql", "postgres"}:
            raise ValueError(f"esquema no soportado {scheme!r}; usar postgresql://")
        return value

    @property
    def zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


class Settings(DjangoSettings):
    """Todas las variables de entorno del bot (ver sección 8 del plan)."""

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

    # --- Caché de respuestas del LLM ---
    llm_cache_enabled: bool = True
    llm_cache_ttl_days: int = 30

    # --- Robustez y operación ---
    photo_retention_days: int = 90
    # Traza de las llamadas al LLM: prompt y respuesta cruda en `llm_calls`.
    llm_trace_enabled: bool = True
    llm_trace_retention_days: int = 30
    # Caducidad de una conversación a medias guardada en el grafo.
    graph_state_ttl_hours: int = 24
    # OpenTelemetry: apagado por defecto. Langfuse ingiere OTLP, así que apuntarlo allí es
    # solo poner el endpoint. Las dependencias van en el extra `otel`.
    otel_enabled: bool = False
    otel_service_name: str = "agenda-escolar-bot"
    # Home Assistant (Fase 5, opcional): segunda vía si Telegram falla.
    ha_url: str | None = None
    ha_token: str | None = None
    ha_notify_service: str | None = None
    # Cifrado de las claves de LLM de cada familia (Fase 9.2). Sin esto no se pueden
    # guardar ni leer claves de terceros.
    credentials_key: str | None = None
    retry_give_up_hours: int = 24

    # --- Ollama ---
    ollama_base_url: str | None = None
    ollama_vision_model: str = "qwen3-vl:8b"
    ollama_text_model: str = "qwen3:8b"

    # --- Claude vía suscripción (Agent SDK) ---
    claude_code_oauth_token: str | None = None
    claude_sdk_model: str = "sonnet"
    claude_sdk_max_turns: int = 4
    claude_token_issued_at: date | None = None

    # --- OpenAI ---
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_vision_model: str = "gpt-5"
    openai_text_model: str = "gpt-5-mini"

    # --- Claude por API key ---
    anthropic_api_key: str | None = None
    anthropic_api_model: str = "claude-sonnet-4-6"

    # --- Infra ---
    database_url: str  # en el bot vuelve a ser obligatoria
    data_dir: Path = Path("/data")
    daily_notify_time: str = "19:00"
    gap_check_time: str = "18:00"
    skip_weekend: bool = True
    # Horario rotativo y calendario escolar (Fase 6).
    schedule_enabled: bool = True
    school_country: str = "CO"
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

    @field_validator("school_country")
    @classmethod
    def _check_country(cls, value: str) -> str:
        """Un país que `holidays` no conoce daría «sin festivos» en silencio.

        Y sin festivos el bot anunciaría clase el 12 de octubre, que es justo el fallo que
        nadie mira hasta que pasa. Mejor no arrancar.
        """
        import holidays

        code = value.strip().upper()
        try:
            holidays.country_holidays(code, years=[date.today().year])
        except NotImplementedError as exc:
            raise ValueError(
                f"país desconocido para los festivos: {value!r}. Usa un código ISO de dos "
                "letras que soporte la librería `holidays` (p. ej. CO)."
            ) from exc
        return code

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
            elif name == "openai" and not self.openai_api_key:
                problems.append("el proveedor openai está seleccionado pero falta OPENAI_API_KEY")

        if self.admin_enabled and self.django_secret_key == INSECURE_SECRET_KEY:
            problems.append(
                "el admin está habilitado (ADMIN_ENABLED=true) pero DJANGO_SECRET_KEY no está "
                "definida; generarla con "
                '`python -c "import secrets; print(secrets.token_urlsafe(50))"`'
            )
        if bool(self.django_superuser_username) != bool(self.django_superuser_password):
            problems.append(
                "DJANGO_SUPERUSER_USERNAME y DJANGO_SUPERUSER_PASSWORD van juntas (ambas o ninguna)"
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
    if settings.admin_enabled and settings.django_debug:
        warnings.append("DJANGO_DEBUG=true: el admin muestra trazas completas; solo para depurar.")
    return warnings


def _silence_langsmith() -> None:
    """`langsmith` llega como dependencia de langgraph aunque no se use.

    Se apaga explícitamente: la observabilidad de este proyecto es `llm_calls` y, si acaso,
    OTLP; no queremos que nada salga a un servicio de terceros por defecto.
    """
    os.environ.setdefault("LANGSMITH_TRACING", "false")
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")


def harden_environment(settings: Settings) -> None:
    """Evita que la API key gane sobre la suscripción en el subproceso de Claude Code.

    El Agent SDK hereda el entorno del proceso. Si ANTHROPIC_API_KEY está ahí, tiene
    precedencia sobre CLAUDE_CODE_OAUTH_TOKEN y se pasaría a pagar por uso sin querer.
    El proveedor anthropic_api recibe la key explícitamente desde `settings`.
    """
    if "claude_sdk" in settings.providers_in_use:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

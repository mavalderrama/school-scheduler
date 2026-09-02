# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Estado del proyecto

`docs/PLAN.md` es la fuente de verdad del diseño (stack, esquema de DB, contratos del LLM, flujos, fases). **Léelo completo antes de escribir código.** Este archivo resume lo operativo; ante cualquier duda de diseño, manda el plan.

Fases (sección 10 del plan):

- **Fase 0 (hecha):** esqueleto, config con validación de proveedores, bot que responde `/start` y `/ping` solo a la whitelist, migración 001, abstracción `LLMProvider` con los tres proveedores (healthcheck real; extracción e intención son stubs `NotImplementedError`), `scripts/check_llm.py`, Dockerfile con dos targets, compose, Makefile.
- **Fase 1:** ingesta de fotos + confirmación. **Fase 2:** notificación diaria. **Fase 3:** consultas por texto. **Fase 4:** robustez y `/estado`. **Fase 5 (opcional):** Home Assistant.

Decisión del usuario: **proveedor principal = suscripción Claude (`claude_sdk`)** para visión y texto. Ollama y API key quedan implementados para cambiar solo por variables de entorno.

## Qué es

`agenda-escolar-bot`: bot de Telegram que recibe fotos de la agenda escolar de un niño, extrae entradas por fecha con un LLM, las guarda en Postgres previa **confirmación del usuario**, envía una notificación diaria a las 19:00 con lo de mañana y responde preguntas en lenguaje natural. Self-hosted en Proxmox. Un niño, un colegio, dos padres en whitelist.

## Forma de trabajar (obligatorio)

- Avanzar **fase por fase**. Al terminar cada fase: correr `make check`, hacer commit con mensaje `feat(faseN): ...` y **detenerse** para revisión antes de continuar.
- **Preguntar antes de:** cambiar una librería principal, cambiar el esquema de la DB después de la Fase 2, o agregar un servicio nuevo al `docker-compose`.
- No inventar credenciales ni IDs de Telegram; `.env.example` lleva placeholders.
- El Agent SDK cambia: antes de tocar `app/llm/claude_sdk.py` revisar la documentación vigente (`code.claude.com/docs/en/agent-sdk/python.md`, `structured-outputs.md`, `hosting.md`) o inspeccionar `claude_agent_sdk/types.py` en `.venv`.
- Mantener este archivo actualizado cuando cambien comandos, variables de entorno o convenciones.

## Stack

Python 3.12 · `aiogram` 3.x con **long polling** (nada expuesto a internet) · PostgreSQL 16 · SQLAlchemy 2.x async (`asyncpg`) + Alembic · APScheduler (`AsyncIOScheduler`, en el mismo proceso del bot) · `pydantic-settings` · `structlog` · `uv` para dependencias (`uv.lock` versionado) · Docker Compose (servicios `bot` y `postgres`). Ollama, si se usa, corre en otro host de la red interna.

## Comandos

```
make dev              # docker compose up --build + logs (aplica migraciones al arrancar)
make down / up / logs / ps / shell
make migrate          # alembic upgrade head dentro del contenedor
make revision m="..." # alembic revision --autogenerate
make check-llm        # scripts/check_llm.py dentro del contenedor
make install          # uv sync --all-extras (local)
make test             # pytest (no necesita Postgres ni red)
make lint / fmt       # ruff
make typecheck        # mypy --strict sobre app, scripts, tests y alembic
make check            # lint + typecheck + test
make check-llm-local  # check_llm.py con el .env local
```

Un solo test: `uv run pytest tests/test_config.py::test_defaults_use_claude_subscription`.

`scripts/check_llm.py` corre `healthcheck()` de cada proveedor referenciado en la config y luego una llamada mínima de texto y de visión (sobre `tests/fixtures/agenda_sample.jpg`). Para `claude_sdk` el healthcheck **es** una llamada real: valida token, arranque del subproceso y salida JSON. Ejecutarlo al cambiar de modelo o proveedor.

## Convenciones de código

- Comentarios y docstrings en **español**; nombres de variables, funciones y clases en **inglés**.
- Todo lo que dependa de fecha/hora usa `settings.zoneinfo` (`ZoneInfo("America/Bogota")`, sin horario de verano).
- Nada de SQL fuera de `app/db/repo.py`.
- Prompts en `app/llm/prompts/*.md`, cargados con `load_prompt(name)`, **compartidos por los tres proveedores**; cada proveedor solo cambia cómo los envía y cómo obtiene el JSON.
- En `app/db/models.py` la columna `text` de `agenda_entries` sombrea `sqlalchemy.text`; ahí se importa como `sa_text`.
- `ruff` (line-length 100), `mypy --strict` con el plugin de pydantic; `pytest-asyncio` en modo `auto`.
- Tests unitarios no dependen de Postgres, Telegram ni LLM reales: `tests/conftest.py::make_settings` construye `Settings` sin leer `.env`, y `query()` del SDK se sustituye con `monkeypatch`.

## Arquitectura (lo que hay que entender antes de tocar código)

**Regla de oro del LLM:** el modelo **nunca ejecuta nada**. Solo clasifica intención y extrae datos en JSON validado con pydantic (`ExtractionResult`, `Intent` en `app/llm/schemas.py`). Toda la lógica la ejecutan handlers Python deterministas en `app/services/`. Si el JSON no valida: un reintento con el error en el prompt; si vuelve a fallar, `LLMOutputError` y fallback si está configurado. Esto es a la vez la defensa contra prompt injection (texto de Telegram y texto dentro de fotos son entrada no confiable) y la forma de que funcione con modelos abiertos pequeños sin function calling.

**Proveedores intercambiables por tarea.** `app/llm/provider.py` define el Protocol `LLMProvider` (`extract_from_image`, `classify_intent`, `healthcheck`), la jerarquía de errores (`LLMError` → `LLMUnavailableError`, `LLMQuotaError`, `LLMOutputError`), `build_provider(name, settings)` con imports perezosos y `FallbackProvider`, que encadena principal → fallback y aplica el timeout de la tarea. `build_providers(settings)` devuelve `LLMProviders(vision, text)`; se inyecta en los handlers vía `Dispatcher(providers=...)`. `NotImplementedError` (stubs) se propaga sin pasar por el fallback. Tres implementaciones:

- `ollama.py`: SDK `openai` contra `OLLAMA_BASE_URL/v1`. Healthcheck: lista modelos y exige que los dos configurados estén descargados.
- `claude_sdk.py`: Claude Agent SDK con la suscripción (`CLAUDE_CODE_OAUTH_TOKEN`). Es Claude Code headless como subproceso; el binario viene **empaquetado dentro del wheel** de `claude-agent-sdk` (`_bundled/claude`), no se instala aparte. `_run_json()` centraliza toda llamada: `tools=[]` para texto (ninguna herramienta) y `tools=["Read"]` para visión; `disallowed_tools` explícito (`Bash`, `Write`, `Edit`, `WebSearch`, `WebFetch`, `Agent`, ...); `setting_sources=[]`; sesión nueva por llamada; `output_format` con el JSON schema pydantic y el resultado en `ResultMessage.structured_output`. El entorno del subproceso lleva solo el token, `CLAUDE_CONFIG_DIR=$DATA_DIR/claude` y `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`. `ResultError` con status 429 o texto de límite se traduce a `LLMQuotaError`; el resto a `LLMUnavailableError`.
- `anthropic_api.py`: SDK `anthropic` con la key explícita. Healthcheck: `models.retrieve` (no gasta tokens).

**Credenciales de Claude.** En Claude Code `ANTHROPIC_API_KEY` tiene precedencia sobre `CLAUDE_CODE_OAUTH_TOKEN`, y el subproceso del SDK hereda el entorno del proceso. Por eso `config.harden_environment()` retira `ANTHROPIC_API_KEY` y `ANTHROPIC_AUTH_TOKEN` de `os.environ` en arranque cuando `claude_sdk` está en uso; `anthropic_api` recibe la key desde `settings`, no del entorno. `startup_warnings()` avisa si ambas están definidas. No usar `--bare` (no lee el token).

**Datos: nunca borrar, siempre versionar.** Cada foto o corrección es una fila en `sources`. Al confirmar una source, `services/agenda.apply_source()` (Fase 1) marca como `is_active=false, superseded_by=<nueva source>` las entradas activas previas de cada fecha cubierta e inserta las nuevas, todo en una transacción. Una corrección por texto desactiva solo la entrada afectada. Rechazar = no tocar nada, status `rejected`.

**Nada se da por bueno sin confirmación.** Foto → extracción → resumen con `doubts` + inline keyboard (✅ Confirmar / ✏️ Corregir / ❌ Descartar). `add_entry` y `remove_entry` por texto también pasan por confirmación. Una confirmación pendiente por chat.

**La notificación diaria no usa LLM.** `scheduler/jobs.py` + `services/notify.py` (Fase 2) a las `DAILY_NOTIFY_TIME`: consulta entradas activas de mañana y envía a `NOTIFY_CHAT_IDS`. Si no hay nada, envía un nudge pidiendo foto. Idempotente vía `notifications_log`. Los comandos `/` tampoco dependen del LLM.

**Transporte aislado.** Todo lo de Telegram vive en `app/bot/`. `AuthMiddleware` va como `outer_middleware` de `dp.update` y descarta en silencio cualquier update cuyo `user_id` y `chat_id` no estén ambos en la whitelist (mensajes, ediciones y callbacks).

**Arranque** (`app/main.py`): `load_settings()` → logging → warnings → `harden_environment` → `check_connection` a Postgres (falla rápido) → `build_providers` → `Dispatcher` con `settings`, `providers` y `session_factory` como workflow data → scheduler → polling. En compose el comando es `alembic upgrade head && python -m app.main`.

Esquema SQL en la sección 5 del plan y en `alembic/versions/001_initial.py`; contratos pydantic en la sección 6 y en `app/llm/schemas.py`.

## Variables de entorno

Lista canónica con comentarios en `.env.example`. Validación en `app/config.py`: falla en arranque con mensaje claro si un proveedor seleccionado no tiene sus variables, si un fallback es igual a su principal, o si hora/zona horaria son inválidas.

| Grupo | Variables |
|---|---|
| Telegram | `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`, `ALLOWED_CHAT_IDS`, `NOTIFY_CHAT_IDS` (listas separadas por coma) |
| Selección de proveedor | `LLM_VISION_PROVIDER`, `LLM_TEXT_PROVIDER` (default `claude_sdk`), `LLM_VISION_FALLBACK`, `LLM_TEXT_FALLBACK` (default `none`), `LLM_VISION_TIMEOUT`, `LLM_TEXT_TIMEOUT`, `LLM_RETRY_AFTER_MIN` |
| Ollama | `OLLAMA_BASE_URL`, `OLLAMA_VISION_MODEL`, `OLLAMA_TEXT_MODEL` |
| Claude (suscripción) | `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_TOKEN_ISSUED_AT`, `CLAUDE_SDK_MODEL`, `CLAUDE_SDK_MAX_TURNS` |
| Claude (API key) | `ANTHROPIC_API_KEY`, `ANTHROPIC_API_MODEL` |
| Infra | `DATABASE_URL`, `POSTGRES_USER/PASSWORD/DB` (compose), `DATA_DIR`, `TZ`, `DAILY_NOTIFY_TIME`, `GAP_CHECK_TIME`, `SKIP_WEEKEND`, `LOG_LEVEL`, `LOG_FORMAT` (`console` \| `json`), `BOT_IMAGE_TARGET` (`with-claude` \| `base`) |
| Home Assistant (Fase 5, opcional) | `HA_URL`, `HA_TOKEN`, `HA_NOTIFY_SERVICE` |

## Despliegue

LXC Debian 12 en Proxmox (`nesting=1,keyctl=1`, unprivileged) con Docker Compose. El bot no publica puertos. `Dockerfile` sobre `python:3.12-slim` con `uv sync --frozen`; usuario `bot` (uid 1000) sin privilegios, `/data` es suyo (el bind mount `./data` debe ser escribible por uid 1000). Targets: `base` (solo Ollama/API) y `with-claude` (añade el extra `claude` y verifica en build que el binario empaquetado arranca). El token de suscripción se genera en la laptop con `claude setup-token`, no en el LXC, y caduca al año. Detalles en la sección 9 del plan.

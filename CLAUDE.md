# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Estado del proyecto

`docs/PLAN.md` es la fuente de verdad del diseño (stack, esquema de DB, contratos del LLM, flujos, fases). **Léelo completo antes de escribir código.** Este archivo resume lo operativo; ante cualquier duda de diseño, manda el plan.

Fases (sección 10 del plan):

- **Fase 0 (hecha):** esqueleto, config con validación de proveedores, bot que responde `/start` y `/ping` solo a la whitelist, modelos Django + migración `0001_initial` (con la extensión `vector`), admin de Django embebido en el proceso del bot, abstracción `LLMProvider` con los tres proveedores (healthcheck real; extracción e intención son stubs `NotImplementedError`), `scripts/check_llm.py`, Dockerfile con dos targets, compose, Makefile.
- **Fase 1 (hecha):** foto → `sources` + descarga a `DATA_DIR/photos/{source_id}.jpg` → `extract_from_image` con cadena principal/fallback (también si el resultado es débil) → resumen + botones ✅/✏️/❌ → `apply_source` (merge por fecha en transacción) o rechazo; ✏️ toma el siguiente texto como corrección (`correct_extraction`, cadena de texto); cola de fotos por chat; todo intento en `llm_calls`.
- **Fase 2 (hecha):** `services/notify.py` (sin LLM): a `DAILY_NOTIFY_TIME` lo de mañana a `NOTIFY_CHAT_IDS` o el aviso de agenda vacía (`nudge_empty`), saltando fines de semana si `SKIP_WEEKEND`; domingos a `GAP_CHECK_TIME` los días hábiles de la próxima semana sin entradas (`gap_check`). Idempotente por `notifications_log`; un envío fallido se registra con `ok=false` y se reintenta en la siguiente ejecución. Comando `/manana` con el mismo formato.
- **Caché de respuestas (hecha, entre Fase 2 y 3):** `app/services/cache.py` + tabla `llm_cache`. Coincidencia exacta; un repetido no gasta tokens. Métricas de tokens de caché en `llm_calls`. Migración `0002`.
- **Fase 3 (hecha):** `classify_intent` en los tres proveedores; `services/chat.py` clasifica (con caché) y despacha; consultas por rango, altas y bajas por texto con confirmación ✅/❌ y elección entre candidatas; historial corto por chat en `conversation_messages`; comandos `/hoy`, `/manana`, `/semana`, `/pendiente`, `/ayuda`, `/ping` sin LLM.
- **Fase 4 (hecha):** reintento automático tras cuota agotada (estado en `sources`, sobrevive a reinicios), retención de fotos, `/estado`, `scripts/backup.sh` y runbook en el README.
- **Fase 5 (opcional):** Home Assistant.

Decisión del usuario: **proveedor principal = suscripción Claude (`claude_sdk`)** para visión y texto. Ollama y API key quedan implementados para cambiar solo por variables de entorno.

Decisión del usuario (2026-09-02): **persistencia con Django** (ORM, migraciones y admin) en lugar de SQLAlchemy + Alembic, para tener el admin como panel de operación sin construir UI. El admin corre **dentro del proceso del bot**. Postgres 18 con `pgvector` (extensión creada en 0001; todavía no hay columnas vector, se decidirán más adelante).

## Qué es

`agenda-escolar-bot`: bot de Telegram que recibe fotos de la agenda escolar de un niño, extrae entradas por fecha con un LLM, las guarda en Postgres previa **confirmación del usuario**, envía una notificación diaria a las 19:00 con lo de mañana y responde preguntas en lenguaje natural. Self-hosted en Proxmox. Un niño, un colegio, dos padres en whitelist.

## Forma de trabajar (obligatorio)

- Avanzar **fase por fase**. Al terminar cada fase: correr `make check`, hacer commit con mensaje `feat(faseN): ...` y **detenerse** para revisión antes de continuar.
- **Preguntar antes de:** cambiar una librería principal, cambiar el esquema de la DB después de la Fase 2, o agregar un servicio nuevo al `docker-compose`.
- No inventar credenciales ni IDs de Telegram; `.env.example` lleva placeholders.
- El Agent SDK cambia: antes de tocar `app/llm/claude_sdk.py` revisar la documentación vigente (`code.claude.com/docs/en/agent-sdk/python.md`, `structured-outputs.md`, `hosting.md`) o inspeccionar `claude_agent_sdk/types.py` en `.venv`.
- Mantener este archivo actualizado cuando cambien comandos, variables de entorno o convenciones.

## Stack

Python 3.12 · `aiogram` 3.x con **long polling** · PostgreSQL 18 + `pgvector` (imagen `pgvector/pgvector:0.8.6-pg18-trixie`) · **Django 6.1** (ORM, migraciones y admin) con `psycopg` 3 (`binary` + `pool`) · `uvicorn` embebido para el admin · APScheduler (`AsyncIOScheduler`, en el mismo proceso del bot) · `pydantic-settings` · `structlog` · `uv` para dependencias (`uv.lock` versionado) · Docker Compose (servicios `bot` y `postgres`). Ollama, si se usa, corre en otro host de la red interna.

## Comandos

```
make dev              # docker compose up --build + logs (aplica migraciones al arrancar)
make down / up / logs / ps / shell
make migrate          # manage.py migrate dentro del contenedor
make manage cmd="..." # cualquier comando de manage.py en el contenedor (p. ej. changepassword admin)
make check-llm        # scripts/check_llm.py dentro del contenedor
make install          # uv sync --all-extras (local)
make makemigrations   # manage.py makemigrations agenda (local; escribe en app/db/migrations)
make migrations-check # falla si los modelos tienen cambios sin migración
make test-db-up/down  # Postgres desechable de tests (docker-compose.test.yml, 127.0.0.1:5533)
make test             # pytest completo (levanta el Postgres de tests; necesita Docker)
make test-unit        # pytest -m "not django_db" (sin Docker)
make lint / fmt       # ruff
make typecheck        # mypy --strict sobre app, scripts, tests y manage.py
make check            # lint + typecheck + migrations-check + test
make check-llm-local  # check_llm.py con el .env local
scripts/backup.sh     # pg_dump nocturno (cron del host, no del contenedor)
```

Un solo test: `uv run pytest tests/test_config.py::test_defaults_use_claude_subscription`.

`scripts/check_llm.py` corre `healthcheck()` de cada proveedor referenciado en la config y luego una llamada mínima de texto y de visión (sobre `tests/fixtures/agenda_sample.jpg`). Para `claude_sdk` el healthcheck **es** una llamada real: valida token, arranque del subproceso y salida JSON. Ejecutarlo al cambiar de modelo o proveedor.

## Convenciones de código

- Comentarios y docstrings en **español**; nombres de variables, funciones y clases en **inglés**.
- Todo lo que dependa de fecha/hora usa `settings.zoneinfo` (`ZoneInfo("America/Bogota")`, sin horario de verano). Django corre con `USE_TZ=True` y `TIME_ZONE` desde la misma variable `TZ`.
- Nada de SQL fuera de `app/db/repo.py`.
- **ORM de Django en código async:** lecturas y escrituras simples con los métodos `a*` (`aget`, `acreate`, `async for`); escrituras multi-sentencia como **función sync con `transaction.atomic()` envuelta en `sync_to_async`** (las transacciones no son async). `select_related` antes de tocar una FK desde código async. Nunca `DJANGO_ALLOW_ASYNC_UNSAFE`. Todo handler pasa por `DjangoDBMiddleware` y todo job de APScheduler se decora con `db_job` (hilo y conexión propios por update/job + `close_old_connections`).
- `django.setup()` se hace **solo** en `app/django_bootstrap.setup_django()`, llamado por los entrypoints (`app/main.py`, `manage.py`, `app/asgi.py`) antes de importar modelos. Nunca desde `app/db/__init__.py`.
- Los modelos usan `TextChoices` + `CheckConstraint` (no ENUM de Postgres), `db_default=Now()` en `created_at`, `on_delete=PROTECT` en todas las FK (nada se borra) y `db_table`/`db_column` para que el SQL de la sección 5 del plan siga siendo cierto.
- Prompts en `app/llm/prompts/*.md`, cargados con `load_prompt(name)`, **compartidos por los tres proveedores**; cada proveedor solo cambia cómo los envía y cómo obtiene el JSON.
- `ruff` (line-length 100), `mypy --strict` con los plugins de pydantic y django-stubs (`django_stubs_ext.monkeypatch()` vive en `app/django_settings.py` porque el plugin importa ese módulo); `pytest-asyncio` en modo `auto` + `pytest-django`.
- Tests: `tests/conftest.py::make_settings` construye `Settings` sin leer `.env`; `query()` del SDK se sustituye con `monkeypatch`. Los tests que tocan la DB llevan `pytest.mark.django_db(transaction=True)` (obligatorio en tests `async def`: el ORM async corre en otro hilo/conexión que la transacción de test) y usan `tests/django_settings_test.py` (sin pool).

## Arquitectura (lo que hay que entender antes de tocar código)

**Regla de oro del LLM:** el modelo **nunca ejecuta nada**. Solo clasifica intención y extrae datos en JSON validado con pydantic (`ExtractionResult`, `Intent` en `app/llm/schemas.py`). Toda la lógica la ejecutan handlers Python deterministas en `app/services/`. Si el JSON no valida: un reintento con el error en el prompt; si vuelve a fallar, `LLMOutputError` y fallback si está configurado. Esto es a la vez la defensa contra prompt injection (texto de Telegram y texto dentro de fotos son entrada no confiable) y la forma de que funcione con modelos abiertos pequeños sin function calling.

**Proveedores intercambiables por tarea.** `app/llm/provider.py` define el Protocol `LLMProvider` (`extract_from_image`, `correct_extraction`, `classify_intent`, `healthcheck`, atributo `last_usage`), la jerarquía de errores (`LLMError` → `LLMUnavailableError`, `LLMQuotaError`, `LLMOutputError`), `build_provider(name, settings)` con imports perezosos y `FallbackProvider`, que encadena principal → fallback y aplica el timeout de la tarea. `FallbackProvider.run(call, accept=...)` devuelve `LLMRun(value, provider, attempts)`: un `LLMAttempt` por proveedor probado (ok/error/usage) para registrar en `llm_calls`; con `accept` también se va al fallback cuando el principal responde pero el resultado es débil; si todo falla, el `LLMError` lleva `.attempts`. Las llamadas de una cadena se serializan con un lock (una a la vez, por diseño). `build_providers(settings)` devuelve `LLMProviders(vision, text)`; se inyecta en los handlers vía `Dispatcher(providers=...)`. `NotImplementedError` (stubs) se propaga sin pasar por el fallback. Los prompts se rellenan en `app/llm/prompting.py` (fecha, día de la semana, JSON de contexto) y la validación con un reintento vive en `app/llm/json_out.py::validate_with_retry` (el error de pydantic va en el prompt del reintento). Tres implementaciones:

- `ollama.py`: SDK `openai` contra `OLLAMA_BASE_URL/v1`; imagen como data URL base64; `response_format` json_schema. Healthcheck: lista modelos y exige que los dos configurados estén descargados.
- `claude_sdk.py`: Claude Agent SDK con la suscripción (`CLAUDE_CODE_OAUTH_TOKEN`). Es Claude Code headless como subproceso; el binario viene **empaquetado dentro del wheel** de `claude-agent-sdk` (`_bundled/claude`), no se instala aparte. `_run_json()` centraliza toda llamada: `tools=[]` para texto (ninguna herramienta) y `tools=["Read"]` para visión con `cwd` en la carpeta de la foto y ruta relativa `./{source_id}.jpg`; `disallowed_tools` explícito (`Bash`, `Write`, `Edit`, `WebSearch`, `WebFetch`, `Agent`, ...); `setting_sources=[]`; sesión nueva por llamada; `output_format` con el JSON schema pydantic y el resultado en `ResultMessage.structured_output`. El entorno del subproceso lleva solo el token, `CLAUDE_CONFIG_DIR=$DATA_DIR/claude` y `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`. `ResultError` con status 429 o texto de límite se traduce a `LLMQuotaError`; el resto a `LLMUnavailableError`.
- `anthropic_api.py`: SDK `anthropic` con la key explícita; imagen base64 nativa; salida estructurada forzando una herramienta `emit` cuyo `input_schema` es el schema pydantic (el modelo no ejecuta nada). Healthcheck: `models.retrieve` (no gasta tokens).

**Credenciales de Claude.** En Claude Code `ANTHROPIC_API_KEY` tiene precedencia sobre `CLAUDE_CODE_OAUTH_TOKEN`, y el subproceso del SDK hereda el entorno del proceso. Por eso `config.harden_environment()` retira `ANTHROPIC_API_KEY` y `ANTHROPIC_AUTH_TOKEN` de `os.environ` en arranque cuando `claude_sdk` está en uso; `anthropic_api` recibe la key desde `settings`, no del entorno. `startup_warnings()` avisa si ambas están definidas. No usar `--bare` (no lee el token).

**Configuración en dos capas.** `app/config.py` tiene `DjangoSettings` (DB, admin, `TZ`; con defaults, importable sin `.env`) y `Settings(DjangoSettings)` (todo el bot; `DATABASE_URL` obligatoria, valida proveedores, exige `DJANGO_SECRET_KEY` real si `ADMIN_ENABLED=true`). `app/django_settings.py` deriva los settings de Django de `DjangoSettings` y parsea `DATABASE_URL` con `database_from_url()` (`CONN_MAX_AGE=0`, pool de psycopg).

**Caché de respuestas del LLM.** `app/services/cache.py` (capa de servicios, no dentro de los proveedores: así el resultado de cualquier proveedor es reutilizable y la decisión "caché vs. LLM" queda visible). Coincidencia **exacta**: `key = sha256({task, prompt_version, today, tz, inputs})`, guardada en `llm_cache` con TTL (`LLM_CACHE_TTL_DAYS`, default 30; `LLM_CACHE_ENABLED=false` la apaga). `prompt_version` es el hash de los `.md` de prompts + los JSON schema, así que editar un prompt invalida todo solo. La fecha va en la clave: una consulta con fechas relativas no se sirve al día siguiente. `inputs`: visión → sha256 de los bytes de la imagen; corrección → extracción + texto normalizado (espacios y mayúsculas). Un acierto se registra en `llm_calls` con `provider="cache"` y cero tokens, mientras `sources.llm_provider` conserva el proveedor original para que las estadísticas no mientan. **❌ Descartar invalida la entrada** (`sources.llm_cache_key` → `cache.invalidate`): quien descarta suele hacerlo porque la lectura estaba mal, así que reenviar la foto debe volver a leerla.

**El prompt caching de Anthropic NO se usa, a propósito.** Con este perfil (llamadas separadas por horas contra un TTL de 5 minutos, prefijo estático de ~900 tokens por debajo del mínimo de 1024 de Sonnet, e imágenes únicas) no cachearía nada y además falla en silencio. Los prompts sí están ordenados con lo estático primero y el bloque `CONTEXTO` al final por si algún día cambia el modelo (Opus 5 baja el mínimo a 512) o crecen los prompts; decidirlo con los datos de `cache_read_tokens` / `cache_write_tokens`, no de memoria.

**Datos: nunca borrar, siempre versionar.** Cada foto o corrección es una fila en `sources`. Al confirmar una source, `services/agenda.apply_source()` (Fase 1) marca como `is_active=false, superseded_by=<nueva source>` las entradas activas previas de cada fecha cubierta e inserta las nuevas, todo en una transacción. Una corrección por texto desactiva solo la entrada afectada. Rechazar = no tocar nada, status `rejected`. El admin tampoco permite borrar (`has_delete_permission` → `False`, sin `delete_selected`); `users` y `agenda_entries` son editables, el resto solo lectura.

**Nada se da por bueno sin confirmación.** Foto → extracción → resumen con `doubts` + inline keyboard (✅ Confirmar / ✏️ Corregir / ❌ Descartar). `add_entry` y `remove_entry` por texto también pasan por confirmación. Una confirmación pendiente por chat: `services/confirm.PendingStore` (en memoria, inyectado como `pending` en el `Dispatcher`) guarda por chat la `Pending(source_id, extraction, awaiting_correction)` y una cola de fotos que llegaron mientras tanto; al confirmar o descartar se procesa la siguiente. `services/ingest.py` hace el flujo de la foto sin hablar con Telegram (recibe la función de descarga y lanza `IngestError` con mensaje apto para el chat); `services/agenda.py` aplica o rechaza; `app/llm/compose.py` arma los textos (HTML, sin LLM); los handlers en `app/bot/handlers/{photo,callbacks,text}.py` son finos. Los botones usan `CallbackData` con prefijo `src` y comprueban que la `source_id` sea la pendiente (botones viejos se desactivan). Si el bot se reinicia con una confirmación pendiente, esa confirmación se pierde (vive en memoria) pero **la foto no**: la source sigue `pending` en la DB y, si nunca llegó a leerse, `retry_photos_job` la recupera y vuelve a preguntar en su chat (Fase 4). Si ya estaba leída y solo faltaba el ✅, hay que reenviarla.

**Texto libre (Fase 3).** `handlers/text.py` es fino y ordena así: si hay una foto pendiente y se pulsó ✏️, el mensaje es la corrección y no pasa por el clasificador; si no, lee el historial (`repo.recent_history`, 6 turnos), guarda el mensaje y llama a `services/chat.classify` (cadena de texto + caché con `task="intent"`). Con algo pendiente, `confirm` / `reject` / `correct_pending` hacen exactamente lo mismo que los botones porque ambos caminos llaman a `app/bot/actions.py` (un "sí" escrito y un ✅ pulsado no pueden divergir). Si no hay nada pendiente, `chat.dispatch` resuelve `query_range` / `add_entry` / `remove_entry` / `help` / `unknown` y devuelve un `ChatReply(text, edit, candidates)` sin tocar aiogram; el handler le pone el teclado. **Si el LLM falla, el texto libre responde `compose.NO_LLM_TEXT`** y remite a los comandos.

**Altas y bajas por texto son aditivas y versionadas.** `agenda.add_entry` crea una source `text_correction` e inserta **una** entrada con `repo.add_single_entry` (no reemplaza el día, a diferencia de una foto). `agenda.remove_entry` desactiva **solo** esa entrada con `superseded_by` a la nueva source. Nada de esto ocurre sin confirmación: `chat.prepare_add` / `prepare_remove` solo dejan un `PendingEdit`. Con varias candidatas (`repo.find_active_entries` filtra por `icontains` con las palabras de 4+ letras de `target_entry_hint`, y si no casa ninguna reintenta con el día entero) se ofrecen botones para elegir. `PendingStore` guarda **una** cosa por chat: `PendingPhoto | PendingEdit`; `edit_id` incremental detecta botones de una edición ya resuelta.

**La notificación diaria no usa LLM.** `scheduler/jobs.py` registra dos `CronTrigger` con `settings.zoneinfo` (`daily_notify` diario, `gap_check` domingos; `misfire_grace_time=3600`, `coalesce`) que llaman a `services/notify.py` con un `Sender` (`bot.send_message`) inyectado. `send_daily(send, settings, today)`: `daily_target` (mañana, o `None` en fin de semana con `SKIP_WEEKEND`) → `build_daily_message` (`daily` con formato de 7.3 agrupado por tipo, o `nudge_empty` pidiendo foto) → un envío por chat de `NOTIFY_CHAT_IDS`, saltando los que ya tienen un envío `ok` de `daily` o `nudge_empty` para esa fecha. `send_gap_check`: lunes a viernes de la semana siguiente sin entradas vigentes; si no hay huecos no envía nada; `target_date` = lunes de esa semana. Todo queda en `notifications_log` (unique parcial `notif_log_ok_unique` sobre `(kind, target_date, chat_id) WHERE ok`). `/manana` reutiliza `build_daily_message` sin registrar nada. Los comandos `/` tampoco dependen del LLM.

**Robustez (Fase 4).** Una foto que llega sin cuota **no se pierde**: `extract_photo` deja la source `pending` sin `raw_llm_output` (a diferencia de cualquier otro `LLMError`, que sí la marca `failed`), y ese estado —`photo` + `pending` + `local_path` + sin salida— es justo lo que busca `retry_photos_job` cada `LLM_RETRY_AFTER_MIN` minutos. Como el estado vive en la DB y `sources.chat_id` guarda el chat, **el reintento sobrevive a un reinicio**. Tras `RETRY_GIVE_UP_HOURS` se marca `failed` y se avisa una vez. `purge_photos_job` (04:17) borra el **archivo** de fotos ya resueltas con más de `PHOTO_RETENTION_DAYS` días: la fila, la extracción cruda y las entradas se conservan.

**Comandos sin LLM** (`handlers/commands.py`): `/hoy`, `/manana` (formato de la notificación, sin registrarla), `/semana` (de hoy al domingo; en fin de semana ya mira a la siguiente, `chat.week_range`), `/pendiente`, `/estado`, `/ayuda`, `/ping`. `/estado` (`services/status.py`) es **barato a propósito**: deduce la salud de cada proveedor de la última fila de `llm_calls` en vez de hacer healthchecks, porque con `claude_sdk` un healthcheck es una llamada real y descontaría cuota cada vez. `/estado check` sí los hace. Son la red de seguridad cuando la IA está caída y no deben depender nunca de un proveedor.

**Transporte aislado.** Todo lo de Telegram vive en `app/bot/`. `AuthMiddleware` va como `outer_middleware` de `dp.update` y descarta en silencio cualquier update cuyo `user_id` y `chat_id` no estén ambos en la whitelist (mensajes, ediciones y callbacks). `DjangoDBMiddleware` va después, para que los rechazados no toquen la DB.

**Admin embebido.** `app/web.py` construye un `uvicorn.Server` (subclase que no toca las señales del proceso; `lifespan="off"`, `log_config=None`) sobre `app/asgi.py` (`ASGIStaticFilesHandler(get_asgi_application())`, sin `collectstatic`). Solo `admin/` (`app/admin_urls.py`). Cada request del admin corre en su propio hilo (`ASGIHandler` + `ThreadSensitiveContext`), sin bloquear al bot. Superusuario creado en arranque por `repo.ensure_superuser` si están `DJANGO_SUPERUSER_USERNAME/PASSWORD` (no reescribe la contraseña).

**Arranque** (`app/main.py`): `setup_django()` → `load_settings()` → logging → warnings → `harden_environment` → `repo.check_connection()` (falla rápido) → `ensure_superuser` → `build_providers` → `Dispatcher` con `settings` y `providers` como workflow data → middlewares → scheduler → `asyncio.TaskGroup` con `dp.start_polling(handle_signals=False)`, `server.serve()` y una tarea que espera SIGINT/SIGTERM y apaga ambos (`server.should_exit`, `dp.stop_polling()`). En compose el comando es `python manage.py migrate --noinput && python -m app.main`.

Esquema SQL en la sección 5 del plan; modelos en `app/db/models.py` y migración en `app/db/migrations/0001_initial.py`; contratos pydantic en la sección 6 y en `app/llm/schemas.py`.

## Variables de entorno

Lista canónica con comentarios en `.env.example`. Validación en `app/config.py`: falla en arranque con mensaje claro si un proveedor seleccionado no tiene sus variables, si un fallback es igual a su principal, si hora/zona horaria son inválidas, si `DATABASE_URL` lleva dialecto de SQLAlchemy, si el admin está habilitado sin `DJANGO_SECRET_KEY`, o si el superusuario tiene usuario sin contraseña.

| Grupo | Variables |
|---|---|
| Telegram | `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`, `ALLOWED_CHAT_IDS`, `NOTIFY_CHAT_IDS` (listas separadas por coma) |
| Selección de proveedor | `LLM_VISION_PROVIDER`, `LLM_TEXT_PROVIDER` (default `claude_sdk`), `LLM_VISION_FALLBACK`, `LLM_TEXT_FALLBACK` (default `none`), `LLM_VISION_TIMEOUT`, `LLM_TEXT_TIMEOUT`, `LLM_RETRY_AFTER_MIN` |
| Caché del LLM | `LLM_CACHE_ENABLED` (default `true`), `LLM_CACHE_TTL_DAYS` (default 30; 0 desactiva el guardado) |
| Robustez | `PHOTO_RETENTION_DAYS` (default 90), `RETRY_GIVE_UP_HOURS` (default 24) |
| Ollama | `OLLAMA_BASE_URL`, `OLLAMA_VISION_MODEL`, `OLLAMA_TEXT_MODEL` |
| Claude (suscripción) | `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_TOKEN_ISSUED_AT`, `CLAUDE_SDK_MODEL`, `CLAUDE_SDK_MAX_TURNS` |
| Claude (API key) | `ANTHROPIC_API_KEY`, `ANTHROPIC_API_MODEL` |
| Infra | `DATABASE_URL` (`postgresql://user:pass@host:5432/db`), `POSTGRES_USER/PASSWORD/DB` (compose), `DATA_DIR`, `TZ`, `DAILY_NOTIFY_TIME`, `GAP_CHECK_TIME`, `SKIP_WEEKEND`, `LOG_LEVEL`, `LOG_FORMAT` (`console` \| `json`), `BOT_IMAGE_TARGET` (`with-claude` \| `base`) |
| Admin (Django, solo LAN) | `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `ADMIN_ENABLED`, `ADMIN_HOST`, `ADMIN_PORT`, `ADMIN_BIND` (IP del host donde compose publica el puerto), `DJANGO_SUPERUSER_USERNAME/PASSWORD/EMAIL` |
| Home Assistant (Fase 5, opcional) | `HA_URL`, `HA_TOKEN`, `HA_NOTIFY_SERVICE` |

## Despliegue

LXC **Debian 13** en Proxmox (`nesting=1,keyctl=1`, unprivileged) con Docker Compose. Desplegado el 2026-09-02 en el nodo `hades` como **VMID 109 `agenda-bot`**, IP fija **10.70.70.60/24** (gw .1, `vmbr0`, firewall en la NIC), 2 vCPU / 2 GiB / 20 GiB en `local-lvm`, `onboot=1`. El código vive en `/opt/agenda-escolar-bot` clonado de GitHub; se actualiza con `git pull`. El único puerto publicado es el del admin (`ADMIN_BIND:ADMIN_PORT`, default `0.0.0.0:8000`), **solo en la LAN**; nunca hacer port-forward hacia internet. `Dockerfile` sobre `python:3.12-slim` con `uv sync --frozen`; usuario `bot` (uid 1000) sin privilegios, `/data` es suyo (el bind mount `./data` debe ser escribible por uid 1000). Targets: `base` (solo Ollama/API) y `with-claude` (añade el extra `claude` y verifica en build que el binario empaquetado arranca). Postgres 18: el volumen `pgdata` se monta en `/var/lib/postgresql` (cambió respecto a las imágenes ≤17); `CREATE EXTENSION vector` en 0001 necesita superusuario de Postgres (el de compose lo es). El token de suscripción se genera en la laptop con `claude setup-token`, no en el LXC, y caduca al año. Detalles en la sección 9 del plan.

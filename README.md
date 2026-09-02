# agenda-escolar-bot

Bot de Telegram que recibe fotos de la agenda escolar, extrae las entradas con un LLM,
las guarda en Postgres previa confirmación y avisa cada tarde lo de mañana.

El diseño completo está en `docs/PLAN.md`; las instrucciones operativas en `CLAUDE.md`.

## Puesta en marcha

1. `cp .env.example .env` y completar `TELEGRAM_BOT_TOKEN`, los IDs de Telegram,
   `CLAUDE_CODE_OAUTH_TOKEN` (generarlo en tu laptop con `claude setup-token`),
   `DJANGO_SECRET_KEY` y las credenciales `DJANGO_SUPERUSER_*` del admin.
2. `make dev` levanta Postgres y el bot, aplica migraciones y sigue los logs.
3. `make check-llm` comprueba que el proveedor de LLM configurado responde.
4. Escribir `/ping` al bot desde un chat en la whitelist: responde `pong`.
5. Admin de Django en `http://<ip-del-lxc>:8000/admin/` (solo LAN), con el superusuario del `.env`.

## Uso

Manda una **foto** de la agenda y el bot muestra lo que entendió antes de guardar nada
(✅ Confirmar / ✏️ Corregir / ❌ Descartar). También entiende texto:

- «¿qué hay mañana?», «¿qué lleva el viernes?», «¿qué hay esta semana?»
- «agrega que el martes lleva disfraz» · «quita lo del jueves» (ambas piden confirmación)

Comandos, que funcionan aunque la IA esté caída: `/hoy` `/manana` `/semana` `/pendiente`
`/ayuda` `/ping`. Cada tarde a las 19:00 avisa lo de mañana; los domingos, los días de la
próxima semana sin agenda.

## Cambiar de proveedor de LLM

Solo variables de entorno, sin tocar código:

```
LLM_VISION_PROVIDER=claude_sdk   # ollama | claude_sdk | anthropic_api
LLM_TEXT_PROVIDER=claude_sdk
LLM_VISION_FALLBACK=none
LLM_TEXT_FALLBACK=none
```

Cada proveedor exige sus variables (`OLLAMA_BASE_URL`, `CLAUDE_CODE_OAUTH_TOKEN`,
`ANTHROPIC_API_KEY`); el arranque falla con un mensaje claro si falta alguna.
Si no se usa `claude_sdk`, `BOT_IMAGE_TARGET=base` produce una imagen sin el binario de Claude Code.

## Desarrollo local

```
make install     # uv sync
make check       # ruff + mypy + migraciones + pytest (levanta un Postgres desechable con Docker)
make test-unit   # solo los tests que no necesitan Postgres
```

Migraciones: los modelos viven en `app/db/models.py`; `make makemigrations` genera la migración
y `make migrate` la aplica en el contenedor.

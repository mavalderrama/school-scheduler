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
5. Admin de Django en `http://10.70.70.60:8000/admin/` (solo LAN), con el superusuario del `.env`.

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

## Runbook

### Cambiar de proveedor o de modelo

1. Editar `LLM_VISION_PROVIDER` / `LLM_TEXT_PROVIDER` (o `CLAUDE_SDK_MODEL`,
   `OLLAMA_VISION_MODEL`, `ANTHROPIC_API_MODEL`) en `.env`.
2. `make check-llm` para confirmar que el proveedor nuevo responde de verdad.
3. `make up` (o `make build && make up` si cambió `BOT_IMAGE_TARGET`).

Cambiar de modelo invalida la caché solo si cambian los prompts; las entradas viejas
siguen sirviéndose. Para forzar lectura nueva, borra las filas de `llm_cache` en el admin.

### Rotar el token de la suscripción (caduca al año)

1. En la laptop, **no** en el LXC: `claude setup-token`.
2. Copiar la salida a `CLAUDE_CODE_OAUTH_TOKEN` en el `.env` del LXC y poner la fecha de
   hoy en `CLAUDE_TOKEN_ISSUED_AT`.
3. `make up && make check-llm`. `/estado` avisa cuando quedan menos de 30 días.

### Copias de seguridad

`scripts/backup.sh` corre en el cron del **host** (no dentro del bot: una copia que
depende de que la app esté sana es la que falta cuando hace falta):

```
0 3 * * *  cd /opt/agenda-escolar-bot && ./scripts/backup.sh >> /var/log/agenda-backup.log 2>&1
```

Guarda en `data/backups/` y rota a 14 días. Restaurar:

```
gunzip -c data/backups/agenda-YYYYmmdd-HHMM.sql.gz | \
  docker compose exec -T postgres psql -U agenda -d agenda
```

### Agregar un usuario

1. Pedirle su id a @userinfobot y añadirlo a `ALLOWED_USER_IDS` (y el chat a
   `ALLOWED_CHAT_IDS`; si debe recibir la notificación diaria, a `NOTIFY_CHAT_IDS`).
2. `make up`. La fila en `users` se crea sola la primera vez que mande algo; el nombre y
   el rol se pueden editar en el admin.

### Qué mirar cuando algo va mal

- `/estado` en Telegram: última notificación, últimas fuentes, consumo del mes por
  proveedor, fotos esperando cuota y vencimiento del token. `/estado check` además hace
  un healthcheck real (gasta una llamada).
- `make logs`, y el admin en `http://10.70.70.60:8000/admin/` para ver `sources`,
  `llm_calls` y `notifications_log`.
- Una foto que llegó en un momento sin cuota se reintenta sola; no hay que hacer nada.

## Desarrollo local

```
make install     # uv sync
make check       # ruff + mypy + migraciones + pytest (levanta un Postgres desechable con Docker)
make test-unit   # solo los tests que no necesitan Postgres
```

Migraciones: los modelos viven en `app/db/models.py`; `make makemigrations` genera la migración
y `make migrate` la aplica en el contenedor.

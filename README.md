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
(✅ Confirmar / ✏️ Corregir / ❌ Descartar). Si le falta un dato imprescindible, **pregunta
antes de guardar** en vez de adivinar. También reconoce la **tabla del horario** (Semana A /
Semana B) y a partir de ella calcula qué clase toca cada día. También entiende texto:

- «¿qué hay mañana?», «¿qué lleva el viernes?», «¿qué hay esta semana?»
- «¿cuándo hay natación?»
- «agrega que el martes lleva disfraz» · «quita lo del jueves» (ambas piden confirmación)

Comandos, que funcionan aunque la IA esté caída: `/hoy` `/manana` `/semana` `/horario`
`/pendiente` `/estado` `/ayuda` `/ping`. Cada tarde a las 19:00 avisa lo de mañana (con la
clase del horario); los domingos, los días de la próxima semana sin agenda.

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

### Cargar el horario rotativo

1. Mandar al bot una foto de la tabla del horario (Semana A / Semana B).
2. El bot pregunta **qué lunes empezó la Semana A**. Vale responder con cualquier día de
   esa semana («el martes 1 de septiembre»): él saca el lunes.
3. Confirmar con ✅. A partir de ahí `/horario`, `/hoy`, `/manana`, `/semana` y la
   notificación de las 19:00 dicen qué clase toca.

Pueden convivir **varios horarios** (por ejemplo la rotación académica y el programa de la
jornada extendida): si ya hay alguno cargado, el bot pregunta si el nuevo se **añade aparte**
o **reemplaza** a uno concreto. Lo reemplazado se conserva desactivado, no se borra.

Lo que escribas **junto a la foto** se usa como contexto para leerla («este es el horario del
PAC»), así que conviene decirle qué es cuando no sea obvio.

### Días sin clase

Los festivos nacionales de Colombia salen solos de la librería `holidays`, incluidos los
que se corren al lunes. Lo que solo sabe el colegio —semana de receso, jornadas
pedagógicas, día de la familia— se carga a mano en el admin, en **excepciones del
calendario**:

- `school_closed`: no hay clase ese día.
- `class_day`: sí hay clase, aunque sea festivo nacional.

Un festivo **solo cancela ese día**: la semana sigue siendo la A o la B que le toca por
calendario, y esa rotación se pierde esa vuelta. Si no se cargan los días sin clase, el
bot anunciará clases en días en los que no hay colegio.

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

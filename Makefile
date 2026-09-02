.DEFAULT_GOAL := help
COMPOSE ?= docker compose
COMPOSE_TEST ?= docker compose -f docker-compose.test.yml
# makemigrations consulta el historial de migraciones; usa el Postgres de tests para no esperar.
TEST_DATABASE_URL ?= postgresql://agenda:agenda@127.0.0.1:5533/agenda

help: ## Lista los targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

# --- Contenedores ---------------------------------------------------------------
dev: ## Construye y levanta bot + postgres, y sigue los logs del bot
	mkdir -p data
	$(COMPOSE) up --build -d
	$(COMPOSE) logs -f bot

up: ## Levanta sin reconstruir
	$(COMPOSE) up -d

down: ## Para todo (conserva volúmenes)
	$(COMPOSE) down

build: ## Reconstruye la imagen del bot
	$(COMPOSE) build bot

logs: ## Logs del bot
	$(COMPOSE) logs -f bot

ps: ## Estado de los servicios
	$(COMPOSE) ps

shell: ## Shell dentro del contenedor del bot
	$(COMPOSE) run --rm bot bash

migrate: ## Aplica migraciones (manage.py migrate) en el contenedor
	$(COMPOSE) run --rm bot python manage.py migrate --noinput

manage: ## Cualquier comando de manage.py en el contenedor: make manage cmd="changepassword admin"
	$(COMPOSE) run --rm bot python manage.py $(cmd)

check-llm: ## Verifica los proveedores de LLM configurados (dentro del contenedor)
	$(COMPOSE) run --rm bot python scripts/check_llm.py

# --- Local (uv) -----------------------------------------------------------------
install: ## Instala dependencias locales con uv (incluye extras y dev)
	uv sync --all-extras

makemigrations: test-db-up ## Nueva migración a partir de los modelos (escribe en app/db/migrations)
	DATABASE_URL=$(TEST_DATABASE_URL) uv run python manage.py makemigrations agenda

migrations-check: test-db-up ## Falla si los modelos tienen cambios sin migración
	DATABASE_URL=$(TEST_DATABASE_URL) uv run python manage.py makemigrations --check --dry-run

test-db-up: ## Levanta el Postgres desechable de tests (127.0.0.1:5533)
	$(COMPOSE_TEST) up -d --wait

test-db-down: ## Tira el Postgres de tests
	$(COMPOSE_TEST) down -v

test: test-db-up ## pytest completo (necesita Docker para el Postgres de tests)
	uv run pytest

test-unit: ## pytest sin los tests que necesitan Postgres (no necesita Docker)
	uv run pytest -m "not django_db"

lint: ## ruff check + format --check
	uv run ruff check .
	uv run ruff format --check .

fmt: ## ruff format + fixes automáticos
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## mypy --strict
	uv run mypy app scripts tests manage.py

check: lint typecheck migrations-check test ## lint + typecheck + migraciones + test

check-llm-local: ## check_llm.py con el .env local (sin contenedor)
	uv run python scripts/check_llm.py

.PHONY: help dev up down build logs ps shell migrate manage check-llm install makemigrations migrations-check test-db-up test-db-down test test-unit lint fmt typecheck check check-llm-local

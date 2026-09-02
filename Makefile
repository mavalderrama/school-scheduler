.RECIPEPREFIX := >
.DEFAULT_GOAL := help
COMPOSE ?= docker compose

help: ## Lista los targets
> @grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

# --- Contenedores ---------------------------------------------------------------
dev: ## Construye y levanta bot + postgres, y sigue los logs del bot
> mkdir -p data
> $(COMPOSE) up --build -d
> $(COMPOSE) logs -f bot

up: ## Levanta sin reconstruir
> $(COMPOSE) up -d

down: ## Para todo (conserva volúmenes)
> $(COMPOSE) down

build: ## Reconstruye la imagen del bot
> $(COMPOSE) build bot

logs: ## Logs del bot
> $(COMPOSE) logs -f bot

ps: ## Estado de los servicios
> $(COMPOSE) ps

shell: ## Shell dentro del contenedor del bot
> $(COMPOSE) run --rm bot bash

migrate: ## Aplica migraciones (alembic upgrade head) en el contenedor
> $(COMPOSE) run --rm bot alembic upgrade head

revision: ## Nueva migración autogenerada: make revision m="descripcion"
> $(COMPOSE) run --rm bot alembic revision --autogenerate -m "$(m)"

check-llm: ## Verifica los proveedores de LLM configurados (dentro del contenedor)
> $(COMPOSE) run --rm bot python scripts/check_llm.py

# --- Local (uv) -----------------------------------------------------------------
install: ## Instala dependencias locales con uv (incluye extras y dev)
> uv sync --all-extras

test: ## pytest
> uv run pytest

lint: ## ruff check + format --check
> uv run ruff check .
> uv run ruff format --check .

fmt: ## ruff format + fixes automáticos
> uv run ruff format .
> uv run ruff check --fix .

typecheck: ## mypy --strict
> uv run mypy app scripts tests alembic

check: lint typecheck test ## lint + typecheck + test

check-llm-local: ## check_llm.py con el .env local (sin contenedor)
> uv run python scripts/check_llm.py

.PHONY: help dev up down build logs ps shell migrate revision check-llm install test lint fmt typecheck check check-llm-local

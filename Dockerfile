# syntax=docker/dockerfile:1
# Dos targets:
#   base        -> solo ollama / anthropic_api
#   with-claude -> además claude-agent-sdk con el binario de Claude Code empaquetado

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 bot \
    && mkdir -p /data /srv/app \
    && chown bot:bot /data /srv/app

WORKDIR /srv/app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts
COPY tests/fixtures ./tests/fixtures
RUN chown -R bot:bot /srv/app

USER bot
CMD ["python", "-m", "app.main"]


FROM base AS with-claude

USER root
# El binario nativo de Claude Code viene dentro del wheel de claude-agent-sdk.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 git \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra claude
# Verificación en build: el binario empaquetado arranca en esta arquitectura.
RUN /opt/venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude --version
USER bot

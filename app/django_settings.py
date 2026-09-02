"""Settings de Django, derivados de `DjangoSettings` (pydantic) para tener una sola fuente.

Este módulo debe importar sin `.env`: lo importan `manage.py`, `makemigrations` y el plugin
de mypy de django-stubs. Por eso usa `DjangoSettings` (con defaults) y no `load_settings()`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlsplit

import django_stubs_ext

from app.config import DjangoSettings

# Permite `ModelAdmin[Model]` y `QuerySet[Model]` en tiempo de ejecución (django-stubs).
# Va aquí y no en django_bootstrap porque el plugin de mypy importa este módulo directamente.
django_stubs_ext.monkeypatch()

_cfg = DjangoSettings()


def database_from_url(url: str) -> dict[str, Any]:
    """Traduce postgresql://user:pass@host:5432/db al dict DATABASES de Django.

    En modo async `CONN_MAX_AGE` debe ser 0 y el reciclado lo hace el pool de psycopg.
    """
    parts = urlsplit(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parts.path.lstrip("/"),
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname or "",
        "PORT": str(parts.port or 5432),
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {"pool": True},
    }


SECRET_KEY = _cfg.django_secret_key
DEBUG = _cfg.django_debug
ALLOWED_HOSTS = _cfg.django_allowed_hosts
CSRF_TRUSTED_ORIGINS = _cfg.django_csrf_trusted_origins

DATABASES = {"default": database_from_url(_cfg.database_url)}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "app.db.apps.AgendaConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "app.admin_urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# Los estáticos del admin los sirve ASGIStaticFilesHandler desde los finders (app/asgi.py).
STATIC_URL = "static/"

LANGUAGE_CODE = "es"
TIME_ZONE = _cfg.tz
USE_I18N = True
USE_TZ = True

# structlog ya enruta el logging estándar (app/log.py); Django no debe reconfigurarlo.
LOGGING_CONFIG = None

# HTTP plano en la LAN: sin cookies Secure ni redirección a HTTPS.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False

"""Config de Django derivada de pydantic-settings (sin DB)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import INSECURE_SECRET_KEY, DjangoSettings
from app.django_settings import database_from_url
from tests.conftest import make_settings


def test_database_from_url_parses_all_parts() -> None:
    db = database_from_url("postgresql://user:p%40ss@db.local:5433/agenda")
    assert db["ENGINE"] == "django.db.backends.postgresql"
    assert db["NAME"] == "agenda"
    assert db["USER"] == "user"
    assert db["PASSWORD"] == "p@ss"
    assert db["HOST"] == "db.local"
    assert db["PORT"] == "5433"
    assert db["CONN_MAX_AGE"] == 0
    assert db["OPTIONS"] == {"pool": True}


def test_database_from_url_defaults_port() -> None:
    assert database_from_url("postgresql://agenda:agenda@postgres/agenda")["PORT"] == "5432"


def test_sqlalchemy_dialect_in_url_is_rejected() -> None:
    with pytest.raises(ValidationError, match="asyncpg"):
        make_settings(database_url="postgresql+asyncpg://agenda:agenda@localhost/agenda")


def test_non_postgres_scheme_is_rejected() -> None:
    with pytest.raises(ValidationError, match="esquema"):
        make_settings(database_url="sqlite:///x.db")


def test_django_settings_import_without_env() -> None:
    """manage.py, makemigrations y mypy importan app/django_settings.py sin .env."""
    cfg = DjangoSettings(_env_file=None)
    assert cfg.django_secret_key == INSECURE_SECRET_KEY
    assert cfg.admin_enabled is True
    assert cfg.django_allowed_hosts == ["*"]


def test_admin_requires_real_secret_key() -> None:
    with pytest.raises(ValidationError, match="DJANGO_SECRET_KEY"):
        make_settings(django_secret_key=INSECURE_SECRET_KEY)


def test_admin_disabled_accepts_insecure_secret_key() -> None:
    settings = make_settings(django_secret_key=INSECURE_SECRET_KEY, admin_enabled=False)
    assert settings.admin_enabled is False


def test_superuser_credentials_go_together() -> None:
    with pytest.raises(ValidationError, match="DJANGO_SUPERUSER_USERNAME"):
        make_settings(django_superuser_username="admin")
    settings = make_settings(django_superuser_username="admin", django_superuser_password="x")
    assert settings.django_superuser_username == "admin"


def test_allowed_hosts_and_origins_split_on_comma() -> None:
    settings = make_settings(
        django_allowed_hosts="192.168.1.50, agenda.lan",
        django_csrf_trusted_origins="http://agenda.lan:8000",
    )
    assert settings.django_allowed_hosts == ["192.168.1.50", "agenda.lan"]
    assert settings.django_csrf_trusted_origins == ["http://agenda.lan:8000"]

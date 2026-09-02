"""Arranque de Django fuera de manage.py.

`setup_django()` es idempotente y hay que llamarla en cada entrypoint (app/main.py,
manage.py, app/asgi.py, scripts) **antes** de importar cualquier módulo con modelos.
No llamarla desde `app/db/__init__.py`: `apps.populate()` no es reentrante.
"""

from __future__ import annotations

import os

import django
from django.apps import apps

DEFAULT_SETTINGS_MODULE = "app.django_settings"


def setup_django() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", DEFAULT_SETTINGS_MODULE)
    if not apps.ready:
        django.setup()

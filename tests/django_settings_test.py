"""Settings de Django para pytest: apunta al Postgres desechable de docker-compose.test.yml.

pytest-django hace `django.setup()` antes de importar `conftest.py`, así que la URL de test
tiene que fijarse aquí. Una variable de entorno DATABASE_URL explícita sigue ganando.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://agenda:agenda@127.0.0.1:5533/agenda")

from app.django_settings import *  # noqa: F403

# Sin pool en tests: pytest-django no puede borrar `test_agenda` si el pool mantiene sesiones.
DATABASES["default"]["OPTIONS"] = {}  # noqa: F405

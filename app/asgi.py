"""Aplicación ASGI del admin, servida por uvicorn dentro del proceso del bot (app/web.py).

`ASGIStaticFilesHandler` sirve los estáticos del admin desde los finders sin `collectstatic`.
Suficiente para dos usuarios en la LAN; si algún día importa el caché, cambiar a whitenoise.
"""

from __future__ import annotations

from app.django_bootstrap import setup_django

setup_django()

from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler  # noqa: E402
from django.core.asgi import get_asgi_application  # noqa: E402

application = ASGIStaticFilesHandler(get_asgi_application())

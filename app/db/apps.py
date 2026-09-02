"""Configuración de la app Django que contiene los modelos de la agenda."""

from __future__ import annotations

from django.apps import AppConfig


class AgendaConfig(AppConfig):
    name = "app.db"
    label = "agenda"
    verbose_name = "Agenda escolar"
    default_auto_field = "django.db.models.BigAutoField"

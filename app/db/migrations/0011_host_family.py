"""Las familias que ya existían usan el LLM del anfitrión.

`uses_host_llm` nació en 0010 con `False`, que es lo correcto para una familia nueva: nadie
debe estrenar cuenta gastando la suscripción del operador. Pero la familia que creó el
backfill de 0009 es justo la excepción —es la del operador, la que ya venía funcionando con
el `.env`— y dejarla en `False` sin credenciales la deja sin ningún proveedor resoluble.

Por eso el backfill mira la fecha de creación: al aplicarse esta migración, las únicas
familias existentes son las que precedían al multi-inquilino.
"""

from __future__ import annotations

from django.db import migrations


def use_host_llm(apps, schema_editor) -> None:  # type: ignore[no-untyped-def]
    apps.get_model("agenda", "Family").objects.update(uses_host_llm=True)


def noop(apps, schema_editor) -> None:  # type: ignore[no-untyped-def]
    """No se revierte: quitarle el LLM al operador rompería su bot."""


class Migration(migrations.Migration):
    dependencies = [("agenda", "0010_credentials")]

    operations = [migrations.RunPython(use_host_llm, noop)]

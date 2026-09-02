"""Pie de foto en `sources` y coherencia de la vigencia de los horarios.

La comprobación `valid_to >= valid_from` llega con datos ya en producción: al guardar un
segundo horario el mismo día que el primero, el anterior se cerraba con `valid_to` = el día
antes de su propio `valid_from`. `_fix_invalid_ranges` arregla esas filas antes de crear la
comprobación, que si no fallaría al migrar.
"""

from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def _fix_invalid_ranges(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Sube `valid_to` hasta `valid_from` en los horarios cerrados antes de empezar."""
    ScheduleTemplate = apps.get_model("agenda", "ScheduleTemplate")
    broken = ScheduleTemplate.objects.filter(
        valid_to__isnull=False, valid_to__lt=models.F("valid_from")
    )
    broken.update(valid_to=models.F("valid_from"))


def _noop(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Nada que deshacer: la corrección de datos no es reversible ni hace falta que lo sea."""


class Migration(migrations.Migration):
    dependencies = [
        ("agenda", "0004_schedule"),
    ]

    operations = [
        migrations.AddField(
            model_name="source",
            name="caption",
            field=models.TextField(
                blank=True,
                help_text="Lo que escribió el usuario junto a la foto; contexto para releerla.",
                null=True,
                verbose_name="pie de foto",
            ),
        ),
        migrations.RunPython(_fix_invalid_ranges, _noop),
        migrations.AddConstraint(
            model_name="scheduletemplate",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("valid_to__isnull", True),
                    ("valid_to__gte", models.F("valid_from")),
                    _connector="OR",
                ),
                name="schedules_valid_range_check",
            ),
        ),
    ]

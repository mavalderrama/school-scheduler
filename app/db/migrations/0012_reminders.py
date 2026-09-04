"""Recordatorios que pide el usuario (Fase 10).

Además de la tabla, cambia la clave de idempotencia de `notifications_log`: el recordatorio
entra en ella porque dos recordatorios distintos del mismo día, chat y niño comparten todo
lo demás y el segundo no se podría ni registrar. Como el índice ya era `nulls_distinct=False`,
las filas de `daily`, `gap_check` y `nudge_empty` —que dejan el recordatorio en NULL— siguen
comportándose exactamente igual que antes, y por eso no hace falta ningún `RunPython`.
"""

# Generada con makemigrations el 2026-09-04 21:43

import django.db.models.deletion
import django.db.models.functions.datetime
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("agenda", "0011_host_family"),
    ]

    operations = [
        migrations.CreateModel(
            name="Reminder",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("chat_id", models.BigIntegerField(verbose_name="chat de destino")),
                ("text", models.TextField(verbose_name="texto")),
                ("time_of_day", models.TimeField(verbose_name="hora (local del colegio)")),
                (
                    "repeat",
                    models.TextField(
                        choices=[
                            ("once", "Una vez"),
                            ("daily", "Todos los días"),
                            ("weekly", "Días de la semana"),
                        ],
                        db_default="once",
                        default="once",
                        verbose_name="repetición",
                    ),
                ),
                (
                    "weekdays",
                    models.TextField(blank=True, db_default="", default="", verbose_name="días"),
                ),
                (
                    "on_date",
                    models.DateField(blank=True, null=True, verbose_name="fecha (si es una vez)"),
                ),
                (
                    "only_school_days",
                    models.BooleanField(
                        db_default=False,
                        default=False,
                        help_text="Respeta fines de semana, festivos y el calendario del colegio.",
                        verbose_name="solo días de colegio",
                    ),
                ),
                (
                    "next_fire_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="Vacío: ya no suena más.",
                        null=True,
                        verbose_name="próxima vez",
                    ),
                ),
                (
                    "last_fired_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="última vez"),
                ),
                (
                    "is_active",
                    models.BooleanField(db_default=True, default=True, verbose_name="vigente"),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        db_default=django.db.models.functions.datetime.Now(),
                        editable=False,
                        verbose_name="creado",
                    ),
                ),
            ],
            options={
                "verbose_name": "recordatorio",
                "verbose_name_plural": "recordatorios",
                "db_table": "reminders",
            },
        ),
        migrations.RemoveConstraint(
            model_name="notificationlog",
            name="notif_log_ok_unique",
        ),
        migrations.AlterField(
            model_name="notificationlog",
            name="kind",
            field=models.TextField(
                choices=[
                    ("daily", "Diaria"),
                    ("gap_check", "Chequeo de huecos"),
                    ("nudge_empty", "Aviso de agenda vacía"),
                    ("reminder", "Recordatorio"),
                ],
                verbose_name="tipo",
            ),
        ),
        migrations.AddField(
            model_name="reminder",
            name="child",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="reminders",
                to="agenda.child",
                verbose_name="niño",
            ),
        ),
        migrations.AddField(
            model_name="reminder",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                db_column="created_by",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="reminders",
                to="agenda.user",
                verbose_name="pedido por",
            ),
        ),
        migrations.AddField(
            model_name="notificationlog",
            name="reminder",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="notifications",
                to="agenda.reminder",
                verbose_name="recordatorio",
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationlog",
            constraint=models.UniqueConstraint(
                condition=models.Q(("ok", True)),
                fields=("kind", "target_date", "chat_id", "child", "reminder"),
                name="notif_log_ok_unique",
                nulls_distinct=False,
            ),
        ),
        migrations.AddIndex(
            model_name="reminder",
            index=models.Index(
                condition=models.Q(("is_active", True)),
                fields=["next_fire_at"],
                name="reminder_due_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="reminder",
            constraint=models.CheckConstraint(
                condition=models.Q(("repeat__in", ["once", "daily", "weekly"])),
                name="reminder_repeat_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="reminder",
            constraint=models.CheckConstraint(
                condition=models.Q(("weekdays__regex", "^[1-7]{0,7}$")),
                name="reminder_weekdays_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="reminder",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("repeat", "once"), _negated=True),
                    ("on_date__isnull", False),
                    _connector="OR",
                ),
                name="reminder_once_needs_date",
            ),
        ),
        migrations.AddConstraint(
            model_name="reminder",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("is_active", False), ("next_fire_at__isnull", False), _connector="OR"
                ),
                name="reminder_active_has_next",
            ),
        ),
    ]

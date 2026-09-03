"""Multi-familia: modelos de inquilino y columnas de ámbito (Fase 9.1).

Va en tres tiempos a propósito: crear las tablas nuevas, añadir las claves ajenas
**nullable**, rellenarlas con una familia por defecto para los datos que ya existen, y solo
entonces exigirlas. Con datos en producción no hay otra forma de añadir una FK obligatoria.

Las dos correcciones de integridad viajan aquí porque sin ellas el multi-inquilino es
destructivo: el día de `calendar_exceptions` dejaba de ser único globalmente (dos colegios
no podían tener excepción el mismo día) y la idempotencia de las notificaciones incorpora
el niño (dos hermanos en un chat compartían ranura).
"""

from typing import Any

import django.db.models.deletion
import django.db.models.functions.datetime
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

DEFAULT_FAMILY = "Familia"
DEFAULT_SCHOOL = "Colegio"
DEFAULT_CHILD = "Alejandro"


def _backfill(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Mete todo lo que ya existe en una familia por defecto.

    El `chat_id` del niño sale de la source más reciente que tenga uno: es el grupo desde
    el que se ha estado usando el bot, así que la vinculación queda hecha sola.
    """
    Family = apps.get_model("agenda", "Family")
    School = apps.get_model("agenda", "School")
    Child = apps.get_model("agenda", "Child")
    Membership = apps.get_model("agenda", "Membership")
    User = apps.get_model("agenda", "User")
    Source = apps.get_model("agenda", "Source")
    AgendaEntry = apps.get_model("agenda", "AgendaEntry")
    ScheduleTemplate = apps.get_model("agenda", "ScheduleTemplate")
    CalendarException = apps.get_model("agenda", "CalendarException")
    NotificationLog = apps.get_model("agenda", "NotificationLog")
    LLMCall = apps.get_model("agenda", "LLMCall")

    has_data = (
        Source.objects.exists()
        or ScheduleTemplate.objects.exists()
        or CalendarException.objects.exists()
        or User.objects.exists()
    )
    if not has_data:
        return

    family = Family.objects.create(name=DEFAULT_FAMILY)
    school = School.objects.create(family=family, name=DEFAULT_SCHOOL)
    chat_id = (
        Source.objects.filter(chat_id__isnull=False)
        .order_by("-id")
        .values_list("chat_id", flat=True)
        .first()
    )
    child = Child.objects.create(family=family, school=school, name=DEFAULT_CHILD, chat_id=chat_id)

    for user in User.objects.all():
        Membership.objects.get_or_create(family=family, user=user, defaults={"role": "owner"})

    Source.objects.filter(child__isnull=True).update(child=child)
    AgendaEntry.objects.filter(child__isnull=True).update(child=child)
    ScheduleTemplate.objects.filter(child__isnull=True).update(child=child)
    CalendarException.objects.filter(school__isnull=True).update(school=school)
    NotificationLog.objects.filter(child__isnull=True).update(child=child)
    LLMCall.objects.filter(family__isnull=True).update(family=family)


def _noop(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """No se deshace: revertir borraría la asignación de familia de todo."""


def _fk(
    target: str, related: str, *, null: bool = False, verbose: str = ""
) -> models.ForeignKey[Any, Any]:
    return models.ForeignKey(
        null=null,
        blank=null,
        on_delete=django.db.models.deletion.PROTECT,
        related_name=related,
        to=f"agenda.{target}",
        verbose_name=verbose,
    )


class Migration(migrations.Migration):
    dependencies = [("agenda", "0008_graph_thread_view")]

    operations = [
        # --- 1. Tablas de inquilino -------------------------------------------------------
        migrations.CreateModel(
            name="Family",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.TextField(verbose_name="nombre")),
                (
                    "is_active",
                    models.BooleanField(db_default=True, default=True, verbose_name="activa"),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        db_default=django.db.models.functions.datetime.Now(),
                        editable=False,
                        verbose_name="creada",
                    ),
                ),
            ],
            options={
                "verbose_name": "familia",
                "verbose_name_plural": "familias",
                "db_table": "families",
            },
        ),
        migrations.CreateModel(
            name="School",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.TextField(verbose_name="nombre")),
                ("city", models.TextField(blank=True, default="", verbose_name="ciudad")),
                ("country", models.TextField(db_default="CO", default="CO", verbose_name="país")),
                (
                    "timezone",
                    models.TextField(
                        db_default="America/Bogota",
                        default="America/Bogota",
                        verbose_name="zona horaria",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        db_default=django.db.models.functions.datetime.Now(),
                        editable=False,
                        verbose_name="creado",
                    ),
                ),
                ("family", _fk("Family", "schools", verbose="familia")),
            ],
            options={
                "verbose_name": "colegio",
                "verbose_name_plural": "colegios",
                "db_table": "schools",
            },
        ),
        migrations.CreateModel(
            name="Child",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.TextField(verbose_name="nombre")),
                (
                    "chat_id",
                    models.BigIntegerField(
                        blank=True,
                        help_text="El grupo de este niño. Un chat pertenece como mucho a un niño.",
                        null=True,
                        unique=True,
                        verbose_name="chat de Telegram",
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(db_default=True, default=True, verbose_name="activo"),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        db_default=django.db.models.functions.datetime.Now(),
                        editable=False,
                        verbose_name="creado",
                    ),
                ),
                ("family", _fk("Family", "children", verbose="familia")),
                ("school", _fk("School", "children", verbose="colegio")),
            ],
            options={
                "verbose_name": "niño",
                "verbose_name_plural": "niños",
                "db_table": "children",
            },
        ),
        migrations.CreateModel(
            name="Membership",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "role",
                    models.TextField(
                        choices=[("owner", "Responsable"), ("parent", "Padre/madre")],
                        db_default="parent",
                        default="parent",
                        verbose_name="rol",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        db_default=django.db.models.functions.datetime.Now(),
                        editable=False,
                        verbose_name="creada",
                    ),
                ),
                ("family", _fk("Family", "memberships", verbose="familia")),
                ("user", _fk("User", "memberships", verbose="usuario")),
            ],
            options={
                "verbose_name": "membresía",
                "verbose_name_plural": "membresías",
                "db_table": "memberships",
            },
        ),
        # --- 2. Columnas de ámbito, nullable para poder rellenarlas ------------------------
        migrations.AddField(
            model_name="source",
            name="child",
            field=_fk("Child", "sources", null=True, verbose="niño"),
        ),
        migrations.AddField(
            model_name="agendaentry",
            name="child",
            field=_fk("Child", "entries", null=True, verbose="niño"),
        ),
        migrations.AddField(
            model_name="scheduletemplate",
            name="child",
            field=_fk("Child", "schedules", null=True, verbose="niño"),
        ),
        migrations.AddField(
            model_name="calendarexception",
            name="school",
            field=_fk("School", "calendar", null=True, verbose="colegio"),
        ),
        migrations.AddField(
            model_name="notificationlog",
            name="child",
            field=_fk("Child", "notifications", null=True, verbose="niño"),
        ),
        migrations.AddField(
            model_name="llmcall",
            name="family",
            field=_fk("Family", "llm_calls", null=True, verbose="familia"),
        ),
        # --- 3. Rellenar ------------------------------------------------------------------
        migrations.RunPython(_backfill, _noop),
        # --- 4. Exigirlas donde no puede faltar --------------------------------------------
        migrations.AlterField(
            model_name="source", name="child", field=_fk("Child", "sources", verbose="niño")
        ),
        migrations.AlterField(
            model_name="agendaentry", name="child", field=_fk("Child", "entries", verbose="niño")
        ),
        migrations.AlterField(
            model_name="scheduletemplate",
            name="child",
            field=_fk("Child", "schedules", verbose="niño"),
        ),
        migrations.AlterField(
            model_name="calendarexception",
            name="school",
            field=_fk("School", "calendar", verbose="colegio"),
        ),
        # --- 5. Índices y restricciones que ahora llevan el ámbito -------------------------
        migrations.RemoveIndex(model_name="agendaentry", name="agenda_entry_date_active_idx"),
        migrations.AddIndex(
            model_name="agendaentry",
            index=models.Index(
                condition=models.Q(("is_active", True)),
                fields=["child", "entry_date"],
                name="agenda_entry_date_active_idx",
            ),
        ),
        migrations.RemoveIndex(model_name="scheduletemplate", name="schedule_active_idx"),
        migrations.AddIndex(
            model_name="scheduletemplate",
            index=models.Index(
                condition=models.Q(("is_active", True)),
                fields=["child", "valid_from"],
                name="schedule_active_idx",
            ),
        ),
        migrations.AlterField(
            model_name="calendarexception", name="day", field=models.DateField(verbose_name="día")
        ),
        migrations.AddConstraint(
            model_name="calendarexception",
            constraint=models.UniqueConstraint(
                fields=("school", "day"), name="calendar_school_day_unique"
            ),
        ),
        migrations.RemoveConstraint(model_name="notificationlog", name="notif_log_ok_unique"),
        migrations.AddConstraint(
            model_name="notificationlog",
            constraint=models.UniqueConstraint(
                condition=models.Q(("ok", True)),
                fields=("kind", "target_date", "chat_id", "child"),
                name="notif_log_ok_unique",
                nulls_distinct=False,
            ),
        ),
        migrations.AddConstraint(
            model_name="membership",
            constraint=models.UniqueConstraint(fields=("family", "user"), name="membership_unique"),
        ),
        migrations.AddConstraint(
            model_name="membership",
            constraint=models.CheckConstraint(
                condition=models.Q(("role__in", ["owner", "parent"])), name="membership_role_check"
            ),
        ),
    ]

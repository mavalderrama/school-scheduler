"""Modelo de datos (sección 5 del plan). Principio: nunca borrar, siempre versionar.

Tablas y columnas coinciden con el SQL del plan (`db_table`, `db_column`). Todas las FK son
PROTECT porque nada se borra; los enums son TEXT + CHECK, no tipos ENUM de Postgres.
"""

from __future__ import annotations

from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Now


class UserRole(models.TextChoices):
    PARENT = "parent", "Padre/madre"
    ADMIN = "admin", "Administrador"


class SourceKind(models.TextChoices):
    PHOTO = "photo", "Foto"
    TEXT_CORRECTION = "text_correction", "Corrección por texto"
    MANUAL = "manual", "Manual"


class SourceStatus(models.TextChoices):
    PENDING = "pending", "Pendiente"
    CONFIRMED = "confirmed", "Confirmada"
    REJECTED = "rejected", "Rechazada"
    FAILED = "failed", "Fallida"


class EntryKind(models.TextChoices):
    BRING = "bring", "Llevar"
    HOMEWORK = "homework", "Tarea"
    EVENT = "event", "Evento"
    NOTE = "note", "Nota"


class MessageRole(models.TextChoices):
    USER = "user", "Usuario"
    ASSISTANT = "assistant", "Asistente"


class NotificationKind(models.TextChoices):
    DAILY = "daily", "Diaria"
    GAP_CHECK = "gap_check", "Chequeo de huecos"
    NUDGE_EMPTY = "nudge_empty", "Aviso de agenda vacía"


class LLMTask(models.TextChoices):
    VISION = "vision", "Visión"
    INTENT = "intent", "Intención"
    CORRECTION = "correction", "Corrección"
    REFINE = "refine", "Refinado con respuestas"


class HolidayPolicy(models.TextChoices):
    """Qué le hace un día no lectivo a la rotación."""

    SKIP_DAY = "skip_day", "Solo se cancela ese día"
    SHIFT = "shift", "La rotación se corre"


class CalendarKind(models.TextChoices):
    """Excepciones del calendario que la librería de festivos no puede saber."""

    HOLIDAY = "holiday", "Festivo"
    SCHOOL_CLOSED = "school_closed", "Sin clase"
    CLASS_DAY = "class_day", "Sí hay clase"


class User(models.Model):
    """Padre o madre en la whitelist. La PK es el id de Telegram."""

    telegram_user_id = models.BigIntegerField(primary_key=True, verbose_name="id de Telegram")
    display_name = models.TextField(verbose_name="nombre")
    role = models.TextField(choices=UserRole, verbose_name="rol")
    created_at = models.DateTimeField(db_default=Now(), editable=False, verbose_name="creado")

    class Meta:
        db_table = "users"
        verbose_name = "usuario"
        verbose_name_plural = "usuarios"
        constraints = [
            models.CheckConstraint(condition=Q(role__in=UserRole.values), name="users_role_check"),
        ]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.telegram_user_id})"


class Source(models.Model):
    """Cada foto, corrección por texto o alta manual."""

    kind = models.TextField(choices=SourceKind, verbose_name="tipo")
    telegram_file_id = models.TextField(null=True, blank=True)
    local_path = models.TextField(null=True, blank=True, verbose_name="ruta local")
    raw_llm_output = models.JSONField(null=True, blank=True, verbose_name="salida cruda del LLM")
    llm_provider = models.TextField(null=True, blank=True, verbose_name="proveedor de LLM")
    submitted_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        db_column="submitted_by",
        related_name="sources",
        verbose_name="enviada por",
    )
    status = models.TextField(
        choices=SourceStatus,
        default=SourceStatus.PENDING,
        db_default=SourceStatus.PENDING,
        verbose_name="estado",
    )
    chat_id = models.BigIntegerField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="chat de origen",
        help_text="Chat de Telegram donde llegó, para reintentar y responder tras un reinicio.",
    )
    caption = models.TextField(
        null=True,
        blank=True,
        verbose_name="pie de foto",
        help_text="Lo que escribió el usuario junto a la foto; contexto para releerla.",
    )
    llm_cache_key = models.TextField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="clave de caché",
        help_text="Entrada de llm_cache que produjo la extracción; se borra al descartar.",
    )
    created_at = models.DateTimeField(db_default=Now(), editable=False, verbose_name="creada")

    class Meta:
        db_table = "sources"
        verbose_name = "fuente"
        verbose_name_plural = "fuentes"
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=SourceKind.values), name="sources_kind_check"
            ),
            models.CheckConstraint(
                condition=Q(status__in=SourceStatus.values), name="sources_status_check"
            ),
        ]

    def __str__(self) -> str:
        return f"#{self.pk} {self.kind} ({self.status})"


class AgendaEntry(models.Model):
    entry_date = models.DateField(verbose_name="fecha")
    kind = models.TextField(choices=EntryKind, verbose_name="tipo")
    text = models.TextField(verbose_name="texto")
    source = models.ForeignKey(
        Source,
        on_delete=models.PROTECT,
        db_column="source_id",
        related_name="entries",
        verbose_name="fuente",
    )
    is_active = models.BooleanField(default=True, db_default=True, verbose_name="vigente")
    superseded_by = models.ForeignKey(
        Source,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        db_column="superseded_by",
        related_name="superseded_entries",
        verbose_name="reemplazada por",
    )
    created_at = models.DateTimeField(db_default=Now(), editable=False, verbose_name="creada")

    class Meta:
        db_table = "agenda_entries"
        verbose_name = "entrada de agenda"
        verbose_name_plural = "entradas de agenda"
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=EntryKind.values), name="agenda_entries_kind_check"
            ),
        ]
        indexes = [
            models.Index(
                fields=["entry_date"],
                condition=Q(is_active=True),
                name="agenda_entry_date_active_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.entry_date} {self.kind}: {self.text[:40]}"


class ConversationMessage(models.Model):
    """Historial corto por chat para el prompt de intención."""

    chat_id = models.BigIntegerField()
    telegram_user_id = models.BigIntegerField(null=True, blank=True)
    role = models.TextField(choices=MessageRole, verbose_name="rol")
    content = models.TextField(verbose_name="contenido")
    created_at = models.DateTimeField(db_default=Now(), editable=False, verbose_name="creado")

    class Meta:
        db_table = "conversation_messages"
        verbose_name = "mensaje de conversación"
        verbose_name_plural = "mensajes de conversación"
        constraints = [
            models.CheckConstraint(
                condition=Q(role__in=MessageRole.values),
                name="conversation_messages_role_check",
            ),
        ]
        indexes = [
            models.Index(fields=["chat_id", "-created_at"], name="conv_msg_chat_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.chat_id} {self.role}: {self.content[:40]}"


class NotificationLog(models.Model):
    """Registro de envíos. Idempotencia: un solo envío ok por (kind, target_date, chat_id)."""

    kind = models.TextField(choices=NotificationKind, verbose_name="tipo")
    target_date = models.DateField(null=True, blank=True, verbose_name="fecha objetivo")
    chat_id = models.BigIntegerField()
    sent_at = models.DateTimeField(db_default=Now(), editable=False, verbose_name="enviada")
    ok = models.BooleanField()
    error = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "notifications_log"
        verbose_name = "notificación"
        verbose_name_plural = "notificaciones"
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "target_date", "chat_id"],
                condition=Q(ok=True),
                nulls_distinct=False,
                name="notif_log_ok_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.target_date} -> {self.chat_id} ({'ok' if self.ok else 'error'})"


class LLMCall(models.Model):
    """Consumo por proveedor, para vigilar cuota y costo."""

    provider = models.TextField(verbose_name="proveedor")
    task = models.TextField(choices=LLMTask, verbose_name="tarea")
    model = models.TextField(null=True, blank=True, verbose_name="modelo")
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)
    # Solo los reportan claude_sdk y anthropic_api; Ollama no expone info de caché.
    cache_read_tokens = models.IntegerField(null=True, blank=True)
    cache_write_tokens = models.IntegerField(null=True, blank=True)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    ok = models.BooleanField()
    error = models.TextField(null=True, blank=True)
    # Traza: qué se le mandó y qué contestó. Es lo único que no se podía ver cuando algo
    # salía raro; las métricas de arriba solo dicen que falló, no por qué.
    prompt = models.TextField(null=True, blank=True, verbose_name="prompt enviado")
    response = models.JSONField(null=True, blank=True, verbose_name="respuesta cruda")
    created_at = models.DateTimeField(db_default=Now(), editable=False, verbose_name="creada")

    class Meta:
        db_table = "llm_calls"
        verbose_name = "llamada al LLM"
        verbose_name_plural = "llamadas al LLM"

    def __str__(self) -> str:
        return f"{self.provider}/{self.task} ({'ok' if self.ok else 'error'})"


class LLMCacheEntry(models.Model):
    """Caché de respuestas por coincidencia exacta: un repetido no gasta tokens.

    La clave incluye la fecha de hoy y la versión de los prompts, así que una consulta
    con fechas relativas falla correctamente al día siguiente y editar un prompt
    invalida la caché sola (ver `app/llm/cache.py`).
    """

    key = models.CharField(max_length=64, unique=True, verbose_name="clave")
    task = models.TextField(choices=LLMTask, verbose_name="tarea")
    prompt_version = models.CharField(max_length=64, verbose_name="versión de prompts")
    provider = models.TextField(verbose_name="proveedor original")
    model = models.TextField(null=True, blank=True, verbose_name="modelo")
    response = models.JSONField(verbose_name="respuesta")
    hits = models.IntegerField(default=0, db_default=0, verbose_name="aciertos")
    created_at = models.DateTimeField(db_default=Now(), editable=False, verbose_name="creada")
    last_hit_at = models.DateTimeField(null=True, blank=True, verbose_name="último acierto")
    expires_at = models.DateTimeField(db_index=True, verbose_name="expira")

    class Meta:
        db_table = "llm_cache"
        verbose_name = "entrada de caché"
        verbose_name_plural = "entradas de caché"

    def __str__(self) -> str:
        return f"{self.task}/{self.provider} ({self.hits} aciertos)"


class ScheduleTemplate(models.Model):
    """Horario rotativo: un ciclo de N semanas etiquetadas (A, B, ...) desde un lunes ancla.

    Se versiona como todo lo demás: una foto nueva del horario desactiva la plantilla
    anterior con `superseded_by` en vez de editarla.
    """

    name = models.TextField(verbose_name="nombre")
    anchor_monday = models.DateField(
        verbose_name="lunes ancla",
        help_text="Lunes de la primera semana del ciclo (la etiquetada con week_index 0).",
    )
    cycle_weeks = models.SmallIntegerField(
        default=2, db_default=2, verbose_name="semanas del ciclo"
    )
    valid_from = models.DateField(verbose_name="vigente desde")
    valid_to = models.DateField(null=True, blank=True, verbose_name="vigente hasta")
    holiday_policy = models.TextField(
        choices=HolidayPolicy,
        default=HolidayPolicy.SKIP_DAY,
        db_default=HolidayPolicy.SKIP_DAY,
        verbose_name="política de festivos",
    )
    source = models.ForeignKey(
        Source,
        on_delete=models.PROTECT,
        db_column="source_id",
        related_name="schedules",
        verbose_name="fuente",
    )
    is_active = models.BooleanField(default=True, db_default=True, verbose_name="vigente")
    superseded_by = models.ForeignKey(
        Source,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        db_column="superseded_by",
        related_name="superseded_schedules",
        verbose_name="reemplazada por",
    )
    created_at = models.DateTimeField(db_default=Now(), editable=False, verbose_name="creada")

    class Meta:
        db_table = "schedules"
        verbose_name = "horario"
        verbose_name_plural = "horarios"
        constraints = [
            models.CheckConstraint(
                condition=Q(holiday_policy__in=HolidayPolicy.values),
                name="schedules_holiday_policy_check",
            ),
            models.CheckConstraint(condition=Q(cycle_weeks__gte=1), name="schedules_cycle_check"),
            # Un horario cerrado antes de empezar es una fila incoherente: pasó de verdad
            # al reemplazar el mismo día en que se había creado el anterior.
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gte=F("valid_from")),
                name="schedules_valid_range_check",
            ),
        ]
        indexes = [
            models.Index(
                fields=["valid_from"], condition=Q(is_active=True), name="schedule_active_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} (desde {self.valid_from})"


class ScheduleSlot(models.Model):
    """Una franja del ciclo: semana + día de la semana -> materia."""

    schedule = models.ForeignKey(
        ScheduleTemplate,
        on_delete=models.PROTECT,
        db_column="schedule_id",
        related_name="slots",
        verbose_name="horario",
    )
    week_index = models.SmallIntegerField(verbose_name="semana del ciclo (0 = la primera)")
    week_label = models.TextField(verbose_name="etiqueta de la semana")
    weekday = models.SmallIntegerField(verbose_name="día (ISO: 1 lunes ... 7 domingo)")
    # TEXT y no entero: en este colegio la última franja se llama «Cultural».
    rotation = models.TextField(null=True, blank=True, verbose_name="rotación")
    subject = models.TextField(verbose_name="materia")
    note = models.TextField(null=True, blank=True, verbose_name="nota")
    created_at = models.DateTimeField(db_default=Now(), editable=False, verbose_name="creada")

    class Meta:
        db_table = "schedule_slots"
        verbose_name = "franja del horario"
        verbose_name_plural = "franjas del horario"
        constraints = [
            models.UniqueConstraint(
                fields=["schedule", "week_index", "weekday"], name="schedule_slot_unique"
            ),
            models.CheckConstraint(
                condition=Q(weekday__gte=1) & Q(weekday__lte=7), name="schedule_slot_weekday_check"
            ),
            models.CheckConstraint(
                condition=Q(week_index__gte=0), name="schedule_slot_week_index_check"
            ),
        ]

    def __str__(self) -> str:
        return f"Semana {self.week_label} día {self.weekday}: {self.subject}"


class CalendarException(models.Model):
    """Días que la librería de festivos no puede saber: receso, jornadas pedagógicas.

    `class_day` es la excepción a la excepción: un festivo nacional en el que el colegio
    sí tiene clase.
    """

    day = models.DateField(unique=True, verbose_name="día")
    kind = models.TextField(choices=CalendarKind, verbose_name="tipo")
    label = models.TextField(verbose_name="motivo")
    created_at = models.DateTimeField(db_default=Now(), editable=False, verbose_name="creado")

    class Meta:
        db_table = "calendar_exceptions"
        verbose_name = "excepción del calendario"
        verbose_name_plural = "excepciones del calendario"
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=CalendarKind.values), name="calendar_exceptions_kind_check"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.day} {self.kind}: {self.label}"

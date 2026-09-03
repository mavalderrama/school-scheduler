"""Modelo de datos (sección 5 del plan). Principio: nunca borrar, siempre versionar.

Tablas y columnas coinciden con el SQL del plan (`db_table`, `db_column`). Todas las FK son
PROTECT porque nada se borra; los enums son TEXT + CHECK, no tipos ENUM de Postgres.
"""

from __future__ import annotations

from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Now


class MembershipRole(models.TextChoices):
    OWNER = "owner", "Responsable"
    PARENT = "parent", "Padre/madre"


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


class CalendarKind(models.TextChoices):
    """Excepciones del calendario que la librería de festivos no puede saber."""

    HOLIDAY = "holiday", "Festivo"
    SCHOOL_CLOSED = "school_closed", "Sin clase"
    CLASS_DAY = "class_day", "Sí hay clase"


class Family(models.Model):
    """Una familia. Es la unidad de aislamiento: todo cuelga de aquí, directa o
    indirectamente, y ninguna consulta debe cruzar esta frontera."""

    name = models.TextField(verbose_name="nombre")
    is_active = models.BooleanField(default=True, db_default=True, verbose_name="activa")
    created_at = models.DateTimeField(db_default=Now(), editable=False, verbose_name="creada")

    class Meta:
        db_table = "families"
        verbose_name = "familia"
        verbose_name_plural = "familias"

    def __str__(self) -> str:
        return self.name


class School(models.Model):
    """El colegio de un niño. Los hermanos del mismo colegio comparten calendario.

    `country` y `timezone` viven aquí y no en la configuración global porque dos familias
    pueden estar en ciudades —o países— distintos.
    """

    family = models.ForeignKey(
        "Family", on_delete=models.PROTECT, related_name="schools", verbose_name="familia"
    )
    name = models.TextField(verbose_name="nombre")
    city = models.TextField(blank=True, default="", verbose_name="ciudad")
    country = models.TextField(default="CO", db_default="CO", verbose_name="país")
    timezone = models.TextField(
        default="America/Bogota", db_default="America/Bogota", verbose_name="zona horaria"
    )
    created_at = models.DateTimeField(db_default=Now(), editable=False, verbose_name="creado")

    class Meta:
        db_table = "schools"
        verbose_name = "colegio"
        verbose_name_plural = "colegios"

    def __str__(self) -> str:
        return self.name


class Child(models.Model):
    """Un niño. **El chat de Telegram determina el niño**, así que no hay que preguntar
    de quién es cada foto: un grupo por niño."""

    family = models.ForeignKey(
        "Family", on_delete=models.PROTECT, related_name="children", verbose_name="familia"
    )
    school = models.ForeignKey(
        "School", on_delete=models.PROTECT, related_name="children", verbose_name="colegio"
    )
    name = models.TextField(verbose_name="nombre")
    chat_id = models.BigIntegerField(
        null=True,
        blank=True,
        unique=True,
        verbose_name="chat de Telegram",
        help_text="El grupo de este niño. Un chat pertenece como mucho a un niño.",
    )
    is_active = models.BooleanField(default=True, db_default=True, verbose_name="activo")
    created_at = models.DateTimeField(db_default=Now(), editable=False, verbose_name="creado")

    class Meta:
        db_table = "children"
        verbose_name = "niño"
        verbose_name_plural = "niños"

    def __str__(self) -> str:
        return self.name


class Membership(models.Model):
    """Quién puede ver qué familia. Sustituye a la whitelist de variables de entorno."""

    family = models.ForeignKey(
        "Family", on_delete=models.PROTECT, related_name="memberships", verbose_name="familia"
    )
    user = models.ForeignKey(
        "User", on_delete=models.PROTECT, related_name="memberships", verbose_name="usuario"
    )
    role = models.TextField(
        choices=MembershipRole,
        default=MembershipRole.PARENT,
        db_default=MembershipRole.PARENT,
        verbose_name="rol",
    )
    created_at = models.DateTimeField(db_default=Now(), editable=False, verbose_name="creada")

    class Meta:
        db_table = "memberships"
        verbose_name = "membresía"
        verbose_name_plural = "membresías"
        constraints = [
            models.UniqueConstraint(fields=["family", "user"], name="membership_unique"),
            models.CheckConstraint(
                condition=Q(role__in=MembershipRole.values), name="membership_role_check"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} en {self.family_id}"


class User(models.Model):
    """Padre o madre. La PK es el id de Telegram.

    A qué familias pertenece lo dice `Membership`: un mismo adulto puede estar en más de
    una (padres separados, abuelos que ayudan).
    """

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

    child = models.ForeignKey(
        "Child", on_delete=models.PROTECT, related_name="sources", verbose_name="niño"
    )
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
    # Desnormalizado a propósito aunque se deduzca por `source`: el merge por fecha y el
    # índice parcial necesitan el niño como primera columna, y una entrada sin dueño
    # explícito es exactamente el fallo que borraría la agenda de otra familia.
    child = models.ForeignKey(
        "Child", on_delete=models.PROTECT, related_name="entries", verbose_name="niño"
    )
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
                fields=["child", "entry_date"],
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

    child = models.ForeignKey(
        "Child",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="notifications",
        verbose_name="niño",
    )
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
            # El niño entra en la clave: dos hermanos en el mismo chat compartirían ranura
            # y el segundo aviso se descartaría por idempotencia.
            models.UniqueConstraint(
                fields=["kind", "target_date", "chat_id", "child"],
                condition=Q(ok=True),
                nulls_distinct=False,
                name="notif_log_ok_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.target_date} -> {self.chat_id} ({'ok' if self.ok else 'error'})"


class LLMCall(models.Model):
    """Consumo por proveedor, para vigilar cuota y costo."""

    family = models.ForeignKey(
        "Family",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="llm_calls",
        verbose_name="familia",
    )
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

    Un día no lectivo **nunca** mueve la rotación: solo se pierde la franja de ese día y la
    semana sigue siendo la que le toca por calendario. Hubo un campo `holiday_policy` para
    configurarlo; se quitó porque la otra opción no existía y, al ser elegible desde el
    admin, tumbaba la notificación diaria.
    """

    child = models.ForeignKey(
        "Child", on_delete=models.PROTECT, related_name="schedules", verbose_name="niño"
    )
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
                fields=["child", "valid_from"],
                condition=Q(is_active=True),
                name="schedule_active_idx",
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

    school = models.ForeignKey(
        "School", on_delete=models.PROTECT, related_name="calendar", verbose_name="colegio"
    )
    day = models.DateField(verbose_name="día")
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
            # Antes el día era único globalmente: dos colegios no podían tener excepción el
            # mismo día y el segundo sobrescribía al primero en silencio.
            models.UniqueConstraint(fields=["school", "day"], name="calendar_school_day_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.day} {self.kind}: {self.label}"


class GraphThread(models.Model):
    """Vista de solo lectura sobre los checkpoints de LangGraph, para el admin.

    `managed = False`: la tabla la crea `AsyncPostgresSaver.setup()` en cada arranque, no
    las migraciones de Django. No se exponen los valores del estado porque son msgpack
    binario en `checkpoint_blobs`; aquí solo interesa qué chat tiene una conversación viva
    y desde cuándo, que es lo que responde `/pendiente` pero de todos los chats a la vez.
    """

    thread_id = models.TextField(primary_key=True, verbose_name="chat")
    checkpoint_id = models.TextField(verbose_name="checkpoint")
    checkpoint = models.JSONField(verbose_name="estado interno")
    metadata = models.JSONField(verbose_name="metadatos")

    class Meta:
        managed = False
        db_table = "checkpoints"
        verbose_name = "conversación en curso"
        verbose_name_plural = "conversaciones en curso"

    def __str__(self) -> str:
        return self.thread_id

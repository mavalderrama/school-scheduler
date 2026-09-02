"""Modelo de datos (sección 5 del plan). Principio: nunca borrar, siempre versionar.

Tablas y columnas coinciden con el SQL del plan (`db_table`, `db_column`). Todas las FK son
PROTECT porque nada se borra; los enums son TEXT + CHECK, no tipos ENUM de Postgres.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q
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
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    ok = models.BooleanField()
    error = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now(), editable=False, verbose_name="creada")

    class Meta:
        db_table = "llm_calls"
        verbose_name = "llamada al LLM"
        verbose_name_plural = "llamadas al LLM"

    def __str__(self) -> str:
        return f"{self.provider}/{self.task} ({'ok' if self.ok else 'error'})"

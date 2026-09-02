"""Admin de Django: panel de operación de solo lectura salvo `users` y `agenda_entries`.

Regla "nunca borrar": ningún modelo permite eliminar desde el admin.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from app.db.models import (
    AgendaEntry,
    CalendarException,
    ConversationMessage,
    LLMCacheEntry,
    LLMCall,
    NotificationLog,
    ScheduleSlot,
    ScheduleTemplate,
    Source,
    User,
)

admin.site.site_header = "Agenda escolar"
admin.site.site_title = "Agenda escolar"
admin.site.index_title = "Operación del bot"
admin.site.disable_action("delete_selected")


class NoDeleteMixin:
    """Nada se borra: las entradas se versionan con `is_active` y `superseded_by`."""

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class ReadOnlyMixin(NoDeleteMixin):
    """Tablas de auditoría: solo lectura."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(User)
class UserAdmin(NoDeleteMixin, admin.ModelAdmin[User]):
    list_display = ["telegram_user_id", "display_name", "role", "created_at"]
    list_filter = ["role"]
    search_fields = ["display_name"]
    readonly_fields = ["created_at"]


@admin.register(Source)
class SourceAdmin(NoDeleteMixin, admin.ModelAdmin[Source]):
    list_display = ["id", "kind", "status", "llm_provider", "submitted_by", "created_at"]
    list_filter = ["kind", "status", "llm_provider"]
    search_fields = ["id", "telegram_file_id", "local_path"]
    readonly_fields = ["raw_llm_output", "created_at"]
    list_select_related = ["submitted_by"]
    date_hierarchy = "created_at"
    ordering = ["-id"]


@admin.register(AgendaEntry)
class AgendaEntryAdmin(NoDeleteMixin, admin.ModelAdmin[AgendaEntry]):
    list_display = [
        "id",
        "entry_date",
        "kind",
        "text",
        "is_active",
        "source",
        "superseded_by",
        "created_at",
    ]
    list_filter = ["is_active", "kind"]
    search_fields = ["text"]
    readonly_fields = ["created_at"]
    list_select_related = ["source", "superseded_by"]
    autocomplete_fields = ["source", "superseded_by"]
    date_hierarchy = "entry_date"
    ordering = ["-entry_date", "-id"]


@admin.register(ConversationMessage)
class ConversationMessageAdmin(ReadOnlyMixin, admin.ModelAdmin[ConversationMessage]):
    list_display = ["id", "chat_id", "role", "short_content", "created_at"]
    list_filter = ["role"]
    search_fields = ["content"]
    ordering = ["-id"]

    @admin.display(description="contenido")
    def short_content(self, obj: ConversationMessage) -> str:
        return obj.content[:80]


@admin.register(NotificationLog)
class NotificationLogAdmin(ReadOnlyMixin, admin.ModelAdmin[NotificationLog]):
    list_display = ["id", "kind", "target_date", "chat_id", "ok", "sent_at"]
    list_filter = ["kind", "ok"]
    date_hierarchy = "target_date"
    ordering = ["-id"]


@admin.register(LLMCall)
class LLMCallAdmin(ReadOnlyMixin, admin.ModelAdmin[LLMCall]):
    list_display = [
        "id",
        "provider",
        "task",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_usd",
        "duration_ms",
        "ok",
        "created_at",
    ]
    list_filter = ["provider", "task", "ok"]
    search_fields = ["model", "error"]
    ordering = ["-id"]


@admin.register(LLMCacheEntry)
class LLMCacheEntryAdmin(ReadOnlyMixin, admin.ModelAdmin[LLMCacheEntry]):
    """Caché de respuestas: solo lectura, pero se puede vaciar desde aquí si hace falta."""

    list_display = [
        "id",
        "task",
        "provider",
        "model",
        "hits",
        "created_at",
        "last_hit_at",
        "expires_at",
    ]
    list_filter = ["task", "provider"]
    search_fields = ["key"]
    date_hierarchy = "created_at"
    ordering = ["-id"]

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return True  # la caché es material desechable, no un dato versionado


class ScheduleSlotInline(admin.TabularInline[ScheduleSlot, ScheduleTemplate]):
    """Las franjas se editan dentro del horario: corregir una materia mal leída es común."""

    model = ScheduleSlot
    extra = 0
    fields = ["week_index", "week_label", "weekday", "rotation", "subject", "note"]
    ordering = ["week_index", "weekday"]

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(ScheduleTemplate)
class ScheduleTemplateAdmin(NoDeleteMixin, admin.ModelAdmin[ScheduleTemplate]):
    list_display = [
        "name",
        "anchor_monday",
        "cycle_weeks",
        "valid_from",
        "valid_to",
        "is_active",
        "created_at",
    ]
    list_filter = ["is_active"]
    readonly_fields = ["source", "superseded_by", "created_at"]
    inlines = [ScheduleSlotInline]


@admin.register(CalendarException)
class CalendarExceptionAdmin(NoDeleteMixin, admin.ModelAdmin[CalendarException]):
    """Lo que la librería de festivos no puede saber: receso, jornadas pedagógicas.

    Se edita a mano una vez al año. `class_day` sirve para decir que un festivo nacional
    sí es día de clase en este colegio.
    """

    list_display = ["day", "kind", "label", "created_at"]
    list_filter = ["kind"]
    ordering = ["-day"]
    search_fields = ["label"]

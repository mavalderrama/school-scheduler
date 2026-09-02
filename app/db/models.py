"""Modelo de datos (sección 5 del plan). Principio: nunca borrar, siempre versionar."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('parent','admin')", name="users_role_check"),)

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Source(Base):
    """Cada foto, corrección por texto o alta manual."""

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint("kind IN ('photo','text_correction','manual')", name="sources_kind_check"),
        CheckConstraint(
            "status IN ('pending','confirmed','rejected','failed')", name="sources_status_check"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    telegram_file_id: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    raw_llm_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    llm_provider: Mapped[str | None] = mapped_column(Text)
    submitted_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'pending'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgendaEntry(Base):
    __tablename__ = "agenda_entries"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('bring','homework','event','note')", name="agenda_entries_kind_check"
        ),
        Index(
            "ix_agenda_entries_entry_date_active",
            "entry_date",
            postgresql_where=sa_text("is_active"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sources.id"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa_text("true"))
    superseded_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("sources.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConversationMessage(Base):
    """Historial corto por chat para el prompt de intención."""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user','assistant')", name="conversation_messages_role_check"),
        Index(
            "ix_conversation_messages_chat_created",
            "chat_id",
            sa_text("created_at DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NotificationLog(Base):
    __tablename__ = "notifications_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # 'daily' | 'gap_check' | 'nudge_empty'
    target_date: Mapped[date | None] = mapped_column(Date)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class LLMCall(Base):
    """Consumo por proveedor, para vigilar cuota y costo."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    task: Mapped[str] = mapped_column(Text, nullable=False)  # 'vision' | 'intent'
    model: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

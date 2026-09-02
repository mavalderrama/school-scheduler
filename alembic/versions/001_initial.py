"""Tablas iniciales (sección 5 del plan).

Revision ID: 001
Revises:
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("telegram_user_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("role IN ('parent','admin')", name="users_role_check"),
    )

    op.create_table(
        "sources",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("telegram_file_id", sa.Text()),
        sa.Column("local_path", sa.Text()),
        sa.Column("raw_llm_output", postgresql.JSONB()),
        sa.Column("llm_provider", sa.Text()),
        sa.Column("submitted_by", sa.BigInteger(), sa.ForeignKey("users.telegram_user_id")),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "kind IN ('photo','text_correction','manual')", name="sources_kind_check"
        ),
        sa.CheckConstraint(
            "status IN ('pending','confirmed','rejected','failed')", name="sources_status_check"
        ),
    )

    op.create_table(
        "agenda_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("superseded_by", sa.BigInteger(), sa.ForeignKey("sources.id")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "kind IN ('bring','homework','event','note')", name="agenda_entries_kind_check"
        ),
    )
    op.create_index(
        "ix_agenda_entries_entry_date_active",
        "agenda_entries",
        ["entry_date"],
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger()),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("role IN ('user','assistant')", name="conversation_messages_role_check"),
    )
    op.create_index(
        "ix_conversation_messages_chat_created",
        "conversation_messages",
        ["chat_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "notifications_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target_date", sa.Date()),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text()),
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("model", sa.Text()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("cost_usd", sa.Numeric(10, 6)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("llm_calls")
    op.drop_table("notifications_log")
    op.drop_index("ix_conversation_messages_chat_created", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_index("ix_agenda_entries_entry_date_active", table_name="agenda_entries")
    op.drop_table("agenda_entries")
    op.drop_table("sources")
    op.drop_table("users")

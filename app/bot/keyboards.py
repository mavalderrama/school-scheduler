"""Teclados inline del ciclo de confirmación."""

from __future__ import annotations

from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

ConfirmAction = Literal["confirm", "correct", "reject"]


class SourceCallback(CallbackData, prefix="src"):
    action: ConfirmAction
    source_id: int


def confirmation_keyboard(source_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Confirmar",
                    callback_data=SourceCallback(action="confirm", source_id=source_id).pack(),
                ),
                InlineKeyboardButton(
                    text="✏️ Corregir",
                    callback_data=SourceCallback(action="correct", source_id=source_id).pack(),
                ),
                InlineKeyboardButton(
                    text="❌ Descartar",
                    callback_data=SourceCallback(action="reject", source_id=source_id).pack(),
                ),
            ]
        ]
    )

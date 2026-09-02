"""Teclados inline del ciclo de confirmación."""

from __future__ import annotations

from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

ConfirmAction = Literal["confirm", "correct", "reject"]
EditAction = Literal["confirm", "reject"]


class SourceCallback(CallbackData, prefix="src"):
    """Botones del resumen de una foto."""

    action: ConfirmAction
    source_id: int


class EditCallback(CallbackData, prefix="edit"):
    """Botones de un alta o una baja por texto."""

    action: EditAction
    edit_id: int


class CandidateCallback(CallbackData, prefix="pick"):
    """Elección entre varias entradas candidatas a borrar."""

    entry_id: int
    edit_id: int


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


def edit_keyboard(edit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Sí",
                    callback_data=EditCallback(action="confirm", edit_id=edit_id).pack(),
                ),
                InlineKeyboardButton(
                    text="❌ No",
                    callback_data=EditCallback(action="reject", edit_id=edit_id).pack(),
                ),
            ]
        ]
    )


def candidates_keyboard(candidates: list[tuple[int, str]], edit_id: int) -> InlineKeyboardMarkup:
    """Un botón por candidata (id, etiqueta corta) más un botón de cancelar."""
    rows = [
        [
            InlineKeyboardButton(
                text=label,
                callback_data=CandidateCallback(entry_id=entry_id, edit_id=edit_id).pack(),
            )
        ]
        for entry_id, label in candidates
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="❌ Ninguna",
                callback_data=EditCallback(action="reject", edit_id=edit_id).pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)

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


class ScheduleCallback(CallbackData, prefix="sch"):
    """Qué hacer con un horario nuevo cuando ya hay otros vigentes."""

    action: Literal["add", "replace"]
    source_id: int
    target: int  # id del horario a reemplazar; 0 en `add`


class CandidateCallback(CallbackData, prefix="pick"):
    """Elección entre varias candidatas a borrar: entradas de agenda o recordatorios.

    `target_id` y no `entry_id` porque qué es ese id lo dice la acción del `edit` que está
    pendiente en el grafo, no el botón.
    """

    target_id: int
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


def no_reminder_keyboard(edit_id: int) -> InlineKeyboardMarkup:
    """Salida de un toque a «¿te aviso a alguna hora?».

    Solo lleva el «no»: el «sí» necesita una hora, y esa se escribe. Sin este botón, la
    única forma de cerrar la pregunta sería escribir, que es justo lo que dejó atrapado al
    usuario en la Fase 6.2.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔕 Sin aviso",
                    callback_data=EditCallback(action="reject", edit_id=edit_id).pack(),
                )
            ]
        ]
    )


def candidates_keyboard(candidates: list[tuple[int, str]], edit_id: int) -> InlineKeyboardMarkup:
    """Un botón por candidata (id, etiqueta corta) más un botón de cancelar."""
    rows = [
        [
            InlineKeyboardButton(
                text=label,
                callback_data=CandidateCallback(target_id=target_id, edit_id=edit_id).pack(),
            )
        ]
        for target_id, label in candidates
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


def schedule_keyboard(source_id: int, existing: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Horario nuevo con otros ya vigentes: añadir aparte o reemplazar uno concreto.

    Sin esto el horario nuevo pisaba a los anteriores en silencio, que es justo lo que no
    debe pasar: la rotación académica y la jornada extendida conviven.
    """
    rows = [
        [
            InlineKeyboardButton(
                text="➕ Añadir aparte",
                callback_data=ScheduleCallback(action="add", source_id=source_id, target=0).pack(),
            )
        ]
    ]
    rows.extend(
        [
            InlineKeyboardButton(
                text=f"🔁 Reemplazar «{name[:30]}»",
                callback_data=ScheduleCallback(
                    action="replace", source_id=source_id, target=schedule_id
                ).pack(),
            )
        ]
        for schedule_id, name in existing[:3]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="✏️ Corregir",
                callback_data=SourceCallback(action="correct", source_id=source_id).pack(),
            ),
            InlineKeyboardButton(
                text="❌ Descartar",
                callback_data=SourceCallback(action="reject", source_id=source_id).pack(),
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def question_keyboard(source_id: int) -> InlineKeyboardMarkup:
    """Salida siempre visible mientras el bot pregunta.

    Sin esto la única forma de salir del interrogatorio era contestar, y cualquier texto
    se tomaba como respuesta: quedarse atrapado respondiendo era el comportamiento normal.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Descartar la foto",
                    callback_data=SourceCallback(action="reject", source_id=source_id).pack(),
                )
            ]
        ]
    )

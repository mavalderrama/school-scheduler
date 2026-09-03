"""Informe de `/estado`: qué está pasando sin gastar un solo token.

La "salud" de cada proveedor se deduce de la última llamada registrada en `llm_calls`, no
de un healthcheck en vivo: con `claude_sdk` un healthcheck **es** una llamada real y
descontaría de la cuota cada vez que alguien escribe /estado. Para comprobarlo de verdad
está `/estado check`, que sí llama a los proveedores.
"""

from __future__ import annotations

import html
from datetime import date, timedelta
from typing import Any

from django.utils import timezone

from app.config import Settings
from app.db import repo
from app.llm.compose import format_date_es
from app.llm.provider import LLMProviders
from app.services import schedule as schedule_service
from app.services import schoolcal
from app.services.scope import Scope

TOKEN_LIFETIME_DAYS = 365
TOKEN_WARN_DAYS = 30


def _token_line(settings: Settings, today: date) -> str | None:
    if "claude_sdk" not in settings.providers_in_use:
        return None
    issued = settings.claude_token_issued_at
    if issued is None:
        return "🔑 Token de suscripción: sin CLAUDE_TOKEN_ISSUED_AT, no sé cuándo caduca."
    expires = issued + timedelta(days=TOKEN_LIFETIME_DAYS)
    left = (expires - today).days
    mark = "⚠️" if left <= TOKEN_WARN_DAYS else "🔑"
    return (
        f"{mark} Token de suscripción: caduca el "
        f"{format_date_es(expires, with_year=True)} ({left} días)."
    )


def _usage_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["  (sin llamadas este mes)"]
    lines = []
    for row in rows:
        provider = str(row["provider"])
        calls = int(row["calls"] or 0)
        errors = int(row["errors"] or 0)
        tokens_in = int(row["input_tokens"] or 0)
        tokens_out = int(row["output_tokens"] or 0)
        cached = int(row["cache_read_tokens"] or 0)
        detail = f"  • {provider}: {calls} llamada(s)"
        if errors:
            detail += f", {errors} con error"
        if tokens_in or tokens_out:
            detail += f", {tokens_in}↓/{tokens_out}↑ tokens"
        if cached:
            detail += f", {cached} de caché"
        if row.get("cost_usd"):
            detail += f", ${row['cost_usd']}"
        lines.append(detail)
    return lines


async def build_status(settings: Settings, providers: LLMProviders, *, scope: Scope) -> str:
    """Informe barato: solo consultas a la DB."""
    now = timezone.now()
    today = now.astimezone(settings.zoneinfo).date()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    lines = ["🩺 <b>Estado del bot</b>", ""]

    # Proveedores configurados y su última llamada conocida.
    lines.append(f"Visión: <code>{providers.vision.name}</code>")
    lines.append(f"Texto: <code>{providers.text.name}</code>")
    family = await repo.get_family(scope.family_id)
    if family is not None and not family.uses_host_llm:
        rows = await repo.credentials_of(scope.family_id)
        have = {row.provider for row in rows if row.is_active and row.secret}
        needed = {str(family.vision_provider), str(family.text_provider)}
        missing = sorted(needed - have - {"ollama"})
        if missing:
            lines.append(f"⚠️ Falta la clave de: {', '.join(missing)} (mándamela con /clave)")
        used = await repo.calls_this_month(scope.family_id, month_start)
        lines.append(f"📈 Llamadas este mes: {used} de {family.monthly_call_limit}")
    last_calls = await repo.last_call_by_provider(scope.family_id)
    if last_calls:
        lines.append("")
        lines.append("Última llamada por proveedor:")
        for provider, call in sorted(last_calls.items()):
            when = call.created_at.astimezone(settings.zoneinfo).strftime("%d/%m %H:%M")
            mark = "✅" if call.ok else "⚠️"
            detail = f"  {mark} {provider}: {call.task} el {when}"
            if not call.ok and call.error:
                detail += f" — {html.escape(call.error[:80])}"
            lines.append(detail)

    # Horario rotativo: es lo primero que se mira cuando algo no cuadra por la mañana.
    if settings.schedule_enabled:
        lines.append("")
        loaded = await schedule_service.load(scope, today)
        if loaded is None:
            lines.append("🗓️ Sin horario cargado.")
        else:
            index = schedule_service.week_index(today, loaded.template)
            label = next((s.week_label for s in loaded.slots if s.week_index == index), "?")
            lines.append(
                f"🗓️ Horario: <b>{html.escape(loaded.template.name)}</b> "
                f"({len(loaded.slots)} franjas). Esta semana es la <b>Semana {label}</b>."
            )
            free = schoolcal.next_non_school_day(
                today, exceptions=loaded.exceptions, country=scope.country
            )
            if free is not None:
                lines.append(
                    f"   Próximo día sin clase: {format_date_es(free.day)}"
                    f" ({html.escape(free.reason or '')})."
                )

    # Notificaciones.
    lines.append("")
    last = await repo.last_notification(scope.child_id)
    if last is None:
        lines.append("🔔 Todavía no he enviado ninguna notificación.")
    else:
        when = last.sent_at.astimezone(settings.zoneinfo).strftime("%d/%m %H:%M")
        mark = "✅" if last.ok else "⚠️"
        lines.append(f"🔔 Última notificación: {mark} {last.kind} el {when}.")

    # Últimas fotos y correcciones.
    sources = await repo.recent_sources(scope.family_id, 3)
    if sources:
        lines.append("")
        lines.append("📥 Últimas fuentes:")
        for source in sources:
            when = source.created_at.astimezone(settings.zoneinfo).strftime("%d/%m %H:%M")
            who = source.submitted_by.display_name if source.submitted_by else "—"
            provider = source.llm_provider or "—"
            lines.append(
                f"  • #{source.pk} {source.kind} ({source.status}) {when}, "
                f"{html.escape(who)}, {provider}"
            )

    waiting = await repo.count_awaiting_extraction(scope.family_id)
    if waiting:
        lines.append("")
        lines.append(f"⏳ {waiting} foto(s) esperando a que haya cuota; las reintento solo.")

    # Consumo del mes.
    lines.append("")
    lines.append(
        f"📊 Consumo desde el {month_start.astimezone(settings.zoneinfo).day}/"
        f"{month_start.astimezone(settings.zoneinfo).month}:"
    )
    lines.extend(_usage_lines(await repo.llm_usage_by_provider(scope.family_id, month_start)))

    entries, hits = await repo.cache_stats()
    lines.append(f"  • caché: {entries} entrada(s), {hits} acierto(s) acumulados")

    token_line = _token_line(settings, today)
    if token_line:
        lines.append("")
        lines.append(token_line)

    return "\n".join(lines)


async def check_providers(providers: LLMProviders) -> str:
    """Healthcheck real de cada proveedor. Con claude_sdk gasta cuota: solo bajo petición."""
    seen: dict[str, str] = {}
    for chain in (providers.vision, providers.text):
        for provider in chain.providers:
            if provider.name in seen:
                continue
            health = await provider.healthcheck()
            mark = "✅" if health.ok else "❌"
            latency = f" {health.latency_ms} ms" if health.latency_ms is not None else ""
            detail = f" — {html.escape(health.detail[:120])}" if health.detail else ""
            seen[provider.name] = f"{mark} {provider.name}{latency}{detail}"
    return "🩺 <b>Healthcheck</b>\n" + "\n".join(seen[name] for name in sorted(seen))

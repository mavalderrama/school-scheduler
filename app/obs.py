"""Trazas OpenTelemetry de las llamadas al LLM (opcional, apagadas por defecto).

Por qué OTel y no el SDK de Langfuse: Langfuse ingiere OTLP, así que con esto se puede
apuntar a un Langfuse autoalojado —o a cualquier otro backend— sin tocar el código. Y
`claude_sdk` es un subproceso, no una librería instrumentable: la traza hay que escribirla
a mano de todos modos.

El interruptor real es `OTEL_ENABLED`. Apagado (lo normal) todo esto es un `nullcontext`
y no cuesta nada. Las dependencias van en el extra `otel`, así que la imagen base no las
lleva; si faltan, se avisa una vez y se sigue sin trazas.

Nombres según las convenciones semánticas GenAI de OTel (`gen_ai.*`), que es lo que
entienden Langfuse y compañía.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from app.log import get_logger

if TYPE_CHECKING:  # pragma: no cover - solo para tipos
    from app.config import Settings
    from app.llm.schemas import LLMUsage

log = get_logger(__name__)

_tracer: Any | None = None
_enabled = False


def setup_tracing(settings: Settings) -> bool:
    """Arranca el exportador OTLP si está configurado. Devuelve si quedó activo."""
    global _tracer, _enabled
    if not settings.otel_enabled:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("otel_missing", detail="falta el extra `otel`: uv sync --extra otel")
        return False

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("agenda-escolar-bot")
    _enabled = True
    log.info("otel_enabled", service=settings.otel_service_name)
    return True


@contextmanager
def llm_span(task: str, provider: str, model: str | None) -> Iterator[Any]:
    """Span de una llamada al LLM. Sin OTel activo no hace nada."""
    if not _enabled or _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(f"llm.{task}") as span:
        span.set_attribute("gen_ai.operation.name", task)
        span.set_attribute("gen_ai.system", provider)
        if model:
            span.set_attribute("gen_ai.request.model", model)
        yield span


def record_usage(span: Any, usage: LLMUsage | None, *, ok: bool, error: str | None) -> None:
    """Cuelga del span el consumo y el resultado, con los nombres de la convención GenAI."""
    if span is None:
        return
    if usage is not None:
        if usage.input_tokens is not None:
            span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
        if usage.output_tokens is not None:
            span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
        if usage.model:
            span.set_attribute("gen_ai.response.model", usage.model)
    span.set_attribute("llm.ok", ok)
    if error:
        span.set_attribute("error.type", error[:200])

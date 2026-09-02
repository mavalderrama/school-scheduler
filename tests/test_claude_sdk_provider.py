"""ClaudeSDKProvider: bloqueo de herramientas, entorno del subproceso y manejo de errores."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultError, ResultMessage

from app.config import Settings
from app.llm import claude_sdk
from app.llm.claude_sdk import DISALLOWED_TOOLS, ClaudeSDKProvider
from app.llm.provider import LLMOutputError, LLMQuotaError, LLMUnavailableError
from app.llm.schemas import ExtractionResult, OkProbe, QAPair


def _result(**overrides: Any) -> ResultMessage:
    base: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 1200,
        "duration_api_ms": 1000,
        "is_error": False,
        "num_turns": 1,
        "session_id": "sess-1",
        "total_cost_usd": 0.001,
        "usage": {"input_tokens": 50, "output_tokens": 5},
        "structured_output": {"ok": True},
    }
    return ResultMessage(**{**base, **overrides})


class QuerySpy:
    """Sustituye `query()` y guarda las opciones con las que se llamó."""

    def __init__(self, messages: list[Any] | None = None, raises: Exception | None = None):
        self.messages = messages or [_result()]
        self.raises = raises
        self.options: ClaudeAgentOptions | None = None
        self.prompt: str | None = None

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        self.prompt = prompt
        self.options = options
        return self._gen()

    async def _gen(self) -> AsyncIterator[Any]:
        if self.raises:
            raise self.raises
        for m in self.messages:
            yield m


TODAY = date(2026, 9, 2)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> QuerySpy:
    s = QuerySpy()
    monkeypatch.setattr(claude_sdk, "query", s)
    return s


async def test_healthcheck_ok_and_tools_locked(settings: Settings, spy: QuerySpy) -> None:
    health = await ClaudeSDKProvider(settings).healthcheck()
    assert health.ok
    assert health.model == "sonnet"
    assert "tokens in=50 out=5" in health.detail

    opts = spy.options
    assert opts is not None
    assert opts.tools == []  # ninguna herramienta para texto
    assert opts.allowed_tools == []
    assert set(DISALLOWED_TOOLS) <= set(opts.disallowed_tools)
    assert opts.setting_sources == []
    assert opts.max_turns == 1
    assert opts.output_format == {"type": "json_schema", "schema": OkProbe.model_json_schema()}
    assert opts.output_format["schema"]["required"] == ["ok"]


async def test_subprocess_env_uses_subscription_only(settings: Settings, spy: QuerySpy) -> None:
    await ClaudeSDKProvider(settings).healthcheck()
    assert spy.options is not None
    env = spy.options.env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-test"
    assert "ANTHROPIC_API_KEY" not in env
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["CLAUDE_CONFIG_DIR"] == str(settings.data_dir / "claude")
    assert (settings.data_dir / "claude").is_dir()


async def test_healthcheck_reports_model_saying_false(settings: Settings, spy: QuerySpy) -> None:
    spy.messages = [_result(structured_output={"ok": False})]
    health = await ClaudeSDKProvider(settings).healthcheck()
    assert not health.ok
    assert "ok=false" in health.detail


async def test_missing_structured_output_is_output_error(settings: Settings, spy: QuerySpy) -> None:
    spy.messages = [_result(subtype="error_max_structured_output_retries", structured_output=None)]
    with pytest.raises(LLMOutputError):
        await ClaudeSDKProvider(settings)._run_json("x", tools=[], schema={})


async def test_quota_error_is_classified(settings: Settings, spy: QuerySpy) -> None:
    spy.raises = ResultError(
        "fail",
        data={"subtype": "success", "api_error_status": 429, "result": "API Error: rate limit"},
        exit_code=1,
    )
    with pytest.raises(LLMQuotaError):
        await ClaudeSDKProvider(settings)._run_json("x", tools=[], schema={})


async def test_other_result_error_is_unavailable(settings: Settings, spy: QuerySpy) -> None:
    spy.raises = ResultError(
        "fail", data={"subtype": "error_during_execution", "api_error_status": 500}, exit_code=1
    )
    with pytest.raises(LLMUnavailableError):
        await ClaudeSDKProvider(settings)._run_json("x", tools=[], schema={})


async def test_healthcheck_never_raises(settings: Settings, spy: QuerySpy) -> None:
    spy.raises = RuntimeError("boom")
    health = await ClaudeSDKProvider(settings).healthcheck()
    assert not health.ok and "boom" in health.detail


async def test_text_tasks_are_not_capped_at_one_turn(settings: Settings, spy: QuerySpy) -> None:
    """Regresión: `refine_extraction` fallaba a ratos con `error_max_turns`.

    Las tareas de texto estaban fijadas a `max_turns=1`, así que `CLAUDE_SDK_MAX_TURNS` no
    se aplicaba a ninguna salvo la de visión. Con salida estructurada el modelo a veces
    necesita un turno más, y el refinado —que lleva la extracción entera en el prompt— era
    el que más lo notaba: fallaba de forma intermitente contra la misma foto.
    """
    provider = ClaudeSDKProvider(settings.model_copy(update={"claude_sdk_max_turns": 4}))
    extraction = ExtractionResult(entries=[], doubts=[], detected_language="es")

    spy.messages = [_result(structured_output=extraction.model_dump(mode="json"))]
    await provider.refine_extraction(extraction, [QAPair(question="q", answer="r")], TODAY)
    assert spy.options is not None and spy.options.max_turns == 4

    await provider.correct_extraction(extraction, "cambia el jueves", TODAY)
    assert spy.options is not None and spy.options.max_turns == 4

    spy.messages = [_result(structured_output={"action": "help"})]
    await provider.classify_intent("hola", [], TODAY, False)
    assert spy.options is not None and spy.options.max_turns == 4


async def test_the_healthcheck_stays_at_one_turn(settings: Settings, spy: QuerySpy) -> None:
    """Si la sonda de conectividad no responde a la primera, algo va mal de verdad."""
    provider = ClaudeSDKProvider(settings.model_copy(update={"claude_sdk_max_turns": 4}))
    await provider.healthcheck()
    assert spy.options is not None and spy.options.max_turns == 1

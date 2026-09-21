"""`intraday/llm.py`: the headless `claude -p --bare` invocation wrapper (S4,
`BRIEF_INTRADAY.md`, Phase 2). Every test injects a fake subprocess `run` callable --
never a real subprocess or network call, same boundary-mocking rule as
`test_execution_market_data.py`'s injected SDK client."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from semimc.config import IntradayConfig
from semimc.intraday.context import build_context
from semimc.intraday.ledger import IntradayLedger
from semimc.intraday.llm import (
    ClaudeCliClient,
    ClaudeCliError,
    DailyBudgetExhausted,
    invoke_role,
)

AS_OF = pd.Timestamp("2026-09-21")


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _result_json(**overrides) -> str:
    payload = {
        "session_id": "sess-1",
        "result": "done",
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "is_error": False,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _recording_run(stdout: str = "", returncode: int = 0):
    calls: list[dict] = []

    def run(argv, stdin_text, timeout_seconds):
        calls.append({"argv": argv, "stdin": stdin_text, "timeout": timeout_seconds})
        return _completed(stdout, returncode)

    return calls, run


def _ledger(tmp_path) -> IntradayLedger:
    return IntradayLedger(tmp_path / "intraday.sqlite3")


# -- ClaudeCliClient.call: argv construction ---------------------------------------------


def test_call_argv_contains_every_required_flag(tmp_path):
    calls, run = _recording_run(_result_json())
    client = ClaudeCliClient(run=run)
    prompt_path = tmp_path / "trader.md"
    prompt_path.write_text("be a trader")

    client.call(
        role="trader",
        system_prompt_path=prompt_path,
        model="haiku",
        context_payload={"a": 1},
        max_budget_usd=1.0,
        max_turns=6,
        timeout_seconds=300,
    )

    argv = calls[0]["argv"]
    assert "--bare" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--max-turns") + 1] == "6"
    assert argv[argv.index("--max-budget-usd") + 1] == "1.0"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--append-system-prompt-file") + 1] == str(prompt_path)


def test_call_never_passes_allowedtools():
    calls, run = _recording_run(_result_json())
    client = ClaudeCliClient(run=run)
    client.call(
        role="trader",
        system_prompt_path=Path("dummy.md"),
        model="haiku",
        context_payload={},
        max_budget_usd=1.0,
        max_turns=6,
        timeout_seconds=300,
    )
    assert "--allowedTools" not in calls[0]["argv"]
    assert "--allowed-tools" not in calls[0]["argv"]


def test_call_includes_json_schema_only_when_passed():
    calls, run = _recording_run(_result_json())
    client = ClaudeCliClient(run=run)
    schema = {"type": "object"}

    client.call(
        role="trader",
        system_prompt_path=Path("dummy.md"),
        model="haiku",
        context_payload={},
        max_budget_usd=1.0,
        max_turns=6,
        timeout_seconds=300,
        json_schema=schema,
    )

    argv = calls[0]["argv"]
    assert "--json-schema" in argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == schema


def test_call_omits_json_schema_when_not_passed():
    calls, run = _recording_run(_result_json())
    client = ClaudeCliClient(run=run)
    client.call(
        role="researcher",
        system_prompt_path=Path("dummy.md"),
        model="sonnet",
        context_payload={},
        max_budget_usd=1.0,
        max_turns=6,
        timeout_seconds=300,
    )
    assert "--json-schema" not in calls[0]["argv"]


def test_call_pipes_the_context_payload_on_stdin_not_argv():
    calls, run = _recording_run(_result_json())
    client = ClaudeCliClient(run=run)
    client.call(
        role="trader",
        system_prompt_path=Path("dummy.md"),
        model="haiku",
        context_payload={"watchlist": ["NVDA"]},
        max_budget_usd=1.0,
        max_turns=6,
        timeout_seconds=300,
    )
    assert json.loads(calls[0]["stdin"]) == {"watchlist": ["NVDA"]}
    assert not any("NVDA" in arg for arg in calls[0]["argv"])


# -- ClaudeCliClient.call: response parsing ------------------------------------------------


def test_call_parses_a_successful_response():
    _, run = _recording_run(
        _result_json(
            total_cost_usd=0.0234,
            usage={"input_tokens": 1200, "output_tokens": 300},
            structured_output={"proposals": []},
            result="ok",
            session_id="sess-42",
        )
    )
    client = ClaudeCliClient(run=run)
    response = client.call(
        role="trader",
        system_prompt_path=Path("dummy.md"),
        model="haiku",
        context_payload={},
        max_budget_usd=1.0,
        max_turns=6,
        timeout_seconds=300,
    )
    assert response.cost_usd == pytest.approx(0.0234)
    assert response.input_tokens == 1200
    assert response.output_tokens == 300
    assert response.structured_output == {"proposals": []}
    assert response.result_text == "ok"
    assert response.session_id == "sess-42"
    assert response.is_error is False


def test_call_raises_on_nonzero_exit():
    _, run = _recording_run("", returncode=1)
    client = ClaudeCliClient(run=run)
    with pytest.raises(ClaudeCliError):
        client.call(
            role="trader",
            system_prompt_path=Path("dummy.md"),
            model="haiku",
            context_payload={},
            max_budget_usd=1.0,
            max_turns=6,
            timeout_seconds=300,
        )


def test_call_raises_on_is_error_result():
    _, run = _recording_run(_result_json(is_error=True, result="budget exceeded"))
    client = ClaudeCliClient(run=run)
    with pytest.raises(ClaudeCliError, match="budget exceeded"):
        client.call(
            role="trader",
            system_prompt_path=Path("dummy.md"),
            model="haiku",
            context_payload={},
            max_budget_usd=1.0,
            max_turns=6,
            timeout_seconds=300,
        )


def test_call_raises_on_unparseable_stdout():
    _, run = _recording_run("not json")
    client = ClaudeCliClient(run=run)
    with pytest.raises(ClaudeCliError):
        client.call(
            role="trader",
            system_prompt_path=Path("dummy.md"),
            model="haiku",
            context_payload={},
            max_budget_usd=1.0,
            max_turns=6,
            timeout_seconds=300,
        )


def test_call_raises_on_timeout():
    def run(argv, stdin_text, timeout_seconds):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout_seconds)

    client = ClaudeCliClient(run=run)
    with pytest.raises(ClaudeCliError):
        client.call(
            role="trader",
            system_prompt_path=Path("dummy.md"),
            model="haiku",
            context_payload={},
            max_budget_usd=1.0,
            max_turns=6,
            timeout_seconds=1,
        )


# -- invoke_role: budget integration -------------------------------------------------------


def test_invoke_role_raises_without_calling_the_client_once_daily_budget_is_spent(tmp_path):
    ledger = _ledger(tmp_path)
    config = IntradayConfig(budget={"max_cost_usd_per_day": 1.0})
    ledger.record_model_call(AS_OF, "cycle_1", "trader", "haiku", "hash1", 1000, 1000, cost_usd=1.0)
    calls, run = _recording_run(_result_json())
    client = ClaudeCliClient(run=run)
    context = build_context("trader", AS_OF, "cycle_2", sections={})

    with pytest.raises(DailyBudgetExhausted):
        invoke_role(
            "trader",
            as_of=AS_OF,
            cycle="cycle_2",
            config=config,
            ledger=ledger,
            client=client,
            context=context,
        )
    assert calls == []
    ledger.close()


def test_invoke_role_records_a_model_call_using_the_context_hash(tmp_path):
    ledger = _ledger(tmp_path)
    config = IntradayConfig()
    _, run = _recording_run(_result_json(total_cost_usd=0.02))
    client = ClaudeCliClient(run=run)
    context = build_context("trader", AS_OF, "cycle_1", sections={"a": 1})

    invoke_role(
        "trader",
        as_of=AS_OF,
        cycle="cycle_1",
        config=config,
        ledger=ledger,
        client=client,
        context=context,
    )

    rows = ledger.model_calls_for(AS_OF)
    assert len(rows) == 1
    assert rows[0]["role"] == "trader"
    assert rows[0]["model"] == config.models.trader
    assert rows[0]["cost_usd"] == pytest.approx(0.02)
    from semimc.intraday.context import context_hash

    assert rows[0]["prompt_hash"] == context_hash(context)
    ledger.close()


def test_invoke_role_records_an_incident_when_a_call_exceeds_the_per_call_cap(tmp_path):
    ledger = _ledger(tmp_path)
    config = IntradayConfig(budget={"max_cost_usd_per_call": 0.01})
    _, run = _recording_run(_result_json(total_cost_usd=0.05))
    client = ClaudeCliClient(run=run)
    context = build_context("trader", AS_OF, "cycle_1", sections={})

    response = invoke_role(
        "trader",
        as_of=AS_OF,
        cycle="cycle_1",
        config=config,
        ledger=ledger,
        client=client,
        context=context,
    )

    assert response.cost_usd == pytest.approx(0.05)
    incidents = ledger.incidents()
    assert len(incidents) == 1
    assert "max_cost_usd_per_call" in incidents[0]["message"]
    ledger.close()


def test_invoke_role_uses_the_trader_json_schema_only_for_the_trader_role(tmp_path):
    ledger = _ledger(tmp_path)
    config = IntradayConfig()
    calls, run = _recording_run(_result_json())
    client = ClaudeCliClient(run=run)
    context = build_context("researcher", AS_OF, "research", sections={})

    invoke_role(
        "researcher",
        as_of=AS_OF,
        cycle="research",
        config=config,
        ledger=ledger,
        client=client,
        context=context,
    )

    assert "--json-schema" not in calls[0]["argv"]
    ledger.close()

"""CLI smoke tests for `semimc intraday ...` (S4, `BRIEF_INTRADAY.md`, Phase 5):
`preflight`/`preopen`/`monitor`/`open-batch`/`cycle`/`carry`. Same shallow-on-numbers,
strict-on-plumbing convention as `test_cli.py`'s own paper command tests -- every broker/
market-data/LLM client is patched to a fake, no network, no subprocess.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from semimc.cli import app
from semimc.execution.fake_broker import FakeBroker
from semimc.intraday.llm import ClaudeCliClient

runner = CliRunner()


def _intraday_overrides(tmp_path: Path, **extra) -> list[str]:
    overrides = [
        "--set",
        f"intraday.ledger_path={(tmp_path / 'intraday.sqlite3').as_posix()}",
        "--set",
        f"intraday.kill_switch_path={(tmp_path / 'KILL_SWITCH').as_posix()}",
    ]
    for key, value in extra.items():
        overrides += ["--set", f"{key}={value}"]
    return overrides


def _tradeable_overrides(tmp_path: Path) -> list[str]:
    return _intraday_overrides(
        tmp_path,
        **{"intraday.trading_enabled": "true", "intraday.registered_account": "FAKE-PAPER-0001"},
    )


class _FakeQuote:
    def __init__(self, price: float, at: dt.datetime | None = None):
        self.bid_price = price - 0.05
        self.ask_price = price + 0.05
        self.timestamp = at or dt.datetime.now(dt.UTC)


class _FakeMarketData:
    def __init__(self, quotes: dict[str, float] | None = None):
        self._quotes = quotes or {}

    def latest_quote(self, symbol, at):
        price = self._quotes.get(symbol)
        return _FakeQuote(price) if price is not None else None


def _result_json(**overrides) -> str:
    payload = {
        "session_id": "sess-1",
        "result": "done",
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "is_error": False,
        "structured_output": {"proposals": []},
    }
    payload.update(overrides)
    return json.dumps(payload)


@pytest.fixture
def fake_intraday_env(monkeypatch):
    """Patches every `from_env` construction `cli.py`'s `intraday` commands make --
    broker, market data, and the headless LLM client -- to injectable fakes, mirroring
    `test_cli.py`'s own `fake_alpaca_broker` fixture."""
    broker = FakeBroker(account_number="FAKE-PAPER-0001")
    market_data = _FakeMarketData()

    def fake_run(argv, stdin_text, timeout_seconds):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=_result_json(), stderr="")

    monkeypatch.setattr(
        "semimc.execution.alpaca_broker.AlpacaPaperBroker.from_env",
        classmethod(lambda cls, **kwargs: broker),
    )
    monkeypatch.setattr(
        "semimc.execution.market_data.AlpacaMarketDataClient.from_env",
        classmethod(lambda cls, **kwargs: market_data),
    )
    monkeypatch.setattr(
        "semimc.intraday.llm.ClaudeCliClient.from_env",
        classmethod(lambda cls: ClaudeCliClient(run=fake_run)),
    )
    return broker, market_data


def test_preflight_passes_cleanly_when_trading_is_enabled(tmp_path, fake_intraday_env):
    result = runner.invoke(app, ["intraday", "preflight"] + _tradeable_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Preflight: passed" in result.output


def test_preflight_fails_the_job_on_the_kill_switch(tmp_path, fake_intraday_env):
    (tmp_path / "KILL_SWITCH").write_text("stop", encoding="utf-8")
    result = runner.invoke(app, ["intraday", "preflight"] + _tradeable_overrides(tmp_path))
    assert result.exit_code != 0
    assert "Preflight: failed" in result.output


def test_preopen_runs_cleanly_with_an_empty_watchlist(tmp_path, fake_intraday_env):
    result = runner.invoke(app, ["intraday", "preopen"] + _intraday_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "(none)" in result.output


def test_monitor_runs_cleanly_with_no_open_positions(tmp_path, fake_intraday_env):
    result = runner.invoke(app, ["intraday", "monitor"] + _intraday_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Closed: (none)" in result.output


def test_open_batch_declines_cleanly_when_trading_is_disabled(tmp_path, fake_intraday_env):
    result = runner.invoke(app, ["intraday", "open-batch"] + _intraday_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Skipped" in result.output


def test_cycle_runs_cleanly_with_no_proposals(tmp_path, fake_intraday_env):
    result = runner.invoke(app, ["intraday", "cycle"] + _tradeable_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Proposals: 0 seen" in result.output


def test_carry_runs_cleanly_with_no_positions(tmp_path, fake_intraday_env):
    result = runner.invoke(app, ["intraday", "carry"] + _tradeable_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Carried: (none)" in result.output

"""CLI smoke tests for `semimc intraday ...` (S4, `BRIEF_INTRADAY.md`, Phases 5-6):
`preflight`/`research`/`preopen`/`monitor`/`open-batch`/`cycle`/`carry`. Same
shallow-on-numbers, strict-on-plumbing convention as `test_cli.py`'s own paper command
tests -- every broker/market-data/news/LLM client is patched to a fake, no network, no
subprocess. `research` also needs `data.load_market_data` patched (its own-data inputs --
OHLC, earnings -- come from the shared price cache, which doesn't exist in a test run).
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pandas as pd
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
        f"intraday.shadow_ledger_path={(tmp_path / 'intraday_shadow.sqlite3').as_posix()}",
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


class _FakeNewsClient:
    def __init__(self, articles: list | None = None):
        self._articles = articles or []

    def get_news(self, symbols, start, end, limit=50):
        return self._articles


class _FakePitMarketData:
    """Stands in for `data.load_market_data`'s return value -- `research`'s own-data
    inputs (`_premarket_moves`/`_upcoming_earnings`/`_realized_vol`, `intraday/jobs.py`)
    all degrade to empty/absent gracefully, so this needs no real price cache."""

    earnings = None
    ohlc: dict = {}

    def spot(self, tickers, as_of=None):
        return pd.Series(dtype=float)

    def has_ohlc(self, ticker):
        return False


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
    broker, market data, news client, and the headless LLM client -- to injectable fakes,
    mirroring `test_cli.py`'s own `fake_alpaca_broker` fixture. Also patches
    `data.load_market_data` (Phase 6's `research` command's only non-`from_env`
    dependency) to a fake `MarketData` -- harmless for every other command, which never
    calls it."""
    broker = FakeBroker(account_number="FAKE-PAPER-0001")
    market_data = _FakeMarketData()
    news_client = _FakeNewsClient()

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
        "semimc.execution.news.AlpacaNewsClient.from_env",
        classmethod(lambda cls, **kwargs: news_client),
    )
    monkeypatch.setattr(
        "semimc.intraday.llm.ClaudeCliClient.from_env",
        classmethod(lambda cls: ClaudeCliClient(run=fake_run)),
    )
    monkeypatch.setattr("semimc.data.load_market_data", lambda *a, **k: _FakePitMarketData())
    return broker, market_data, news_client


def test_preflight_passes_cleanly_when_trading_is_enabled(tmp_path, fake_intraday_env):
    result = runner.invoke(app, ["intraday", "preflight"] + _tradeable_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Preflight: passed" in result.output


def test_preflight_fails_the_job_on_the_kill_switch(tmp_path, fake_intraday_env):
    (tmp_path / "KILL_SWITCH").write_text("stop", encoding="utf-8")
    result = runner.invoke(app, ["intraday", "preflight"] + _tradeable_overrides(tmp_path))
    assert result.exit_code != 0
    assert "Preflight: failed" in result.output


def test_research_declines_cleanly_when_trading_is_disabled(tmp_path, fake_intraday_env):
    result = runner.invoke(app, ["intraday", "research"] + _intraday_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Skipped" in result.output


def test_research_runs_cleanly_and_records_a_brief(tmp_path, fake_intraday_env, monkeypatch):
    def fake_run(argv, stdin_text, timeout_seconds):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_result_json(
                structured_output={"morning_brief": "quiet morning", "watchlist": []}
            ),
            stderr="",
        )

    monkeypatch.setattr(
        "semimc.intraday.llm.ClaudeCliClient.from_env",
        classmethod(lambda cls: ClaudeCliClient(run=fake_run)),
    )
    result = runner.invoke(app, ["intraday", "research"] + _tradeable_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Brief recorded" in result.output


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


def test_eod_review_runs_cleanly_and_writes_a_report(tmp_path, fake_intraday_env):
    """`intraday.reports_dir` and `paper.ledger_path` must both be redirected here --
    otherwise this command would resolve them against the real project root and either
    write into the real repo's `reports/` directory or open the real `paper.sqlite3`
    (`CLAUDE.md`'s own `signals --holdout` gotcha, the exact reason `reports_dir` is a
    `--set`-overridable config field rather than a literal in `cli.py`)."""
    result = runner.invoke(
        app,
        ["intraday", "eod-review"]
        + _intraday_overrides(
            tmp_path,
            **{
                "intraday.reports_dir": (tmp_path / "reports").as_posix(),
                "paper.ledger_path": (tmp_path / "paper.sqlite3").as_posix(),
            },
        ),
    )
    assert result.exit_code == 0, result.output
    assert "Wrote" in result.output
    report_files = list((tmp_path / "reports").rglob("eod.md"))
    assert len(report_files) == 1
    content = report_files[0].read_text(encoding="utf-8")
    assert "No proposals today." in content
    assert "not yet built (Phase 9b)" in content


# -- placebo (T6's control group, Phase 7) -------------------------------------------------


@pytest.fixture
def fake_placebo_env(monkeypatch):
    """The placebo path needs only market data -- confirms it structurally, not just by
    convention: `AlpacaPaperBroker.from_env`/`ClaudeCliClient.from_env` are made to raise
    if ever called, since the placebo must never construct a real broker or an LLM client.
    """
    market_data = _FakeMarketData()

    def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError("the placebo path must never construct this")

    monkeypatch.setattr(
        "semimc.execution.market_data.AlpacaMarketDataClient.from_env",
        classmethod(lambda cls, **kwargs: market_data),
    )
    monkeypatch.setattr(
        "semimc.execution.alpaca_broker.AlpacaPaperBroker.from_env",
        classmethod(lambda cls, **kwargs: _must_not_be_called()),
    )
    monkeypatch.setattr(
        "semimc.intraday.llm.ClaudeCliClient.from_env",
        classmethod(lambda cls: _must_not_be_called()),
    )
    return market_data


def test_placebo_open_batch_runs_cleanly_with_an_empty_watchlist(tmp_path, fake_placebo_env):
    result = runner.invoke(
        app, ["intraday", "placebo", "open-batch"] + _intraday_overrides(tmp_path)
    )
    assert result.exit_code == 0, result.output
    assert "Skipped" in result.output


def test_placebo_cycle_runs_cleanly_with_an_empty_watchlist(tmp_path, fake_placebo_env):
    result = runner.invoke(app, ["intraday", "placebo", "cycle"] + _intraday_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Skipped" in result.output


def test_placebo_carry_runs_cleanly_with_no_positions(tmp_path, fake_placebo_env):
    result = runner.invoke(app, ["intraday", "placebo", "carry"] + _intraday_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Carried: (none)" in result.output

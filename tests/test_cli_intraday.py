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
from semimc.execution.broker import Clock
from semimc.execution.fake_broker import FakeBroker
from semimc.intraday.ledger import IntradayLedger
from semimc.intraday.llm import ClaudeCliClient

runner = CliRunner()


def _intraday_overrides(tmp_path: Path, **extra) -> list[str]:
    """`intraday.shadow_mode: false` (Phase 11's own default is `true`) -- every existing
    test in this file was written against `fake_intraday_env`'s real-`AlpacaPaperBroker`
    patch (`open-batch`/`cycle`/`carry`/`monitor` all now branch on `shadow_mode` to pick
    a broker, `cli.py::_intraday_execution_broker`), and this keeps every one of them
    exercising exactly that path rather than silently switching to a shadow `FakeBroker`
    none of them expect. Shadow mode itself gets separate, explicitly-labeled tests."""
    overrides = [
        "--set",
        f"intraday.ledger_path={(tmp_path / 'intraday.sqlite3').as_posix()}",
        "--set",
        f"intraday.shadow_ledger_path={(tmp_path / 'intraday_shadow.sqlite3').as_posix()}",
        "--set",
        f"intraday.kill_switch_path={(tmp_path / 'KILL_SWITCH').as_posix()}",
        "--set",
        "intraday.shadow_mode=false",
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

    def latest_quotes(self, symbols, feed="iex"):
        return {s: q for s in symbols if (q := self.latest_quote(s, at=None)) is not None}

    def latest_trades(self, symbols, feed="iex"):
        return {}

    def bars(self, symbols, start, end, minutes=5):
        return {}


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

    def before(self, date):
        return self

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

    def fake_run(argv, stdin_text, timeout_seconds, env):
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
    def fake_run(argv, stdin_text, timeout_seconds, env):
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


def test_monitor_omits_the_gap_line_on_the_days_first_call(tmp_path, fake_intraday_env):
    result = runner.invoke(app, ["intraday", "monitor"] + _intraday_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Gap since last check" not in result.output


def test_monitor_prints_the_gap_since_the_previous_call(tmp_path, fake_intraday_env):
    """Found 2026-09-23 -- two separate CLI invocations against the same ledger, the same
    way two separate scheduled GitHub Actions runs would, mirroring
    `test_shadow_broker_state_persists_across_two_separate_invocations`'s own shape."""
    runner.invoke(app, ["intraday", "monitor"] + _intraday_overrides(tmp_path))
    result = runner.invoke(app, ["intraday", "monitor"] + _intraday_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Gap since last check:" in result.output


def test_open_batch_declines_cleanly_when_trading_is_disabled(tmp_path, fake_intraday_env):
    result = runner.invoke(app, ["intraday", "open-batch"] + _intraday_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Skipped" in result.output


def test_cycle_runs_cleanly_with_no_proposals(tmp_path, fake_intraday_env):
    result = runner.invoke(app, ["intraday", "cycle"] + _tradeable_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Proposals: 0 seen" in result.output


@pytest.fixture
def at_carry_time(monkeypatch):
    """`carry` acts only in the last hour before the close (2026-10-19), and the CLI reads
    the wall clock, so pin it: 14:45 CT on a weekday, the scheduled run's time. Returns a
    setter for a test that needs another time (naive UTC, `_intraday_now`'s convention)."""

    def at(now: dt.datetime) -> None:
        monkeypatch.setattr("semimc.cli._intraday_now", lambda: now)

    at(dt.datetime(2026, 9, 21, 19, 45))
    return at


def test_carry_runs_cleanly_with_no_positions(tmp_path, fake_intraday_env, at_carry_time):
    result = runner.invoke(app, ["intraday", "carry"] + _tradeable_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Carried: (none)" in result.output


def test_carry_dispatched_mid_session_does_nothing(tmp_path, fake_intraday_env, at_carry_time):
    """2026-10-19: an 11:45 CT carry on a regular day closed AVGO three hours early."""
    at_carry_time(dt.datetime(2026, 9, 21, 16, 45))

    result = runner.invoke(app, ["intraday", "carry"] + _tradeable_overrides(tmp_path))

    assert result.exit_code == 0, result.output
    assert "Carry skipped: the close (16:00 New York) is more than 60 minutes away" in (
        result.output
    )


# -- shadow mode (Phase 11, Rollout Stage 1) -----------------------------------------------


def _shadow_tradeable_overrides(tmp_path: Path) -> list[str]:
    return _intraday_overrides(
        tmp_path,
        **{
            "intraday.trading_enabled": "true",
            "intraday.registered_account": "FAKE-PAPER-0001",
            "intraday.shadow_mode": "true",
        },
    )


class _ClockOnlyBroker:
    """The real account as shadow mode may see it: the exchange clock and nothing else.
    Any other attribute raises, so a test proves no shadow order can reach the account."""

    def __init__(self) -> None:
        self.is_open = True

    def get_clock(self) -> Clock:
        now = dt.datetime(2026, 9, 21, 15, 0)
        return Clock(timestamp=now, is_open=self.is_open, next_open=now, next_close=now)

    def __getattr__(self, name):
        raise AssertionError(f"shadow mode must only read the real account's clock, not {name}")


@pytest.fixture
def fake_intraday_shadow_env(monkeypatch):
    """Shadow mode: `open-batch`/`cycle`/`carry`/`monitor` trade against a shadow
    `FakeBroker` built from the ledger's own persisted state
    (`cli.py::_intraday_execution_broker`). The real `AlpacaPaperBroker` is read for the
    exchange clock only (added 2026-10-06), so `from_env` returns a `_ClockOnlyBroker`
    that raises on anything else -- proving that structurally, mirroring
    `fake_placebo_env`'s own assertion style for the placebo."""
    market_data = _FakeMarketData()
    news_client = _FakeNewsClient()
    real_account = _ClockOnlyBroker()

    def fake_run(argv, stdin_text, timeout_seconds, env):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=_result_json(), stderr="")

    monkeypatch.setattr(
        "semimc.execution.alpaca_broker.AlpacaPaperBroker.from_env",
        classmethod(lambda cls, **kwargs: real_account),
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
    return market_data, news_client, real_account


def test_open_batch_runs_cleanly_in_shadow_mode_without_a_real_broker(
    tmp_path, fake_intraday_shadow_env
):
    result = runner.invoke(app, ["intraday", "open-batch"] + _shadow_tradeable_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Proposals: 0 seen" in result.output


def test_cycle_runs_cleanly_in_shadow_mode_without_a_real_broker(
    tmp_path, fake_intraday_shadow_env
):
    result = runner.invoke(app, ["intraday", "cycle"] + _shadow_tradeable_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Proposals: 0 seen" in result.output


def test_carry_runs_cleanly_in_shadow_mode_without_a_real_broker(
    tmp_path, fake_intraday_shadow_env, at_carry_time
):
    result = runner.invoke(app, ["intraday", "carry"] + _shadow_tradeable_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Carried: (none)" in result.output


def test_carry_declines_on_the_real_clock_after_the_close(tmp_path, fake_intraday_shadow_env):
    """2026-10-16: GitHub's late copy of `carry` lands around 17:40 CT every day. It now
    reads the closed market, sends nothing, and does not fail the job."""
    _market_data, _news_client, real_account = fake_intraday_shadow_env
    real_account.is_open = False

    result = runner.invoke(app, ["intraday", "carry"] + _shadow_tradeable_overrides(tmp_path))

    assert result.exit_code == 0, result.output
    assert "Carry skipped: market is closed" in result.output


def test_monitor_runs_cleanly_in_shadow_mode_without_a_real_broker(
    tmp_path, fake_intraday_shadow_env
):
    result = runner.invoke(app, ["intraday", "monitor"] + _shadow_tradeable_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Closed: (none)" in result.output


def test_cycle_in_shadow_mode_declines_on_the_real_clock_after_the_close(
    tmp_path, fake_intraday_shadow_env, monkeypatch
):
    """2026-10-06: the shadow broker's own clock read open around the clock, so GitHub's
    late copy of `cycle` ran a full Trader cycle after the close every day. It now reads
    the real exchange clock, declines without a Trader call, and does not fail the job."""
    _market_data, _news_client, real_account = fake_intraday_shadow_env
    real_account.is_open = False

    def trader_must_not_be_called(argv, stdin_text, timeout_seconds, env):
        raise AssertionError("no Trader call after the close")

    monkeypatch.setattr(
        "semimc.intraday.llm.ClaudeCliClient.from_env",
        classmethod(lambda cls: ClaudeCliClient(run=trader_must_not_be_called)),
    )

    result = runner.invoke(
        app,
        ["intraday", "cycle", "--cycle", "cycle_2256"] + _shadow_tradeable_overrides(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert "Skipped: market is closed" in result.output
    with IntradayLedger(tmp_path / "intraday.sqlite3") as ledger:
        assert [(i["severity"], i["message"]) for i in ledger.incidents()] == [
            ("info", "cycle_2256 skipped: market is closed")
        ]


def test_shadow_broker_state_persists_across_two_separate_invocations(
    tmp_path, fake_intraday_shadow_env, monkeypatch
):
    """Each cron-triggered CLI call is a fresh process -- Stage 1's shadow book must
    survive that the same way the placebo's own shadow ledger already does."""
    market_data, _news_client, _real_account = fake_intraday_shadow_env
    market_data._quotes["NVDA"] = 190.0

    ledger_path = tmp_path / "intraday.sqlite3"
    with IntradayLedger(ledger_path) as ledger:
        ledger.record_watchlist_item("2026-09-21", "NVDA", thesis="test")

    def fake_run(argv, stdin_text, timeout_seconds, env):
        proposal = {
            "action": "open",
            "symbol": "NVDA",
            "side": "buy",
            "size_pct": 0.05,
            "limit_price": 190.0,
            "thesis": "AI capex",
            "invalidation_price": 185.0,
            "time_stop_minutes": 60,
        }
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_result_json(structured_output={"proposals": [proposal]}),
            stderr="",
        )

    monkeypatch.setattr(
        "semimc.intraday.llm.ClaudeCliClient.from_env",
        classmethod(lambda cls: ClaudeCliClient(run=fake_run)),
    )

    first = runner.invoke(
        app,
        ["intraday", "open-batch", "--as-of", "2026-09-21"] + _shadow_tradeable_overrides(tmp_path),
    )
    assert first.exit_code == 0, first.output
    assert "Orders submitted" in first.output

    with IntradayLedger(ledger_path) as ledger:
        state = ledger.load_shadow_broker_state()
    assert state is not None
    assert "NVDA" in state["positions"]

    second = runner.invoke(
        app,
        ["intraday", "monitor", "--as-of", "2026-09-21"] + _shadow_tradeable_overrides(tmp_path),
    )
    assert second.exit_code == 0, second.output
    assert "NVDA" in second.output


def test_eod_review_on_a_quiet_day_writes_a_report_without_a_model_call(
    tmp_path, fake_intraday_env
):
    """`intraday.reports_dir` and `paper.ledger_path` must both be redirected here --
    otherwise this command would resolve them against the real project root and either
    write into the real repo's `reports/` directory or open the real `paper.sqlite3`
    (`CLAUDE.md`'s own `signals --holdout` gotcha, the exact reason `reports_dir` is a
    `--set`-overridable config field rather than a literal in `cli.py`). An empty ledger
    has nothing to grade: the mechanical report (benchmarks, placebo, blotter) is still
    written, but the Reviewer is never called (`run_eod_review`, changed 2026-09-27)."""
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
    assert "No Reviewer narration: nothing to grade today" in result.output
    assert len(list((tmp_path / "reports").rglob("eod.md"))) == 1


def test_eod_review_grades_the_day_and_writes_a_full_report(
    tmp_path, fake_intraday_env, monkeypatch
):
    """The full happy path (Phase 9b): a seeded proposal/reflection, a fake Reviewer
    response shaped to the real output schema, and a real rendered report with the
    Reviewer's own sections filled in (not the Phase 9a placeholders)."""
    ledger_path = tmp_path / "intraday.sqlite3"
    ledger = IntradayLedger(ledger_path)
    proposal_id = ledger.record_proposal(
        "2026-09-21",
        "open_batch",
        "NVDA",
        "open",
        "accepted",
        payload={"action": "open", "symbol": "NVDA", "side": "buy", "thesis": "AI capex cycle"},
    )
    ledger.record_order(
        client_order_id="entry-1",
        symbol="NVDA",
        side="buy",
        status="filled",
        qty=10,
        notional=1000.0,
        proposal_id=proposal_id,
        leg="entry",
    )
    ledger.record_fill(
        "entry-1", qty=10, price=100.0, filled_at=dt.datetime(2026, 9, 21, 14, 35, tzinfo=dt.UTC)
    )
    reflection_id = ledger.record_reflection(
        "2026-09-21",
        "NVDA",
        {
            "thesis": "AI capex cycle",
            "confidence": 0.6,
            "side": "buy",
            "invalidation_price": 95.0,
            "close_reason": "trader_close",
            "qty": 10,
            "avg_entry_price": 100.0,
            "avg_exit_price": 110.0,
            "pnl_gross": 100.0,
            "pnl_net": 98.0,
            "invalidation_respected": True,
            "time_in_trade_minutes": 30.0,
            "category": None,
            "process_grade": None,
            "lesson": None,
        },
        client_order_id="exit-1",
    )
    ledger.close()

    def fake_run(argv, stdin_text, timeout_seconds, env):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_result_json(
                structured_output={
                    "reflection_updates": [
                        {
                            "reflection_id": reflection_id,
                            "category": "thesis_right",
                            "process_grade": 4,
                            "lesson": None,
                        }
                    ],
                    "closing_market_report": "Semis broadly higher into the close.",
                    "attribution_narrative": "Most of today's gain was sector-driven.",
                    "process_scorecard_narrative": "One trade, on-thesis, no violations.",
                    "playbook_proposal": None,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(
        "semimc.intraday.llm.ClaudeCliClient.from_env",
        classmethod(lambda cls: ClaudeCliClient(run=fake_run)),
    )

    result = runner.invoke(
        app,
        ["intraday", "eod-review", "--as-of", "2026-09-21"]
        + _intraday_overrides(
            tmp_path,
            **{
                "intraday.reports_dir": (tmp_path / "reports").as_posix(),
                "intraday.playbook_path": (tmp_path / "PLAYBOOK.md").as_posix(),
                "intraday.proposals_dir": (tmp_path / "proposals").as_posix(),
                "paper.ledger_path": (tmp_path / "paper.sqlite3").as_posix(),
            },
        ),
    )
    assert result.exit_code == 0, result.output
    assert "Wrote" in result.output
    assert "Reflections graded: 1" in result.output

    report_files = list((tmp_path / "reports").rglob("eod.md"))
    assert len(report_files) == 1
    content = report_files[0].read_text(encoding="utf-8")
    assert "Semis broadly higher into the close." in content
    assert "Most of today's gain was sector-driven." in content
    assert "One trade, on-thesis, no violations." in content
    assert "None proposed today." in content

    with IntradayLedger(ledger_path) as check_ledger:
        graded = check_ledger.reflections_for("2026-09-21")[0]["content"]
        assert graded["category"] == "thesis_right"
        assert graded["process_grade"] == 4
        # The mechanical fields Phase 9a already computed must survive the update.
        assert graded["pnl_net"] == 98.0


def test_weekly_review_writes_a_report_with_nothing_pending(tmp_path):
    """No `fake_intraday_env` needed -- `weekly-review` calls no broker, market data, or
    model (confirmed against the brief; see `run_weekly_review`'s docstring), so nothing
    needs patching to `from_env` for this command to run cleanly."""
    result = runner.invoke(
        app,
        ["intraday", "weekly-review"]
        + _intraday_overrides(
            tmp_path, **{"intraday.reports_dir": (tmp_path / "reports").as_posix()}
        ),
    )
    assert result.exit_code == 0, result.output
    assert "Wrote" in result.output
    assert "Pending playbook proposals: 0" in result.output

    report_files = list((tmp_path / "reports").rglob("weekly_review.md"))
    assert len(report_files) == 1
    assert "None pending." in report_files[0].read_text(encoding="utf-8")


# -- playbook: human accept/reject/retire actions (Phase 10) -------------------------------


def _playbook_overrides(tmp_path: Path, **extra) -> list[str]:
    return _intraday_overrides(
        tmp_path,
        **{
            "intraday.playbook_path": (tmp_path / "PLAYBOOK.md").as_posix(),
            "intraday.proposals_dir": (tmp_path / "proposals").as_posix(),
            **extra,
        },
    )


def _write_proposal(tmp_path: Path, date: str, text: str) -> None:
    proposals_dir = tmp_path / "proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    (proposals_dir / f"{date}.md").write_text(text, encoding="utf-8")


def test_playbook_pending_lists_an_unresolved_proposal(tmp_path):
    _write_proposal(tmp_path, "2026-09-21", "Raise the spread gate.")
    result = runner.invoke(app, ["intraday", "playbook", "pending"] + _playbook_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "2026-09-21.md" in result.output


def test_playbook_pending_reports_none_with_nothing_outstanding(tmp_path):
    result = runner.invoke(app, ["intraday", "playbook", "pending"] + _playbook_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "No pending proposals." in result.output


def test_playbook_accept_fails_without_meeting_the_evidence_bar(tmp_path):
    _write_proposal(tmp_path, "2026-09-21", "Raise the spread gate.")
    result = runner.invoke(
        app,
        ["intraday", "playbook", "accept", "--date", "2026-09-21", "--evidence-trading-days", "2"]
        + _playbook_overrides(tmp_path),
    )
    assert result.exit_code != 0
    assert "Not accepted" in result.output
    assert not (tmp_path / "PLAYBOOK.md").exists()


def test_playbook_accept_with_bug_fix_writes_a_new_playbook_version(tmp_path):
    _write_proposal(tmp_path, "2026-09-21", "Fix a bug in the sizing formula.")
    result = runner.invoke(
        app,
        ["intraday", "playbook", "accept", "--date", "2026-09-21", "--bug-fix"]
        + _playbook_overrides(tmp_path),
    )
    assert result.exit_code == 0, result.output
    assert "version 1" in result.output
    assert "Fix a bug in the sizing formula." in (tmp_path / "PLAYBOOK.md").read_text(
        encoding="utf-8"
    )


def test_playbook_reject_records_the_decision_without_touching_playbook_md(tmp_path):
    _write_proposal(tmp_path, "2026-09-21", "Raise the spread gate.")
    result = runner.invoke(
        app,
        [
            "intraday",
            "playbook",
            "reject",
            "--date",
            "2026-09-21",
            "--reason",
            "not enough evidence",
        ]
        + _playbook_overrides(tmp_path),
    )
    assert result.exit_code == 0, result.output
    assert "Rejected" in result.output
    assert not (tmp_path / "PLAYBOOK.md").exists()


def test_playbook_retire_removes_an_active_entry(tmp_path):
    _write_proposal(tmp_path, "2026-09-21", "Never chase a gap over 3% at the open.")
    accept_result = runner.invoke(
        app,
        ["intraday", "playbook", "accept", "--date", "2026-09-21", "--bug-fix"]
        + _playbook_overrides(tmp_path),
    )
    assert accept_result.exit_code == 0, accept_result.output
    with IntradayLedger(tmp_path / "intraday.sqlite3") as ledger:
        entry_id = ledger.active_playbook_entries()[0]["id"]

    result = runner.invoke(
        app,
        [
            "intraday",
            "playbook",
            "retire",
            "--entry-id",
            str(entry_id),
            "--reason",
            "stopped holding",
        ]
        + _playbook_overrides(tmp_path),
    )
    assert result.exit_code == 0, result.output
    assert "version 2" in result.output
    assert "Never chase" not in (tmp_path / "PLAYBOOK.md").read_text(encoding="utf-8")


def test_playbook_retire_fails_for_an_unknown_entry(tmp_path):
    result = runner.invoke(
        app,
        ["intraday", "playbook", "retire", "--entry-id", "999", "--reason", "stopped holding"]
        + _playbook_overrides(tmp_path),
    )
    assert result.exit_code != 0
    assert "Not retired" in result.output


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


def test_placebo_carry_runs_cleanly_with_no_positions(tmp_path, fake_placebo_env, at_carry_time):
    result = runner.invoke(app, ["intraday", "placebo", "carry"] + _intraday_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Carried: (none)" in result.output


def test_placebo_carry_dispatched_mid_session_does_nothing(
    tmp_path, fake_placebo_env, at_carry_time
):
    at_carry_time(dt.datetime(2026, 9, 21, 16, 45))

    result = runner.invoke(app, ["intraday", "placebo", "carry"] + _intraday_overrides(tmp_path))

    assert result.exit_code == 0, result.output
    assert "Carry skipped: the close (16:00 New York)" in result.output


def test_intraday_now_is_naive_utc_the_convention_every_timezone_boundary_assumes():
    # Found 2026-09-27: `_intraday_now` returned naive *Central* wall-clock time, but
    # `AlpacaMarketDataClient._nearest_quote`, `check_spread_gate`, the monitor's time-stop
    # and `reflect.py` all read a naive datetime as UTC. Every quote lookup during Stage 1
    # therefore asked IEX for a window five hours in the past (pre-market), got nothing,
    # and logged "no quote available" -- zero proposals across every shadow day.
    from semimc.cli import _intraday_now

    now = _intraday_now()
    assert now.tzinfo is None
    utc_now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    assert abs((utc_now - now).total_seconds()) < 60


def test_readiness_writes_a_report_from_the_ledger_and_never_creates_a_placebo_ledger(tmp_path):
    """Like `weekly-review`, no fake env needed: `readiness` reads ledgers only. It must
    not open (and so create) a missing placebo ledger -- an empty one would read as "the
    placebo ran and closed nothing" rather than "no placebo data"."""
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    ledger.record_brief(pd.Timestamp("2026-09-21"), {"summary": "brief"})
    ledger.close()

    result = runner.invoke(
        app,
        ["intraday", "readiness", "--as-of", "2026-09-22"]
        + _intraday_overrides(
            tmp_path, **{"intraday.reports_dir": (tmp_path / "reports").as_posix()}
        ),
    )
    assert result.exit_code == 0, result.output
    assert "Evidence checks passed:" in result.output

    report = tmp_path / "reports" / "2026-09-22" / "stage_readiness.md"
    text = report.read_text(encoding="utf-8")
    # --since defaults to the ledger's first day of activity.
    assert "# S4 stage readiness: 2026-09-21 to 2026-09-22" in text
    assert "no placebo ledger" in text
    assert not (tmp_path / "intraday_shadow.sqlite3").exists()

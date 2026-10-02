"""`intraday/jobs.py`: the scheduled loop (S4, `BRIEF_INTRADAY.md`, Phases 5-6) --
`preflight`/`research`/`preopen`/`open_batch`/`cycle` (`run_cycle`, shared)/`carry`. Every
LLM call is a fake `ClaudeCliClient(run=...)` (no subprocess, no network) and every broker
call is `FakeBroker` -- same boundary-mocking convention as every other intraday test file.
`run_research`'s tests use the `market` fixture (`tests/conftest.py`) for its own-data
inputs (OHLC/earnings) -- the real, fixture-backed `MarketData`, not a fake, since building
one correctly is exactly what those tests should exercise.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pytest
from fixtures.injection_corpus import INJECTION_CORPUS

from semimc.execution.fake_broker import FakeBroker
from semimc.execution.market_data import OptionMarketQuote
from semimc.intraday.jobs import (
    current_prices,
    mark_broker_to_market,
    run_carry,
    run_cycle,
    run_eod_review,
    run_preflight,
    run_preopen,
    run_research,
    run_weekly_review,
)
from semimc.intraday.ledger import IntradayLedger
from semimc.intraday.llm import ClaudeCliClient
from semimc.intraday.orders import build_entry_order, submit_order_with_fault_isolation
from semimc.intraday.proposals import TradeProposal
from semimc.live.ledger import Ledger as PaperLedger

AS_OF = pd.Timestamp("2026-09-21")
NOW = dt.datetime(2026, 9, 21, 8, 35)
ACCOUNT = "FAKE-PAPER-0001"


# -- shared setup helpers -----------------------------------------------------------------


def _tradeable_config(config, tmp_path, **risk_overrides):
    """`shadow_mode: False` (Phase 11's default is `True`) -- every existing test in this
    file exercises the *real*-broker pretrade path (a `FakeBroker` standing in for
    `AlpacaPaperBroker`, matched by account identity), not Stage 1's shadow path. Shadow
    mode gets its own, explicitly-labeled tests instead of silently changing what every
    other test here already verifies."""
    intraday = config.intraday.model_copy(
        update={
            "trading_enabled": True,
            "registered_account": ACCOUNT,
            "shadow_mode": False,
            "kill_switch_path": str(tmp_path / "KILL_SWITCH"),
            **(
                {"risk": config.intraday.risk.model_copy(update=risk_overrides)}
                if risk_overrides
                else {}
            ),
        }
    )
    return config.model_copy(update={"intraday": intraday})


def _ledger(tmp_path) -> IntradayLedger:
    return IntradayLedger(tmp_path / "intraday.sqlite3")


@dataclass
class _FakeQuote:
    bid_price: float
    ask_price: float
    timestamp: dt.datetime


@dataclass
class _FakeMarketData:
    quotes: dict[str, _FakeQuote] = field(default_factory=dict)
    raises: bool = False
    # symbol -> (price, timestamp), what `AlpacaMarketDataClient.latest_trades` returns.
    trades: dict[str, tuple[float, dt.datetime]] = field(default_factory=dict)
    # symbol -> bar objects, what `AlpacaMarketDataClient.bars` returns.
    bars_by_symbol: dict[str, list] = field(default_factory=dict)
    bars_raise: bool = False
    bar_requests: list[dict] = field(default_factory=list)
    # What `AlpacaMarketDataClient.latest_quotes` returns for the spread gate's feed
    # (delayed SIP by default). `None` means "the same quotes as IEX", so a test that
    # doesn't care about the two feeds differing sees one consistent market.
    spread_quotes: dict[str, _FakeQuote] | None = None
    spread_quotes_raise: bool = False
    spread_feeds_requested: list[str] = field(default_factory=list)

    def latest_quote(self, symbol: str, at: dt.datetime):
        if self.raises:
            raise RuntimeError("data feed unreachable")
        return self.quotes.get(symbol)

    def latest_quotes(self, symbols, feed="iex"):
        self.spread_feeds_requested.append(str(feed))
        if self.spread_quotes_raise:
            raise RuntimeError("subscription does not permit querying recent SIP data")
        source = self.quotes if self.spread_quotes is None else self.spread_quotes
        return {s: source[s] for s in symbols if s in source}

    def latest_trades(self, symbols):
        return {s: self.trades[s] for s in symbols if s in self.trades}

    def bars(self, symbols, start, end, minutes=5):
        self.bar_requests.append({"symbols": list(symbols), "start": start, "end": end})
        if self.bars_raise:
            raise RuntimeError("bars endpoint unreachable")
        return {s: self.bars_by_symbol[s] for s in symbols if s in self.bars_by_symbol}


def _quote(price: float, spread: float = 0.05, at: dt.datetime = NOW) -> _FakeQuote:
    return _FakeQuote(bid_price=price - spread / 2, ask_price=price + spread / 2, timestamp=at)


@dataclass
class _FakeOptionMarketData:
    """`AlpacaOptionsMarketDataClient`'s test double (Phase 12b) -- keyed by underlying
    symbol only, matching `_FakeMarketData`'s own simplicity: no test in this file needs
    two different option proposals on the same underlying in the same cycle."""

    quotes: dict[str, OptionMarketQuote] = field(default_factory=dict)

    def quote_contract(self, underlying_symbol, expiry, strike, right, as_of):
        return self.quotes.get(underlying_symbol)


def _with_shadow_pricing(cfg, **option_overrides):
    """Turns on Stage 4 (`IntradayOptionsConfig.shadow_pricing`), optionally overriding
    other options limits -- layered the same way `_tradeable_config`'s own
    `risk_overrides` are."""
    options = cfg.intraday.options.model_copy(update={"shadow_pricing": True, **option_overrides})
    return cfg.model_copy(update={"intraday": cfg.intraday.model_copy(update={"options": options})})


def _option_proposal(**overrides) -> dict:
    payload = {
        "action": "open",
        "instrument": "option",
        "symbol": "NVDA",
        "side": "buy",
        "size_pct": 0.005,
        "limit_price": 10.0,
        "thesis": "AI capex",
        "invalidation_price": 8.0,
        "time_stop_minutes": 60,
        "expiry": "2026-10-21",
        "strike": 190.0,
        "right": "call",
        "structure": "long_call",
    }
    payload.update(overrides)
    return payload


@dataclass
class _FakeArticle:
    id: str
    headline: str
    symbols: list[str] = field(default_factory=list)
    summary: str = ""
    created_at: dt.datetime = NOW
    source: str = "benzinga"


@dataclass
class _FakeNewsClient:
    articles: list[_FakeArticle] = field(default_factory=list)
    raises: bool = False

    def get_news(self, symbols, start, end, limit=50):
        if self.raises:
            raise RuntimeError("news feed unreachable")
        wanted = set(symbols)
        return [a for a in self.articles if wanted & set(a.symbols)]


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


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


def _sequenced_llm_client(*stdouts: str) -> ClaudeCliClient:
    """A trader response per call, in order -- the last one repeats if `run_cycle` is
    called more times than responses were supplied."""
    remaining = list(stdouts)

    def run(argv, stdin_text, timeout_seconds):
        stdout = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return _completed(stdout)

    return ClaudeCliClient(run=run)


def _recording_llm_client(stdout: str) -> tuple[list[dict], ClaudeCliClient]:
    """Records every call's stdin (the context payload) -- what the headline-wiring test
    needs to confirm the Trader's context actually carried `recent_headlines_for`'s
    output, not just that `run_cycle` didn't crash."""
    calls: list[dict] = []

    def run(argv, stdin_text, timeout_seconds):
        calls.append({"argv": argv, "stdin": stdin_text})
        return _completed(stdout)

    return calls, ClaudeCliClient(run=run)


def _seed_open_position(
    broker: FakeBroker,
    ledger: IntradayLedger,
    cfg,
    symbol: str = "NVDA",
    side: str = "buy",
    price: float = 190.0,
    invalidation_price: float = 185.0,
    time_stop_minutes: int = 120,
    horizon: str | None = "intraday",
    confidence: float | None = 0.6,
) -> int:
    """A real, filled entry (via the same `build_entry_order` ->
    `submit_order_with_fault_isolation` pipeline `run_cycle` itself uses), so its OTO
    stop leg and ledger trail are exactly what a real accepted `open` would produce."""
    proposal = TradeProposal(
        action="open",
        symbol=symbol,
        side=side,
        size_pct=0.05,
        limit_price=price,
        thesis="seed",
        invalidation_price=invalidation_price,
        time_stop_minutes=time_stop_minutes,
        horizon=horizon,
        confidence=confidence,
    )
    proposal_id = ledger.record_proposal(
        AS_OF, "seed", symbol, "open", "accepted", payload=proposal.model_dump()
    )
    request = build_entry_order(proposal, AS_OF, "seed", price, 100_000.0, cfg.intraday)
    submit_order_with_fault_isolation(
        request,
        broker,
        ledger,
        NOW,
        proposal_id=proposal_id,
        tranche_index=1,
        tranche_total=1,
        leg="entry",
    )
    return proposal_id


# -- mark_broker_to_market (promoted from placebo.py, Phase 11) -------------------------


def test_mark_broker_to_market_starting_equity_with_no_history_or_positions(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    mark_broker_to_market(broker, ledger, _FakeMarketData(), starting_equity=100_000.0, now=NOW)
    assert broker.cash == pytest.approx(100_000.0)
    assert broker.equity == pytest.approx(100_000.0)
    ledger.close()


def test_mark_broker_to_market_reflects_realized_fills_and_live_marks(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_order("s4-nvda-1", "NVDA", "buy", status="filled", leg="entry")
    ledger.record_fill("s4-nvda-1", qty=10, price=100.0, filled_at=NOW)
    broker = FakeBroker()  # no position on the broker itself -- only the ledger has the fill
    market_data = _FakeMarketData(quotes={"NVDA": _quote(110.0)})

    # cash = 100_000 - 10*100 = 99_000; no live position on the broker itself here, so
    # equity == cash (this test only checks the cash-flow half; the next test adds a
    # position via a real fill so both halves are exercised together).
    mark_broker_to_market(broker, ledger, market_data, starting_equity=100_000.0, now=NOW)
    assert broker.cash == pytest.approx(99_000.0)
    assert broker.equity == pytest.approx(99_000.0)
    ledger.close()


def test_mark_broker_to_market_values_a_held_position_at_the_live_quote(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(broker, ledger, cfg, price=190.0)
    held_qty = broker.list_positions()[0].qty
    market_data = _FakeMarketData(quotes={"NVDA": _quote(200.0)})

    mark_broker_to_market(broker, ledger, market_data, starting_equity=100_000.0, now=NOW)

    cash_flow = ledger.total_realized_cash_flow()
    assert broker.cash == pytest.approx(100_000.0 + cash_flow)
    assert broker.equity == pytest.approx(broker.cash + held_qty * 200.0)
    ledger.close()


def test_mark_broker_to_market_falls_back_to_avg_entry_price_on_a_missing_quote(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(broker, ledger, cfg, price=190.0)
    position = broker.list_positions()[0]

    mark_broker_to_market(broker, ledger, _FakeMarketData(), starting_equity=100_000.0, now=NOW)

    cash_flow = ledger.total_realized_cash_flow()
    assert broker.equity == pytest.approx(broker.cash + position.qty * position.avg_entry_price)
    assert broker.cash == pytest.approx(100_000.0 + cash_flow)
    ledger.close()


# -- run_preflight --------------------------------------------------------------------------


def test_run_preflight_passes_when_everything_is_clear(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    market_data = _FakeMarketData(quotes={"SPY": _quote(500.0)})
    result = run_preflight(cfg, FakeBroker(), market_data, _FakeNewsClient(), ledger, NOW)
    assert result.passed
    assert result.failures == []
    ledger.close()


def test_run_preflight_passes_with_routine_notes_when_not_yet_enabled(config, tmp_path):
    """`trading_enabled=False` and a closed market are both true every single morning
    before S4's own rollout -- neither should fail the job or fire a GitHub failure
    email, unlike a genuine problem (kill switch, a dead feed)."""
    ledger = _ledger(tmp_path)  # config.intraday.trading_enabled defaults to False
    broker = FakeBroker(market_open=False)
    market_data = _FakeMarketData(quotes={"SPY": _quote(500.0)})
    result = run_preflight(config, broker, market_data, _FakeNewsClient(), ledger, NOW)
    assert result.passed
    assert result.failures == []
    assert "intraday trading is not trading_enabled" in result.notes
    assert "market is closed" in result.notes
    incidents = ledger.incidents()
    assert any(i["severity"] == "warning" and "trading_enabled" in i["message"] for i in incidents)
    assert not any(i["severity"] == "error" for i in incidents)
    ledger.close()


def test_run_preflight_fails_on_the_kill_switch(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    (tmp_path / "KILL_SWITCH").write_text("stop", encoding="utf-8")
    ledger = _ledger(tmp_path)
    result = run_preflight(cfg, FakeBroker(), _FakeMarketData(), _FakeNewsClient(), ledger, NOW)
    assert not result.passed
    assert any("kill switch" in f for f in result.failures)
    assert any("kill switch" in i["message"] for i in ledger.incidents())
    ledger.close()


def test_run_preflight_fails_when_market_data_is_unreachable(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    result = run_preflight(
        cfg, FakeBroker(), _FakeMarketData(raises=True), _FakeNewsClient(), ledger, NOW
    )
    assert not result.passed
    assert any("market data unreachable" in f for f in result.failures)
    ledger.close()


def test_run_preflight_fails_when_the_news_feed_is_unreachable(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    market_data = _FakeMarketData(quotes={"SPY": _quote(500.0)})
    result = run_preflight(
        cfg, FakeBroker(), market_data, _FakeNewsClient(raises=True), ledger, NOW
    )
    assert not result.passed
    assert any("news feed unreachable" in f for f in result.failures)
    ledger.close()


def test_run_preflight_flags_a_bad_reconciliation(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_reconciliation(AS_OF, status="mismatch", discrepancies=[{"symbol": "NVDA"}])
    result = run_preflight(
        cfg,
        FakeBroker(),
        _FakeMarketData(quotes={"SPY": _quote(500.0)}),
        _FakeNewsClient(),
        ledger,
        NOW,
    )
    assert not result.passed
    assert any("reconciliation" in f for f in result.failures)
    ledger.close()


def test_run_preflight_reports_no_playbook_version_before_any_acceptance(config, tmp_path):
    ledger = _ledger(tmp_path)
    result = run_preflight(
        config,
        FakeBroker(),
        _FakeMarketData(quotes={"SPY": _quote(500.0)}),
        _FakeNewsClient(),
        ledger,
        NOW,
    )
    assert result.playbook_version is None
    ledger.close()


def test_run_preflight_reports_the_current_playbook_version(config, tmp_path):
    """(Phase 10) `PLAYBOOK.md`'s own version, informational only -- never logged as a
    `"warning"` incident the way `notes` are, since it's routine, not a warning."""
    ledger = _ledger(tmp_path)
    entry_id = ledger.record_playbook_decision(
        "active", "rule A", "proposals/2026-09-01.md", adopted_at=AS_OF, review_date=AS_OF
    )
    ledger.record_playbook_version("# PLAYBOOK.md\n\nrule A", "accepted", entry_id)

    result = run_preflight(
        config,
        FakeBroker(),
        _FakeMarketData(quotes={"SPY": _quote(500.0)}),
        _FakeNewsClient(),
        ledger,
        NOW,
    )

    assert result.playbook_version == 1
    assert not any("playbook version" in i["message"] for i in ledger.incidents())
    ledger.close()


def test_run_preflight_keeps_an_account_mismatch_routine_under_shadow_mode(config, tmp_path):
    """(Phase 11) Once Stage 1 flips `trading_enabled=True`, an account mismatch would
    normally become a real failure -- but preflight's own broker is always the real one
    (unlike `open-batch`/`cycle`/`carry`/`monitor`), so while `shadow_mode=True` this
    finding must stay routine: no real order was ever going to be placed against it."""
    intraday = config.intraday.model_copy(
        update={
            "trading_enabled": True,
            "registered_account": "SOME-REAL-ACCOUNT-NOT-THIS-ONE",
            "shadow_mode": True,
        }
    )
    cfg = config.model_copy(update={"intraday": intraday})
    ledger = _ledger(tmp_path)

    result = run_preflight(
        cfg,
        FakeBroker(),
        _FakeMarketData(quotes={"SPY": _quote(500.0)}),
        _FakeNewsClient(),
        ledger,
        NOW,
    )

    assert result.passed
    assert result.shadow_mode is True
    assert any("registered_account" in n for n in result.notes)
    ledger.close()


def test_run_preflight_still_fails_the_kill_switch_under_shadow_mode(config, tmp_path):
    """The one finding `shadow_mode` must never make routine -- an emergency halt stops
    everything, shadow included."""
    intraday = config.intraday.model_copy(
        update={
            "trading_enabled": True,
            "registered_account": "FAKE-PAPER-0001",
            "shadow_mode": True,
            "kill_switch_path": str(tmp_path / "KILL_SWITCH"),
        }
    )
    cfg = config.model_copy(update={"intraday": intraday})
    (tmp_path / "KILL_SWITCH").write_text("stop", encoding="utf-8")
    ledger = _ledger(tmp_path)

    result = run_preflight(
        cfg,
        FakeBroker(),
        _FakeMarketData(quotes={"SPY": _quote(500.0)}),
        _FakeNewsClient(),
        ledger,
        NOW,
    )

    assert not result.passed
    assert any("kill switch" in f for f in result.failures)
    ledger.close()


# -- run_preopen ----------------------------------------------------------------------------


def test_run_preopen_keeps_a_tight_fresh_quote(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    result = run_preopen(cfg, market_data, _FakeNewsClient(), ledger, AS_OF, NOW)
    assert result.tradable == ["NVDA"]
    assert result.dropped == []
    ledger.close()


def test_run_preopen_drops_a_wide_spread_candidate(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, max_spread_bps=10.0)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0, spread=2.0)})  # ~105 bps
    result = run_preopen(cfg, market_data, _FakeNewsClient(), ledger, AS_OF, NOW)
    assert result.tradable == []
    assert result.dropped[0][0] == "NVDA"
    assert any("spread" in i["message"] for i in ledger.incidents())
    ledger.close()


# 2026-10-01's case: IEX's own book in MU quoted ~160 bps wide while the consolidated
# quote (15 minutes delayed on the free plan) was a few cents.
_IEX_WIDE = _quote(1081.0, spread=17.0)
_SIP_TIGHT = _quote(1081.0, spread=0.10, at=NOW - dt.timedelta(minutes=15))


def _preopen(cfg, ledger, market_data):
    ledger.record_watchlist_item(AS_OF, "MU", thesis="test")
    return run_preopen(cfg, market_data, _FakeNewsClient(), ledger, AS_OF, NOW)


def test_the_spread_gate_reads_the_delayed_consolidated_quote_not_iex(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    market_data = _FakeMarketData(quotes={"MU": _IEX_WIDE}, spread_quotes={"MU": _SIP_TIGHT})

    result = _preopen(cfg, ledger, market_data)

    assert result.tradable == ["MU"]  # the 15-minute-old quote is not "stale" here
    assert market_data.spread_feeds_requested == ["delayed_sip"]
    assert ledger.incidents() == []
    ledger.close()


def test_a_wide_consolidated_spread_still_drops_the_name(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    wide = _quote(1081.0, spread=17.0, at=NOW - dt.timedelta(minutes=15))
    market_data = _FakeMarketData(quotes={"MU": _quote(1081.0)}, spread_quotes={"MU": wide})

    result = _preopen(cfg, ledger, market_data)

    assert result.tradable == []
    assert "spread" in result.dropped[0][1]
    ledger.close()


def test_the_spread_gate_falls_back_to_iex_when_the_delayed_feed_fails(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    market_data = _FakeMarketData(quotes={"MU": _IEX_WIDE}, spread_quotes_raise=True)

    result = _preopen(cfg, ledger, market_data)

    assert result.tradable == []  # judged on the wide IEX quote, as before
    messages = [i["message"] for i in ledger.incidents()]
    assert any("delayed_sip quotes unavailable" in m for m in messages)
    ledger.close()


def test_a_symbol_missing_from_the_delayed_feed_is_checked_on_iex_and_noted(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    market_data = _FakeMarketData(quotes={"MU": _quote(1081.0)}, spread_quotes={})

    result = _preopen(cfg, ledger, market_data)

    assert result.tradable == ["MU"]
    assert any("no delayed_sip quote" in i["message"] for i in ledger.incidents())
    ledger.close()


def test_a_recent_trade_keeps_a_name_whose_iex_quote_has_gone_stale(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    market_data = _FakeMarketData(
        quotes={"MU": _quote(1081.0, at=NOW - dt.timedelta(minutes=2))},
        spread_quotes={"MU": _SIP_TIGHT},
        trades={"MU": (1080.9, NOW - dt.timedelta(seconds=5))},
    )

    assert _preopen(cfg, ledger, market_data).tradable == ["MU"]
    ledger.close()


def test_no_recent_trade_and_a_stale_quote_drops_the_name(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    market_data = _FakeMarketData(
        quotes={"MU": _quote(1081.0, at=NOW - dt.timedelta(minutes=2))},
        spread_quotes={"MU": _SIP_TIGHT},
    )

    result = _preopen(cfg, ledger, market_data)

    assert result.tradable == []
    assert "quote is 120s old" in result.dropped[0][1]
    ledger.close()


def test_current_prices_prefer_a_recent_iex_trade_over_the_quote_midpoint():
    market_data = _FakeMarketData(
        quotes={"MU": _IEX_WIDE, "AMD": _quote(600.0), "NVDA": _quote(190.0)},
        trades={
            "MU": (1079.0, NOW + dt.timedelta(seconds=20)),  # printed after the job's clock
            "AMD": (590.0, NOW - dt.timedelta(minutes=6)),  # too old: use the quote
        },
    )

    prices = current_prices({"MU", "AMD", "NVDA", "TSM"}, market_data, NOW)

    assert prices == {"MU": 1079.0, "AMD": 600.0, "NVDA": 190.0}  # TSM: nothing at all


def test_run_cycle_sizes_an_entry_at_the_trade_price_not_the_iex_midpoint(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, entry_tranches=1)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "MU", thesis="test")
    lopsided = _FakeQuote(bid_price=1000.0, ask_price=1100.0, timestamp=NOW)  # mid 1,050
    market_data = _FakeMarketData(
        quotes={"MU": lopsided},
        spread_quotes={"MU": _quote(1000.0, spread=0.10, at=NOW - dt.timedelta(minutes=15))},
        trades={"MU": (1000.0, NOW)},
    )
    proposal = {
        "action": "open",
        "symbol": "MU",
        "side": "buy",
        "size_pct": 0.03,  # $3,000: 3 shares at 1,000, only 2 at the 1,050 midpoint
        "limit_price": 1000.0,
        "thesis": "memory pricing",
        "invalidation_price": 990.0,
        "time_stop_minutes": 60,
    }
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    result = run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, NOW)

    assert ledger.get_order(result.orders_submitted[0])["qty"] == 3
    ledger.close()


def test_run_preopen_records_and_logs_a_new_headline(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    news_client = _FakeNewsClient(
        articles=[_FakeArticle(id="a1", headline="NVDA guidance raised", symbols=["NVDA"])]
    )

    result = run_preopen(cfg, market_data, news_client, ledger, AS_OF, NOW)

    assert result.new_headlines == [("NVDA", "NVDA guidance raised")]
    assert ledger.headline_already_seen("alpaca_news", "a1")
    assert any("new headline" in i["message"] for i in ledger.incidents())
    ledger.close()


def test_run_preopen_never_drops_a_candidate_over_a_headline(config, tmp_path):
    """`preopen` is a *deterministic* refresh (the brief's own word) -- a new headline is
    surfaced, never used here to judge whether a thesis broke. See `run_preopen`'s
    docstring."""
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    news_client = _FakeNewsClient(
        articles=[_FakeArticle(id="a1", headline="NVDA misses guidance", symbols=["NVDA"])]
    )

    result = run_preopen(cfg, market_data, news_client, ledger, AS_OF, NOW)

    assert result.tradable == ["NVDA"]
    assert result.dropped == []


def test_run_preopen_skips_an_already_seen_headline(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    ledger.record_headline_seen(AS_OF, "alpaca_news", "a1")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    news_client = _FakeNewsClient(
        articles=[_FakeArticle(id="a1", headline="NVDA guidance raised", symbols=["NVDA"])]
    )

    result = run_preopen(cfg, market_data, news_client, ledger, AS_OF, NOW)

    assert result.new_headlines == []
    ledger.close()


def test_run_preopen_redacts_a_malicious_new_headline_and_logs_an_incident(config, tmp_path):
    """Phase 8: a headline carrying an injection attempt is neutralized before it's
    persisted or logged -- `recent_headlines_for` (what the Trader's context reads later)
    must never carry the raw payload."""
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    _label, malicious_headline, _categories = INJECTION_CORPUS[0]
    news_client = _FakeNewsClient(
        articles=[_FakeArticle(id="a1", headline=malicious_headline, symbols=["NVDA"])]
    )

    result = run_preopen(cfg, market_data, news_client, ledger, AS_OF, NOW)

    assert malicious_headline not in result.new_headlines[0][1]
    assert "[redacted:" in result.new_headlines[0][1]
    stored = ledger.recent_headlines_for(AS_OF, ["NVDA"])
    assert malicious_headline not in stored[0]["headline"]
    assert any("suspected prompt injection" in i["message"] for i in ledger.incidents())
    ledger.close()


# -- run_cycle: pretrade gate ---------------------------------------------------------------


def test_run_cycle_is_skipped_when_trading_is_disabled(config, tmp_path):
    ledger = _ledger(tmp_path)  # config.intraday.trading_enabled defaults to False
    client = _sequenced_llm_client(_result_json())
    result = run_cycle(
        "cycle_1", config, FakeBroker(), ledger, _FakeMarketData(), client, AS_OF, NOW
    )
    assert result.skipped_reason is not None
    assert result.proposals_seen == 0
    ledger.close()


# -- run_cycle: shadow mode (Phase 11, Rollout Stage 1) ----------------------------------


def test_run_cycle_shadow_mode_lets_a_proposal_through_despite_an_account_mismatch(
    config, tmp_path
):
    """`shadow_mode=True` (Stage 1's own default) means `broker` is a `FakeBroker` with no
    real account to match -- `run_cycle` must not reject on that mismatch the way it
    would for a real, live account."""
    intraday = config.intraday.model_copy(
        update={
            "trading_enabled": True,
            "registered_account": "SOME-REAL-ACCOUNT-NOT-THIS-ONE",
            "shadow_mode": True,
            "kill_switch_path": str(tmp_path / "KILL_SWITCH"),
        }
    )
    cfg = config.model_copy(update={"intraday": intraday})
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
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
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))
    broker = FakeBroker()  # account_number="FAKE-PAPER-0001" -- never matches registered_account

    result = run_cycle("cycle_1", cfg, broker, ledger, market_data, client, AS_OF, NOW)

    assert result.skipped_reason is None
    assert result.proposals_accepted == 1


def test_run_cycle_non_shadow_mode_still_rejects_an_account_mismatch(config, tmp_path):
    """The exact opposite of the test above, with `shadow_mode=False` -- confirms shadow
    mode is what changed this, not some other regression."""
    intraday = config.intraday.model_copy(
        update={
            "trading_enabled": True,
            "registered_account": "SOME-REAL-ACCOUNT-NOT-THIS-ONE",
            "shadow_mode": False,
            "kill_switch_path": str(tmp_path / "KILL_SWITCH"),
        }
    )
    cfg = config.model_copy(update={"intraday": intraday})
    ledger = _ledger(tmp_path)
    client = _sequenced_llm_client(_result_json())

    result = run_cycle("cycle_1", cfg, FakeBroker(), ledger, _FakeMarketData(), client, AS_OF, NOW)

    assert result.skipped_reason is not None
    assert "registered_account" in result.skipped_reason


# -- run_cycle: opening and tranche-splitting -------------------------------------------------


def test_run_cycle_submits_only_the_first_tranche_of_an_accepted_open(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, entry_tranches=2)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
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
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    result = run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, NOW)

    assert result.proposals_accepted == 1
    assert len(result.orders_submitted) == 1
    pending = ledger.pending_tranche_for("NVDA")
    assert pending is not None
    assert pending["tranche_index"] == 1
    assert pending["tranche_total"] == 2
    ledger.close()


def test_run_cycle_stamps_the_current_playbook_version_onto_every_recorded_proposal(
    config, tmp_path
):
    """(Phase 10) Mechanical, added by `jobs.py` after the model call returns -- never
    asked of the Trader, and `TradeProposal`'s own schema stays untouched."""
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    entry_id = ledger.record_playbook_decision(
        "active", "rule A", "proposals/2026-09-01.md", adopted_at=AS_OF, review_date=AS_OF
    )
    ledger.record_playbook_version("# PLAYBOOK.md\n\nrule A", "accepted", entry_id)
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
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
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, NOW)

    recorded = ledger.proposals_for(AS_OF)
    assert recorded[-1]["playbook_version"] == 1
    ledger.close()


def test_run_cycle_stamps_no_playbook_version_before_any_acceptance(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
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
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, NOW)

    recorded = ledger.proposals_for(AS_OF)
    assert recorded[-1]["playbook_version"] is None
    ledger.close()


def test_run_cycle_submits_the_next_tranche_on_a_later_call(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, entry_tranches=2)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
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
    broker = FakeBroker(now=NOW)
    client = _sequenced_llm_client(
        _result_json(structured_output={"proposals": [proposal]}),
        _result_json(structured_output={"proposals": []}),
    )

    run_cycle("cycle_1", cfg, broker, ledger, market_data, client, AS_OF, NOW)
    result2 = run_cycle(
        "cycle_2",
        cfg,
        broker,
        ledger,
        market_data,
        client,
        AS_OF,
        NOW + dt.timedelta(minutes=20),
    )

    assert len(result2.orders_submitted) == 1
    assert ledger.pending_tranche_for("NVDA") is None
    order = ledger.get_order(result2.orders_submitted[0])
    assert order["tranche_index"] == 2
    assert order["tranche_total"] == 2
    ledger.close()


def _expensive_open(size_pct: float, price: float = 1000.0) -> dict:
    return {
        "action": "open",
        "symbol": "MU",
        "side": "buy",
        "size_pct": size_pct,
        "limit_price": price,
        "thesis": "memory pricing",
        "invalidation_price": price * 0.985,
        "time_stop_minutes": 60,
    }


def test_run_cycle_enters_a_one_share_size_whole_instead_of_in_tranches(config, tmp_path):
    """2026-10-01: 1.5% of $100k in two tranches was $750 a piece, less than one MU share,
    and the cycle failed outright. One share's worth now goes in as a single piece."""
    cfg = _tradeable_config(config, tmp_path, entry_tranches=2)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "MU", thesis="test")
    market_data = _FakeMarketData(quotes={"MU": _quote(1000.0)})
    proposal = _expensive_open(0.015)  # $1,500 -> 1 whole share
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    result = run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, NOW)

    assert result.order_failures == []
    assert len(result.orders_submitted) == 1
    order = ledger.get_order(result.orders_submitted[0])
    assert order["tranche_total"] == 1
    assert ledger.pending_tranche_for("MU") is None
    ledger.close()


def test_run_cycle_rejects_a_size_worth_no_whole_share_instead_of_failing(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, entry_tranches=2)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "MU", thesis="test")
    market_data = _FakeMarketData(quotes={"MU": _quote(1000.0)})
    proposal = _expensive_open(0.0075)  # $750 < one share
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    result = run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, NOW)

    assert result.order_failures == []
    assert result.proposals_rejected == 1
    assert "buys no whole share of MU" in ledger.proposals_for(AS_OF)[-1]["reason"]
    ledger.close()


def test_run_cycle_buys_one_share_for_a_later_tranche_after_a_price_rise(config, tmp_path):
    # 2% of $100k at $1,000 is two shares, one per tranche; at $1,010 the second piece
    # ($1,000) is worth 0.99 of a share and used to fail every cycle from then on.
    cfg = _tradeable_config(config, tmp_path, entry_tranches=2)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "MU", thesis="test")
    broker = FakeBroker(now=NOW)
    client = _sequenced_llm_client(
        _result_json(structured_output={"proposals": [_expensive_open(0.02)]}),
        _result_json(structured_output={"proposals": []}),
    )

    run_cycle(
        "cycle_1",
        cfg,
        broker,
        ledger,
        _FakeMarketData(quotes={"MU": _quote(1000.0)}),
        client,
        AS_OF,
        NOW,
    )
    later = NOW + dt.timedelta(minutes=20)
    result2 = run_cycle(
        "cycle_2",
        cfg,
        broker,
        ledger,
        _FakeMarketData(quotes={"MU": _quote(1010.0, at=later)}),
        client,
        AS_OF,
        later,
    )

    assert result2.order_failures == []
    assert len(result2.orders_submitted) == 1
    order = ledger.get_order(result2.orders_submitted[0])
    assert order["tranche_index"] == 2
    assert order["qty"] == 1
    ledger.close()


def test_run_cycle_rejects_a_proposal_over_the_new_positions_budget(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, new_positions_per_day=0)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
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
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    result = run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, NOW)

    assert result.proposals_rejected == 1
    assert result.orders_submitted == []
    rejected = [r for r in ledger.proposals_for(AS_OF) if r["status"] == "rejected"]
    assert "new_positions_per_day" in rejected[0]["reason"]
    ledger.close()


def test_run_cycle_rejects_an_oversized_proposal_even_if_the_model_looks_compromised(
    config, tmp_path
):
    """Phase 8's actual backstop, not the text-pattern scanner in `security.py`: even a
    schema-valid Trader proposal that reads as if the model had been steered by injected
    content (a thesis quoting the corpus's own override/command phrase, sized to the
    schema's maximum) is still bounded by `validate_proposal`'s `single_name_cap_pct`
    check -- "no text from a source can raise a limit... by itself" holds structurally,
    independent of whether any scanner caught the phrase that (hypothetically) produced it.
    """
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    _label, injected_phrase, _categories = INJECTION_CORPUS[0]
    proposal = {
        "action": "open",
        "symbol": "NVDA",
        "side": "buy",
        "size_pct": 0.9,
        "limit_price": 190.0,
        "thesis": injected_phrase,
        "invalidation_price": 185.0,
        "time_stop_minutes": 60,
    }
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    result = run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, NOW)

    assert result.proposals_rejected == 1
    assert result.orders_submitted == []
    rejected = [r for r in ledger.proposals_for(AS_OF) if r["status"] == "rejected"]
    assert "single_name_cap_pct" in rejected[0]["reason"]
    ledger.close()


def test_run_cycle_handles_a_malformed_trader_proposal_without_crashing(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    market_data = _FakeMarketData()
    malformed = {"action": "open"}  # missing every other required field
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [malformed]}))

    result = run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, NOW)

    assert result.proposals_seen == 1
    assert result.proposals_accepted == 0
    assert any("malformed trader proposal" in i["message"] for i in ledger.incidents())
    ledger.close()


def test_run_cycle_returns_skipped_reason_once_the_daily_budget_is_exhausted(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    budget = cfg.intraday.budget.model_copy(update={"max_cost_usd_per_day": 1.0})
    cfg = cfg.model_copy(update={"intraday": cfg.intraday.model_copy(update={"budget": budget})})
    ledger = _ledger(tmp_path)
    ledger.record_model_call(AS_OF, "cycle_0", "trader", "haiku", "hash", 1000, 1000, cost_usd=1.0)
    client = _sequenced_llm_client(_result_json())

    result = run_cycle("cycle_1", cfg, FakeBroker(), ledger, _FakeMarketData(), client, AS_OF, NOW)

    assert result.skipped_reason is not None
    assert "budget" in result.skipped_reason
    ledger.close()


# -- run_cycle: the monitor runs first --------------------------------------------------------


def test_run_cycle_closes_an_invalidated_position_before_asking_the_trader(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(broker, ledger, cfg, invalidation_price=185.0)
    market_data = _FakeMarketData(quotes={"NVDA": _quote(180.0)})  # through invalidation
    client = _sequenced_llm_client(_result_json())  # empty proposals -- shouldn't matter

    result = run_cycle("cycle_1", cfg, broker, ledger, market_data, client, AS_OF, NOW)

    assert broker.list_positions() == []
    assert any(i["message"] == "stopout:NVDA" for i in ledger.incidents())
    assert result.skipped_reason is None
    ledger.close()


# -- run_cycle: trim and close proposals -------------------------------------------------------


def test_run_cycle_close_proposal_cancels_the_resting_stop_and_exits(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(broker, ledger, cfg, invalidation_price=185.0)
    resting_stop = ledger.open_stop_leg_for("NVDA")
    assert resting_stop is not None
    market_data = _FakeMarketData(quotes={"NVDA": _quote(192.0)})
    proposal = {"action": "close", "symbol": "NVDA"}
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    result = run_cycle("cycle_1", cfg, broker, ledger, market_data, client, AS_OF, NOW)

    assert broker.list_positions() == []
    assert broker.get_order_by_client_id(resting_stop["client_order_id"]).status == "canceled"
    assert len(result.orders_submitted) == 1
    reflections = ledger.reflections_for(AS_OF)
    assert len(reflections) == 1
    assert reflections[0]["content"]["close_reason"] == "trader_close"
    ledger.close()


def test_run_cycle_trim_proposal_reprotects_the_remainder_at_the_original_invalidation(
    config, tmp_path
):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(broker, ledger, cfg, price=190.0, invalidation_price=185.0)
    held_qty = broker.list_positions()[0].qty
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    # size_pct small enough to leave a real remainder.
    proposal = {"action": "trim", "symbol": "NVDA", "size_pct": 0.01}
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    run_cycle("cycle_1", cfg, broker, ledger, market_data, client, AS_OF, NOW)

    [position] = broker.list_positions()
    assert 0 < position.qty < held_qty
    new_stop = ledger.open_stop_leg_for("NVDA")
    assert new_stop is not None
    assert new_stop["broker_order_id"] is not None
    stop_order = broker.get_order_by_client_id(new_stop["client_order_id"])
    assert stop_order.status in ("new", "accepted")
    reflections = ledger.reflections_for(AS_OF)
    assert len(reflections) == 1
    assert reflections[0]["content"]["close_reason"] == "trader_trim"
    ledger.close()


# -- run_cycle: options (Phase 12b, Rollout Stage 4) --------------------------------------


def test_run_cycle_prices_and_logs_an_option_proposal_without_ever_submitting_it(config, tmp_path):
    """The core invariant Phase 12b must get exactly right: an option proposal is priced
    and gated exactly like a real one -- accepted here, on purpose -- and yet never reaches
    `build_entry_order`/`submit_order_with_fault_isolation`. Stage 4 is "priced and
    logged... not traded"; `config.intraday.options.enabled` staying `False` (unchanged
    here) is what that phrase means in code."""
    cfg = _with_shadow_pricing(_tradeable_config(config, tmp_path))
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    options_market_data = _FakeOptionMarketData(
        quotes={
            "NVDA": OptionMarketQuote(
                occ_symbol="NVDA261021C00190000",
                bid=9.5,
                ask=10.5,
                open_interest=100,
                volume=50,
                feed="opra",
            )
        }
    )
    client = _sequenced_llm_client(
        _result_json(structured_output={"proposals": [_option_proposal()]})
    )

    result = run_cycle(
        "cycle_1",
        cfg,
        FakeBroker(now=NOW),
        ledger,
        market_data,
        client,
        AS_OF,
        NOW,
        options_market_data=options_market_data,
    )

    assert result.proposals_accepted == 1
    assert result.orders_submitted == []
    assert result.order_failures == []
    recorded = ledger.proposals_for(AS_OF)[-1]
    assert recorded["status"] == "accepted"
    assert recorded["options_pricing"]["quote"]["feed"] == "opra"
    assert recorded["options_pricing"]["pricing"]["delta"] == pytest.approx(0.536, abs=1e-3)
    ledger.close()


def test_run_cycle_records_options_pricing_even_when_the_proposal_is_rejected(config, tmp_path):
    """Stage 4's whole purpose is judging whether the feed produces sane pricing -- that
    needs the pricing recorded on a rejected proposal too, not only on one that also
    happened to clear every other gate."""
    cfg = _with_shadow_pricing(_tradeable_config(config, tmp_path))
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    options_market_data = _FakeOptionMarketData(
        quotes={
            "NVDA": OptionMarketQuote(
                occ_symbol="NVDA260925C00190000",
                bid=9.5,
                ask=10.5,
                open_interest=100,
                volume=50,
                feed="opra",
            )
        }
    )
    # 4 days to expiry -- below min_dte (7), a clean, deterministic rejection reason.
    proposal = _option_proposal(expiry="2026-09-25")
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    result = run_cycle(
        "cycle_1",
        cfg,
        FakeBroker(now=NOW),
        ledger,
        market_data,
        client,
        AS_OF,
        NOW,
        options_market_data=options_market_data,
    )

    assert result.proposals_rejected == 1
    recorded = ledger.proposals_for(AS_OF)[-1]
    assert recorded["status"] == "rejected"
    assert "min_dte" in recorded["reason"]
    assert recorded["options_pricing"] is not None
    assert recorded["options_pricing"]["quote"]["open_interest"] == 100
    ledger.close()


def test_run_cycle_option_proposal_fails_cleanly_with_no_market_data_client_supplied(
    config, tmp_path
):
    """Every existing equity-only caller (`cli.py` before Stage 4 is turned on, and every
    other test in this file) passes no `options_market_data` at all -- an
    `instrument="option"` proposal must still fail cleanly (schema-checked, then gated),
    never crash on a `None` client."""
    cfg = _with_shadow_pricing(_tradeable_config(config, tmp_path))
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    client = _sequenced_llm_client(
        _result_json(structured_output={"proposals": [_option_proposal()]})
    )

    result = run_cycle("cycle_1", cfg, FakeBroker(now=NOW), ledger, market_data, client, AS_OF, NOW)

    assert result.proposals_rejected == 1
    recorded = ledger.proposals_for(AS_OF)[-1]
    assert "no options pricing available" in recorded["reason"]
    assert recorded["options_pricing"] is None
    ledger.close()


def test_run_cycle_option_trim_does_not_touch_the_underlying_equity_position(config, tmp_path):
    """A real, non-obvious bug this phase must avoid: an option trim/close proposal shares
    `symbol` with its underlying stock, and `validate_proposal` accepts it almost for free
    (trim/close skip every options-specific gate the same way they skip every equity risk
    check). `run_cycle` must never let that fall through to the equity trim/close dispatch,
    which looks up a position by `symbol` alone with no instrument check of its own -- it
    would otherwise trim the real *equity* position instead of doing nothing."""
    cfg = _with_shadow_pricing(_tradeable_config(config, tmp_path))
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(broker, ledger, cfg, price=190.0, invalidation_price=185.0)
    held_qty = broker.list_positions()[0].qty
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    proposal = {
        "action": "trim",
        "instrument": "option",
        "symbol": "NVDA",
        "size_pct": 0.01,
        "expiry": "2026-10-21",
        "strike": 190.0,
        "right": "call",
        "structure": "long_call",
    }
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    result = run_cycle("cycle_1", cfg, broker, ledger, market_data, client, AS_OF, NOW)

    [position] = broker.list_positions()
    assert position.qty == pytest.approx(held_qty)
    assert result.orders_submitted == []
    ledger.close()


# -- run_cycle: headlines reach the Trader (Phase 6) --------------------------------------


def test_run_cycle_passes_recent_headlines_into_the_trader_context(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    ledger.record_headline_seen(
        AS_OF, "alpaca_news", "h1", symbols=["NVDA"], headline="NVDA guidance raised"
    )
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    calls, client = _recording_llm_client(_result_json())

    run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, NOW)

    payload = json.loads(calls[0]["stdin"])
    headlines = payload["sections"]["headlines"]
    assert any(h["headline"] == "NVDA guidance raised" for h in headlines)
    assert "headlines" in payload["untrusted"]
    ledger.close()


# -- run_research (Phase 6) ---------------------------------------------------------------


def test_run_research_is_skipped_when_trading_is_disabled(config, tmp_path, market):
    ledger = _ledger(tmp_path)  # config.intraday.trading_enabled defaults to False
    client = _sequenced_llm_client(_result_json())
    result = run_research(
        config,
        FakeBroker(),
        _FakeMarketData(),
        _FakeNewsClient(),
        market,
        ledger,
        client,
        AS_OF,
        "research",
        NOW,
    )
    assert result.skipped_reason is not None
    assert ledger.watchlist_for(AS_OF) == []
    ledger.close()


def test_run_research_records_a_brief_and_watchlist(config, tmp_path, market):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    news_client = _FakeNewsClient(
        articles=[_FakeArticle(id="a1", headline="NVDA rallies", symbols=["NVDA"])]
    )
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0), "AMD": _quote(150.0)})
    candidate = {"symbol": "NVDA", "thesis": "AI capex cycle"}
    client = _sequenced_llm_client(
        _result_json(structured_output={"morning_brief": "quiet morning", "watchlist": [candidate]})
    )

    result = run_research(
        cfg, FakeBroker(), market_data, news_client, market, ledger, client, AS_OF, "research", NOW
    )

    assert result.brief_recorded
    assert result.watchlist_size == 1
    assert ledger.latest_brief()["content"] == {"text": "quiet morning"}
    assert [row["symbol"] for row in ledger.watchlist_for(AS_OF)] == ["NVDA"]
    assert ledger.headline_already_seen("alpaca_news", "a1")
    ledger.close()


def test_run_research_succeeds_before_the_market_opens(config, tmp_path, market):
    """(Phase 11) A real bug found while building Stage 1's shadow mode: research runs
    at 07:30 Central, before the 08:30 open, so `check_intraday_pretrade`'s default
    market-open check would otherwise fail this every single call, live or shadow. Every
    other `run_research` test happens to use `FakeBroker()`'s default `market_open=True`,
    which is exactly why this went uncaught until now."""
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    client = _sequenced_llm_client(
        _result_json(structured_output={"morning_brief": "quiet morning", "watchlist": []})
    )

    result = run_research(
        cfg,
        FakeBroker(market_open=False),
        _FakeMarketData(),
        _FakeNewsClient(),
        market,
        ledger,
        client,
        AS_OF,
        "research",
        NOW,
    )

    assert result.brief_recorded
    assert result.skipped_reason is None
    ledger.close()


def test_run_research_redacts_a_malicious_headline_before_the_model_and_the_ledger_see_it(
    config, tmp_path, market
):
    """Phase 8: `news_sections` (what the Researcher's own context payload carries) and
    `headlines_seen` (what the Trader reads later via `recent_headlines_for`) must both
    carry the redacted text, not the injection corpus's raw payload -- and a "warning"
    incident records the attempt."""
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    _label, malicious_headline, _categories = INJECTION_CORPUS[1]
    news_client = _FakeNewsClient(
        articles=[_FakeArticle(id="a1", headline=malicious_headline, symbols=["NVDA"])]
    )
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0), "AMD": _quote(150.0)})
    calls, client = _recording_llm_client(
        _result_json(structured_output={"morning_brief": "quiet morning", "watchlist": []})
    )

    run_research(
        cfg, FakeBroker(), market_data, news_client, market, ledger, client, AS_OF, "research", NOW
    )

    sent_payload = json.loads(calls[0]["stdin"])
    sent_news = sent_payload["sections"]["news"]
    assert all(malicious_headline not in article["headline"] for article in sent_news)
    assert any("[redacted:" in article["headline"] for article in sent_news)
    stored = ledger.recent_headlines_for(AS_OF, ["NVDA"])
    assert malicious_headline not in stored[0]["headline"]
    incidents = [i["message"] for i in ledger.incidents()]
    assert any("suspected prompt injection" in m for m in incidents)
    ledger.close()


def test_run_research_is_a_no_op_on_a_repeat_call_the_same_day(config, tmp_path, market):
    """A retried GitHub Actions attempt must not re-call the model or double the
    watchlist -- the same `ON CONFLICT DO NOTHING` reasoning `record_session_open_equity`
    already uses."""
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="already here")
    client = _sequenced_llm_client(_result_json())

    result = run_research(
        cfg,
        FakeBroker(),
        _FakeMarketData(),
        _FakeNewsClient(),
        market,
        ledger,
        client,
        AS_OF,
        "research",
        NOW,
    )

    assert result.skipped_reason is not None
    assert ledger.model_calls_for(AS_OF) == []
    ledger.close()


def test_run_research_handles_a_malformed_response_without_crashing(config, tmp_path, market):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    client = _sequenced_llm_client(_result_json(structured_output={"not": "the right shape"}))

    result = run_research(
        cfg,
        FakeBroker(),
        _FakeMarketData(),
        _FakeNewsClient(),
        market,
        ledger,
        client,
        AS_OF,
        "research",
        NOW,
    )

    assert result.skipped_reason is not None
    assert any("malformed researcher response" in i["message"] for i in ledger.incidents())
    ledger.close()


def test_run_research_returns_skipped_reason_once_the_daily_budget_is_exhausted(
    config, tmp_path, market
):
    cfg = _tradeable_config(config, tmp_path)
    budget = cfg.intraday.budget.model_copy(update={"max_cost_usd_per_day": 1.0})
    cfg = cfg.model_copy(update={"intraday": cfg.intraday.model_copy(update={"budget": budget})})
    ledger = _ledger(tmp_path)
    ledger.record_model_call(
        AS_OF, "cycle_0", "researcher", "sonnet", "hash", 1000, 1000, cost_usd=1.0
    )
    client = _sequenced_llm_client(_result_json())

    result = run_research(
        cfg,
        FakeBroker(),
        _FakeMarketData(),
        _FakeNewsClient(),
        market,
        ledger,
        client,
        AS_OF,
        "research",
        NOW,
    )

    assert result.skipped_reason is not None
    assert "budget" in result.skipped_reason
    ledger.close()


# -- run_carry --------------------------------------------------------------------------------


def test_run_carry_closes_an_intraday_horizon_position(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(broker, ledger, cfg, horizon="intraday")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})

    result = run_carry(cfg, broker, ledger, market_data, AS_OF, NOW)

    assert result.closed == ["NVDA"]
    assert broker.list_positions() == []
    reflections = ledger.reflections_for(AS_OF)
    assert len(reflections) == 1
    assert reflections[0]["content"]["close_reason"] == "carry"
    ledger.close()


def test_run_carry_carries_a_multi_day_position_under_the_cap(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, overnight_gross_cap_pct=1.0)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(broker, ledger, cfg, horizon="multi-day", confidence=0.8)
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})

    result = run_carry(cfg, broker, ledger, market_data, AS_OF, NOW)

    assert result.carried == ["NVDA"]
    assert result.closed == []
    assert len(broker.list_positions()) == 1
    ledger.close()


def test_run_carry_closes_the_lowest_confidence_position_over_the_overnight_cap(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, overnight_gross_cap_pct=0.05)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(
        broker, ledger, cfg, symbol="NVDA", horizon="multi-day", confidence=0.9, price=190.0
    )
    _seed_open_position(
        broker, ledger, cfg, symbol="AMD", horizon="multi-day", confidence=0.2, price=150.0
    )
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0), "AMD": _quote(150.0)})

    result = run_carry(cfg, broker, ledger, market_data, AS_OF, NOW)

    assert "AMD" in result.closed_for_cap  # lowest confidence closed first
    remaining_symbols = {p.symbol for p in broker.list_positions()}
    assert "AMD" not in remaining_symbols


# -- run_eod_review (Phase 9b) ------------------------------------------------------------


def _eod_review_config(config, tmp_path):
    """`reports_dir`/`playbook_path`/`proposals_dir` must all be redirected to `tmp_path`
    -- the `config` fixture leaves `project_root` at the real repo (only `_setup`'s own
    `--set` handling redirects it in a CLI-level test), and these three are exactly the
    config-relative-via-`Config.path()` fields `CLAUDE.md`'s own gotcha exists for."""
    cfg = _tradeable_config(config, tmp_path)
    intraday = cfg.intraday.model_copy(
        update={
            "reports_dir": str(tmp_path / "reports"),
            "playbook_path": str(tmp_path / "PLAYBOOK.md"),
            "proposals_dir": str(tmp_path / "proposals"),
        }
    )
    return cfg.model_copy(update={"intraday": intraday})


def _reviewer_response(reflection_id: int, playbook_proposal: str | None = None) -> str:
    return _result_json(
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
            "playbook_proposal": playbook_proposal,
        }
    )


def test_run_eod_review_on_a_quiet_day_writes_the_mechanical_report_without_a_model_call(
    config, tmp_path, market
):
    # Found 2026-09-27: this used to skip the whole report on a day with no proposals and
    # no closes -- so every Stage-1 day (zero proposals, see the timezone fix) produced no
    # eod.md at all, and a legitimately quiet day would lose its placebo comparison, the
    # one day it arguably matters most. Now: the report is written, the Reviewer is not
    # called (nothing to grade, nothing worth paying for).
    cfg = _eod_review_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    paper_ledger = PaperLedger(tmp_path / "paper.sqlite3")
    calls, client = _recording_llm_client(_result_json())

    result = run_eod_review(
        cfg,
        _FakeMarketData(),
        _FakeNewsClient(),
        market,
        paper_ledger,
        None,
        ledger,
        client,
        AS_OF,
        "eod_review",
        NOW,
    )

    assert result.skipped_reason is None
    assert result.reviewer_skipped_reason == "nothing to grade today"
    assert calls == []
    assert ledger.model_calls_for(AS_OF) == []
    text = Path(result.report_path).read_text(encoding="utf-8")
    assert "No proposals today." in text
    assert "## Benchmarks for the day" in text
    assert "Placebo (T6 control)" in text
    assert "No Reviewer call today: nothing to grade today" in text
    assert "Not yet built" not in text
    paper_ledger.close()
    ledger.close()


def test_run_eod_review_is_idempotent_once_a_reflection_is_already_graded(config, tmp_path, market):
    cfg = _eod_review_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_reflection(AS_OF, "NVDA", {"category": "thesis_right", "pnl_net": 10.0})
    paper_ledger = PaperLedger(tmp_path / "paper.sqlite3")
    client = _sequenced_llm_client(_result_json())

    result = run_eod_review(
        cfg,
        _FakeMarketData(),
        _FakeNewsClient(),
        market,
        paper_ledger,
        None,
        ledger,
        client,
        AS_OF,
        "eod_review",
        NOW,
    )

    assert result.skipped_reason == f"eod_review already ran for {AS_OF}"
    assert ledger.model_calls_for(AS_OF) == []
    paper_ledger.close()
    ledger.close()


def test_run_eod_review_handles_a_malformed_response_without_crashing(config, tmp_path, market):
    cfg = _eod_review_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(broker, ledger, cfg, horizon="intraday")
    run_carry(cfg, broker, ledger, _FakeMarketData(quotes={"NVDA": _quote(190.0)}), AS_OF, NOW)
    paper_ledger = PaperLedger(tmp_path / "paper.sqlite3")
    client = _sequenced_llm_client(_result_json(structured_output={"not": "the right shape"}))

    result = run_eod_review(
        cfg,
        _FakeMarketData(),
        _FakeNewsClient(),
        market,
        paper_ledger,
        None,
        ledger,
        client,
        AS_OF,
        "eod_review",
        NOW,
    )

    assert result.skipped_reason == "malformed reviewer response"
    assert any("malformed reviewer response" in i["message"] for i in ledger.incidents())
    paper_ledger.close()
    ledger.close()


def test_run_eod_review_grades_reflections_and_writes_a_full_report(config, tmp_path, market):
    cfg = _eod_review_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(broker, ledger, cfg, horizon="intraday")
    run_carry(cfg, broker, ledger, _FakeMarketData(quotes={"NVDA": _quote(190.0)}), AS_OF, NOW)
    reflection_id = ledger.reflections_for(AS_OF)[0]["id"]
    paper_ledger = PaperLedger(tmp_path / "paper.sqlite3")
    client = _sequenced_llm_client(_reviewer_response(reflection_id))

    result = run_eod_review(
        cfg,
        _FakeMarketData(),
        _FakeNewsClient(),
        market,
        paper_ledger,
        None,
        ledger,
        client,
        AS_OF,
        "eod_review",
        NOW,
    )

    assert result.skipped_reason is None
    assert result.reflections_updated == 1
    assert result.playbook_proposal_written is False

    graded = ledger.reflections_for(AS_OF)[0]["content"]
    assert graded["category"] == "thesis_right"
    assert graded["process_grade"] == 4
    assert graded["close_reason"] == "carry"  # the mechanical field survives the update

    with open(result.report_path, encoding="utf-8") as handle:
        content = handle.read()
    assert "Semis broadly higher into the close." in content
    assert "Most of today's gain was sector-driven." in content
    assert "None proposed today." in content
    paper_ledger.close()
    ledger.close()


def test_run_eod_review_writes_a_playbook_proposal_when_the_reviewer_makes_one(
    config, tmp_path, market
):
    cfg = _eod_review_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(broker, ledger, cfg, horizon="intraday")
    run_carry(cfg, broker, ledger, _FakeMarketData(quotes={"NVDA": _quote(190.0)}), AS_OF, NOW)
    reflection_id = ledger.reflections_for(AS_OF)[0]["id"]
    paper_ledger = PaperLedger(tmp_path / "paper.sqlite3")
    proposal_text = "## Proposed change\nRaise the spread gate from 20bps to 25bps."
    client = _sequenced_llm_client(
        _reviewer_response(reflection_id, playbook_proposal=proposal_text)
    )

    result = run_eod_review(
        cfg,
        _FakeMarketData(),
        _FakeNewsClient(),
        market,
        paper_ledger,
        None,
        ledger,
        client,
        AS_OF,
        "eod_review",
        NOW,
    )

    assert result.playbook_proposal_written is True
    proposal_files = list((tmp_path / "proposals").glob("*.md"))
    assert len(proposal_files) == 1
    assert proposal_text in proposal_files[0].read_text(encoding="utf-8")
    paper_ledger.close()
    ledger.close()


# -- run_weekly_review (Phase 10) ----------------------------------------------------------


def test_run_weekly_review_writes_a_report_with_nothing_pending(config, tmp_path):
    cfg = _eod_review_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_reflection(AS_OF, "NVDA", {"pnl_gross": 10.0, "pnl_net": 8.0})

    result = run_weekly_review(cfg, ledger, AS_OF)

    assert result.pending_proposals == 0
    assert result.entries_due_for_review == 0
    with open(result.report_path, encoding="utf-8") as handle:
        content = handle.read()
    assert "None pending." in content
    assert "None due." in content
    ledger.close()


def test_run_weekly_review_surfaces_a_pending_proposal(config, tmp_path):
    cfg = _eod_review_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    proposals_dir = cfg.path(cfg.intraday.proposals_dir)
    proposals_dir.mkdir(parents=True)
    (proposals_dir / "2026-09-21.md").write_text("Raise the spread gate.", encoding="utf-8")

    result = run_weekly_review(cfg, ledger, AS_OF)

    assert result.pending_proposals == 1
    with open(result.report_path, encoding="utf-8") as handle:
        content = handle.read()
    assert "2026-09-21.md" in content
    ledger.close()


def test_run_weekly_review_surfaces_an_entry_due_for_review(config, tmp_path):
    cfg = _eod_review_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    entry_id = ledger.record_playbook_decision(
        "active",
        "rule A",
        "proposals/2026-08-01.md",
        adopted_at=pd.Timestamp("2026-08-01"),
        review_date=pd.Timestamp("2026-09-01"),  # already past AS_OF
    )
    ledger.record_playbook_version("# PLAYBOOK.md\n\nrule A", "accepted", entry_id)

    result = run_weekly_review(cfg, ledger, AS_OF)

    assert result.entries_due_for_review == 1
    with open(result.report_path, encoding="utf-8") as handle:
        content = handle.read()
    assert f"Entry {entry_id}" in content
    ledger.close()


def test_run_weekly_review_never_calls_a_model_or_a_broker(config, tmp_path):
    """Confirmed against the brief before building this: `weekly_review` names no agent
    -- this call signature takes only `config`/`ledger`/`as_of`, nothing else."""
    cfg = _eod_review_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    result = run_weekly_review(cfg, ledger, AS_OF)
    assert result.report_path is not None
    ledger.close()


# -- once-a-day guards for jobs that an external trigger and a late cron may both fire ----


def test_open_batch_runs_once_per_day_even_when_triggered_twice(config, tmp_path):
    # External scheduler on time + GitHub's cron copy hours later (added 2026-09-27): the
    # second pass must not make another Trader call or open a second batch.
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    calls, client = _recording_llm_client(_result_json(structured_output={"proposals": []}))
    broker = FakeBroker()

    first = run_cycle("open_batch", cfg, broker, ledger, market_data, client, AS_OF, NOW)
    second = run_cycle("open_batch", cfg, broker, ledger, market_data, client, AS_OF, NOW)

    assert first.skipped_reason is None
    assert second.skipped_reason == f"open_batch already ran for {AS_OF.date()}"
    assert len(calls) == 1
    ledger.close()


def test_the_open_batch_guard_leaves_ordinary_cycles_alone(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    calls, client = _recording_llm_client(_result_json(structured_output={"proposals": []}))
    broker = FakeBroker()

    run_cycle("open_batch", cfg, broker, ledger, market_data, client, AS_OF, NOW)
    later = run_cycle("cycle_1500", cfg, broker, ledger, market_data, client, AS_OF, NOW)

    assert later.skipped_reason is None
    assert len(calls) == 2
    ledger.close()


def test_eod_review_pays_for_one_reviewer_call_per_day_even_with_nothing_graded(
    config, tmp_path, market
):
    # A day with a proposal but no close: no reflection gets a category, so the old
    # graded-reflection check alone let a second trigger call the Reviewer again.
    cfg = _eod_review_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_proposal(AS_OF, "open_batch", "NVDA", "open", "rejected", {}, reason="x")
    paper_ledger = PaperLedger(tmp_path / "paper.sqlite3")
    calls, client = _recording_llm_client(_reviewer_response(reflection_id=0))

    def review():
        return run_eod_review(
            cfg,
            _FakeMarketData(),
            _FakeNewsClient(),
            market,
            paper_ledger,
            None,
            ledger,
            client,
            AS_OF,
            "eod_review",
            NOW,
        )

    first = review()
    second = review()

    assert first.skipped_reason is None
    assert second.skipped_reason == f"eod_review already ran for {AS_OF}"
    assert len(calls) == 1
    paper_ledger.close()
    ledger.close()


# -- 2026-09-30: same-day earnings, the pre-market trade fallback, the Trader's session ---


def test_upcoming_earnings_includes_a_report_due_today():
    """MU reported after the 2026-09-30 close and the Researcher was told nothing was
    coming up: the lookup used `date > today`."""
    from types import SimpleNamespace

    from semimc.data.earnings import EarningsCalendar
    from semimc.intraday.jobs import _upcoming_earnings

    events = pd.DataFrame(
        {
            "ticker": ["MU", "MU", "NVDA"],
            "date": pd.to_datetime(["2026-06-24", "2026-09-30", "2026-11-19"]),
            "timing": ["amc", "amc", "amc"],
        }
    )
    pit = SimpleNamespace(earnings=EarningsCalendar(events))
    upcoming = _upcoming_earnings(["MU", "NVDA"], pit, pd.Timestamp("2026-09-30"), 5)
    assert upcoming == {"MU": "2026-09-30"}


def test_premarket_moves_fall_back_to_a_trade_printed_today_only():
    from types import SimpleNamespace

    from semimc.intraday.jobs import premarket_moves

    now = dt.datetime(2026, 9, 30, 12, 31)  # 07:31 Central, naive UTC like _intraday_now
    pit = SimpleNamespace(
        spot=lambda tickers: {"NVDA": 100.0, "AMD": 50.0, "MU": 1000.0, "TSM": 200.0}
    )
    market_data = _FakeMarketData(
        quotes={"TSM": _quote(202.0, at=now)},
        trades={
            "NVDA": (101.0, dt.datetime(2026, 9, 30, 12, 10, tzinfo=dt.UTC)),  # 08:10 ET today
            # 20:30 ET on the 29th: the UTC date is today, the New York date is not.
            "AMD": (55.0, dt.datetime(2026, 9, 30, 0, 30, tzinfo=dt.UTC)),
            "MU": (1100.0, dt.datetime(2026, 9, 30, 12, 40, tzinfo=dt.UTC)),  # after `now`
        },
    )
    notes: dict[str, list[str]] = {}

    moves = premarket_moves(
        ["NVDA", "AMD", "MU", "TSM", "INTC"], pit, market_data, now, diagnostics=notes
    )

    assert moves == {"NVDA": pytest.approx(0.01), "TSM": pytest.approx(0.01)}
    assert notes == {"no_prior_close": ["INTC"], "from_trade": ["NVDA"], "no_price": ["AMD", "MU"]}


def test_premarket_moves_survive_a_failed_trade_lookup():
    from types import SimpleNamespace

    from semimc.intraday.jobs import premarket_moves

    class _Failing(_FakeMarketData):
        def latest_trades(self, symbols):
            raise RuntimeError("trades endpoint down")

    pit = SimpleNamespace(spot=lambda tickers: {"NVDA": 100.0})
    assert premarket_moves(["NVDA"], pit, _Failing(), NOW) == {}


def test_run_research_logs_how_many_premarket_moves_it_got(config, tmp_path, market):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    client = _sequenced_llm_client(
        _result_json(structured_output={"morning_brief": "quiet", "watchlist": []})
    )

    run_research(
        cfg,
        FakeBroker(),
        _FakeMarketData(),
        _FakeNewsClient(),
        market,
        ledger,
        client,
        AS_OF,
        "research",
        NOW,
    )

    [note] = [i for i in ledger.incidents() if i["message"].startswith("premarket moves:")]
    assert note["severity"] == "info"
    assert f"0/{len(cfg.universe.tickers)} tickers priced" in note["message"]
    assert "no price:" in note["message"]
    ledger.close()


def _session_bars(day: dt.date, closes: list[float]) -> list:
    """5-minute bars from 09:30 New York (13:30 UTC in daylight time), one per close."""
    from types import SimpleNamespace

    start = dt.datetime(day.year, day.month, day.day, 13, 30, tzinfo=dt.UTC)
    return [
        SimpleNamespace(
            timestamp=start + dt.timedelta(minutes=5 * i),
            open=c,
            high=c + 0.5,
            low=c - 0.5,
            close=c,
            volume=100.0,
            vwap=None,
        )
        for i, c in enumerate(closes)
    ]


def test_run_cycle_gives_the_trader_session_data_for_symbols_in_view(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="opening-range breakout")
    now = dt.datetime(2026, 9, 21, 14, 12)  # 10:12 New York
    market_data = _FakeMarketData(
        quotes={"NVDA": _quote(190.0, at=now)},
        bars_by_symbol={"NVDA": _session_bars(AS_OF.date(), [186, 187, 188, 187, 186, 187, 190])},
    )
    calls, client = _recording_llm_client(_result_json())

    run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, now)

    assert market_data.bar_requests[0]["symbols"] == ["NVDA"]
    session = json.loads(calls[0]["stdin"])["sections"]["session"]
    assert session["NVDA"]["open"] == 186
    assert session["NVDA"]["opening_range"]["complete"] is True
    assert session["NVDA"]["last_vs_opening_range"] == "above"
    ledger.close()


def test_run_cycle_still_runs_when_session_bars_are_unavailable(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)}, bars_raise=True)
    calls, client = _recording_llm_client(_result_json())

    run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, NOW)

    assert len(calls) == 1
    assert json.loads(calls[0]["stdin"])["sections"]["session"] == {}
    assert any(
        i["severity"] == "warning" and i["message"].startswith("session bars unavailable")
        for i in ledger.incidents()
    )
    ledger.close()

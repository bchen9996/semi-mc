"""`intraday/jobs.py`: the scheduled loop (S4, `BRIEF_INTRADAY.md`, Phase 5) --
`preflight`/`preopen`/`open_batch`/`cycle` (`run_cycle`, shared)/`carry`. Every LLM call
is a fake `ClaudeCliClient(run=...)` (no subprocess, no network) and every broker call is
`FakeBroker` -- same boundary-mocking convention as every other intraday test file.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from dataclasses import dataclass, field

import pandas as pd

from semimc.execution.fake_broker import FakeBroker
from semimc.intraday.jobs import run_carry, run_cycle, run_preflight, run_preopen
from semimc.intraday.ledger import IntradayLedger
from semimc.intraday.llm import ClaudeCliClient
from semimc.intraday.orders import build_entry_order, submit_order_with_fault_isolation
from semimc.intraday.proposals import TradeProposal

AS_OF = pd.Timestamp("2026-09-21")
NOW = dt.datetime(2026, 9, 21, 8, 35)
ACCOUNT = "FAKE-PAPER-0001"


# -- shared setup helpers -----------------------------------------------------------------


def _tradeable_config(config, tmp_path, **risk_overrides):
    intraday = config.intraday.model_copy(
        update={
            "trading_enabled": True,
            "registered_account": ACCOUNT,
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

    def latest_quote(self, symbol: str, at: dt.datetime):
        if self.raises:
            raise RuntimeError("data feed unreachable")
        return self.quotes.get(symbol)


def _quote(price: float, spread: float = 0.05, at: dt.datetime = NOW) -> _FakeQuote:
    return _FakeQuote(bid_price=price - spread / 2, ask_price=price + spread / 2, timestamp=at)


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


# -- run_preflight --------------------------------------------------------------------------


def test_run_preflight_passes_when_everything_is_clear(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    market_data = _FakeMarketData(quotes={"SPY": _quote(500.0)})
    result = run_preflight(cfg, FakeBroker(), market_data, ledger, NOW)
    assert result.passed
    assert result.failures == []
    ledger.close()


def test_run_preflight_fails_on_the_kill_switch(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    (tmp_path / "KILL_SWITCH").write_text("stop", encoding="utf-8")
    ledger = _ledger(tmp_path)
    result = run_preflight(cfg, FakeBroker(), _FakeMarketData(), ledger, NOW)
    assert not result.passed
    assert any("kill switch" in f for f in result.failures)
    assert any("kill switch" in i["message"] for i in ledger.incidents())
    ledger.close()


def test_run_preflight_fails_when_market_data_is_unreachable(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    result = run_preflight(cfg, FakeBroker(), _FakeMarketData(raises=True), ledger, NOW)
    assert not result.passed
    assert any("market data unreachable" in f for f in result.failures)
    ledger.close()


def test_run_preflight_flags_a_bad_reconciliation(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_reconciliation(AS_OF, status="mismatch", discrepancies=[{"symbol": "NVDA"}])
    result = run_preflight(
        cfg, FakeBroker(), _FakeMarketData(quotes={"SPY": _quote(500.0)}), ledger, NOW
    )
    assert not result.passed
    assert any("reconciliation" in f for f in result.failures)
    ledger.close()


# -- run_preopen ----------------------------------------------------------------------------


def test_run_preopen_keeps_a_tight_fresh_quote(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    result = run_preopen(cfg, market_data, ledger, AS_OF, NOW)
    assert result.tradable == ["NVDA"]
    assert result.dropped == []
    ledger.close()


def test_run_preopen_drops_a_wide_spread_candidate(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, max_spread_bps=10.0)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0, spread=2.0)})  # ~105 bps
    result = run_preopen(cfg, market_data, ledger, AS_OF, NOW)
    assert result.tradable == []
    assert result.dropped[0][0] == "NVDA"
    assert any("spread" in i["message"] for i in ledger.incidents())
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
    ledger.close()

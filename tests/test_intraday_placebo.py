"""`intraday/placebo.py`: T6's control group (S4, `BRIEF_INTRADAY.md`'s "Placebo agent",
`THESES.md`, Phase 7). Every risk/execution primitive is reused from Phases 3-5
(`validate_proposal`, `build_entry_order`, `check_invalidation_and_time_stops`,
`run_carry`) -- these tests focus on what's actually new: mark-to-market, random
candidate selection, and the shadow broker/ledger's own state.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field

import pandas as pd
import pytest

from semimc.execution.fake_broker import FakeBroker
from semimc.intraday.ledger import IntradayLedger
from semimc.intraday.orders import build_entry_order, submit_order_with_fault_isolation
from semimc.intraday.placebo import _mark_to_market, run_placebo_carry, run_placebo_cycle
from semimc.intraday.proposals import TradeProposal

AS_OF = pd.Timestamp("2026-09-21")
NOW = dt.datetime(2026, 9, 21, 8, 35)


def _placebo_config(config, tmp_path, **risk_overrides):
    intraday = config.intraday.model_copy(
        update={
            "kill_switch_path": str(tmp_path / "KILL_SWITCH"),
            **(
                {"risk": config.intraday.risk.model_copy(update=risk_overrides)}
                if risk_overrides
                else {}
            ),
        }
    )
    return config.model_copy(update={"intraday": intraday})


def _ledger(tmp_path, name: str) -> IntradayLedger:
    return IntradayLedger(tmp_path / name)


@dataclass
class _FakeQuote:
    bid_price: float
    ask_price: float
    timestamp: dt.datetime


@dataclass
class _FakeMarketData:
    quotes: dict[str, _FakeQuote] = field(default_factory=dict)

    def latest_quote(self, symbol: str, at: dt.datetime):
        return self.quotes.get(symbol)


def _quote(price: float, spread: float = 0.05, at: dt.datetime = NOW) -> _FakeQuote:
    return _FakeQuote(bid_price=price - spread / 2, ask_price=price + spread / 2, timestamp=at)


def _seed_open_shadow_position(
    shadow_broker: FakeBroker,
    shadow_ledger: IntradayLedger,
    cfg,
    symbol: str = "NVDA",
    price: float = 190.0,
    invalidation_price: float = 185.0,
) -> None:
    proposal = TradeProposal(
        action="open",
        symbol=symbol,
        side="buy",
        size_pct=0.05,
        limit_price=price,
        thesis="seed",
        invalidation_price=invalidation_price,
        time_stop_minutes=120,
    )
    proposal_id = shadow_ledger.record_proposal(
        AS_OF, "seed", symbol, "open", "accepted", payload=proposal.model_dump()
    )
    request = build_entry_order(proposal, AS_OF, "seed", price, 100_000.0, cfg.intraday)
    submit_order_with_fault_isolation(
        request, shadow_broker, shadow_ledger, NOW, proposal_id=proposal_id, leg="entry"
    )


# -- _mark_to_market ------------------------------------------------------------------------


def test_mark_to_market_starting_equity_with_no_history_or_positions(tmp_path):
    shadow_ledger = _ledger(tmp_path, "shadow.sqlite3")
    broker = FakeBroker()
    _mark_to_market(broker, shadow_ledger, _FakeMarketData(), starting_equity=100_000.0, now=NOW)
    assert broker.cash == pytest.approx(100_000.0)
    assert broker.equity == pytest.approx(100_000.0)
    shadow_ledger.close()


def test_mark_to_market_reflects_realized_fills_and_live_marks(tmp_path):
    shadow_ledger = _ledger(tmp_path, "shadow.sqlite3")
    shadow_ledger.record_order("s4-nvda-1", "NVDA", "buy", status="filled", leg="entry")
    shadow_ledger.record_fill("s4-nvda-1", qty=10, price=100.0, filled_at=NOW)
    broker = FakeBroker()  # no position on the broker itself -- only the ledger has the fill
    market_data = _FakeMarketData(quotes={"NVDA": _quote(110.0)})

    # cash = 100_000 - 10*100 = 99_000; no live position on the broker itself here, so
    # equity == cash (this test only checks the cash-flow half; the next test adds a
    # position via a real fill so both halves are exercised together).
    _mark_to_market(broker, shadow_ledger, market_data, starting_equity=100_000.0, now=NOW)
    assert broker.cash == pytest.approx(99_000.0)
    assert broker.equity == pytest.approx(99_000.0)
    shadow_ledger.close()


def test_mark_to_market_values_a_held_position_at_the_live_quote(tmp_path, config):
    shadow_ledger = _ledger(tmp_path, "shadow.sqlite3")
    cfg = _placebo_config(config, tmp_path)
    broker = FakeBroker()
    _seed_open_shadow_position(broker, shadow_ledger, cfg, price=190.0)
    held_qty = broker.list_positions()[0].qty
    market_data = _FakeMarketData(quotes={"NVDA": _quote(200.0)})

    _mark_to_market(broker, shadow_ledger, market_data, starting_equity=100_000.0, now=NOW)

    cash_flow = shadow_ledger.total_realized_cash_flow()
    assert broker.cash == pytest.approx(100_000.0 + cash_flow)
    assert broker.equity == pytest.approx(broker.cash + held_qty * 200.0)
    shadow_ledger.close()


def test_mark_to_market_falls_back_to_avg_entry_price_on_a_missing_quote(tmp_path, config):
    shadow_ledger = _ledger(tmp_path, "shadow.sqlite3")
    cfg = _placebo_config(config, tmp_path)
    broker = FakeBroker()
    _seed_open_shadow_position(broker, shadow_ledger, cfg, price=190.0)
    position = broker.list_positions()[0]

    _mark_to_market(broker, shadow_ledger, _FakeMarketData(), starting_equity=100_000.0, now=NOW)

    cash_flow = shadow_ledger.total_realized_cash_flow()
    assert broker.equity == pytest.approx(broker.cash + position.qty * position.avg_entry_price)
    assert broker.cash == pytest.approx(100_000.0 + cash_flow)
    shadow_ledger.close()


# -- run_placebo_cycle ------------------------------------------------------------------


def test_run_placebo_cycle_halts_on_the_kill_switch(tmp_path, config):
    cfg = _placebo_config(config, tmp_path)
    (tmp_path / "KILL_SWITCH").write_text("stop", encoding="utf-8")
    real_ledger = _ledger(tmp_path, "real.sqlite3")
    shadow_ledger = _ledger(tmp_path, "shadow.sqlite3")
    broker = FakeBroker()

    result = run_placebo_cycle(
        "cycle_1",
        cfg,
        broker,
        shadow_ledger,
        real_ledger,
        _FakeMarketData(),
        AS_OF,
        NOW,
        random.Random(0),
    )
    assert result.skipped_reason is not None
    assert "kill switch" in result.skipped_reason
    real_ledger.close()
    shadow_ledger.close()


def test_run_placebo_cycle_is_a_no_op_with_an_empty_watchlist(tmp_path, config):
    cfg = _placebo_config(config, tmp_path)
    real_ledger = _ledger(tmp_path, "real.sqlite3")
    shadow_ledger = _ledger(tmp_path, "shadow.sqlite3")
    broker = FakeBroker()

    result = run_placebo_cycle(
        "cycle_1",
        cfg,
        broker,
        shadow_ledger,
        real_ledger,
        _FakeMarketData(),
        AS_OF,
        NOW,
        random.Random(0),
    )
    assert result.skipped_reason == "no eligible watchlist candidates"
    real_ledger.close()
    shadow_ledger.close()


def test_run_placebo_cycle_excludes_an_already_held_symbol(tmp_path, config):
    cfg = _placebo_config(config, tmp_path)
    real_ledger = _ledger(tmp_path, "real.sqlite3")
    shadow_ledger = _ledger(tmp_path, "shadow.sqlite3")
    real_ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    broker = FakeBroker(now=NOW)  # matches the seeded fill's own clock -- see the module
    # docstring's "FakeBroker's internal clock" note in test_intraday_jobs.py for why a
    # mismatch here would spuriously trip the time stop before this test even runs.
    _seed_open_shadow_position(broker, shadow_ledger, cfg, symbol="NVDA")

    result = run_placebo_cycle(
        "cycle_1",
        cfg,
        broker,
        shadow_ledger,
        real_ledger,
        _FakeMarketData(quotes={"NVDA": _quote(190.0)}),
        AS_OF,
        NOW,
        random.Random(0),
    )
    assert result.skipped_reason == "no eligible watchlist candidates"
    real_ledger.close()
    shadow_ledger.close()


def test_run_placebo_cycle_proposes_a_seeded_random_candidate_and_submits_it(tmp_path, config):
    cfg = _placebo_config(config, tmp_path)
    real_ledger = _ledger(tmp_path, "real.sqlite3")
    shadow_ledger = _ledger(tmp_path, "shadow.sqlite3")
    real_ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    real_ledger.record_watchlist_item(AS_OF, "AMD", thesis="test")
    broker = FakeBroker()
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0), "AMD": _quote(150.0)})

    result = run_placebo_cycle(
        "cycle_1",
        cfg,
        broker,
        shadow_ledger,
        real_ledger,
        market_data,
        AS_OF,
        NOW,
        random.Random(1),
    )

    assert result.proposed_symbol in ("NVDA", "AMD")
    assert result.order_submitted is not None
    assert len(broker.list_positions()) == 1
    accepted = [r for r in shadow_ledger.proposals_for(AS_OF) if r["status"] == "accepted"]
    assert len(accepted) == 1
    assert accepted[0]["payload"]["thesis"].startswith("placebo:")
    real_ledger.close()
    shadow_ledger.close()


def test_run_placebo_cycle_is_rejected_once_the_new_positions_budget_is_spent(tmp_path, config):
    cfg = _placebo_config(config, tmp_path, new_positions_per_day=0)
    real_ledger = _ledger(tmp_path, "real.sqlite3")
    shadow_ledger = _ledger(tmp_path, "shadow.sqlite3")
    real_ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    broker = FakeBroker()
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})

    result = run_placebo_cycle(
        "cycle_1",
        cfg,
        broker,
        shadow_ledger,
        real_ledger,
        market_data,
        AS_OF,
        NOW,
        random.Random(0),
    )

    assert result.proposed_symbol == "NVDA"
    assert result.order_submitted is None
    assert "new_positions_per_day" in result.skipped_reason
    assert broker.list_positions() == []
    rejected = [r for r in shadow_ledger.proposals_for(AS_OF) if r["status"] == "rejected"]
    assert len(rejected) == 1
    real_ledger.close()
    shadow_ledger.close()


def test_run_placebo_cycle_closes_an_invalidated_position_before_the_next_mark(tmp_path, config):
    cfg = _placebo_config(config, tmp_path)
    real_ledger = _ledger(tmp_path, "real.sqlite3")
    shadow_ledger = _ledger(tmp_path, "shadow.sqlite3")
    broker = FakeBroker(now=NOW)
    _seed_open_shadow_position(
        broker, shadow_ledger, cfg, symbol="NVDA", price=190.0, invalidation_price=185.0
    )
    market_data = _FakeMarketData(quotes={"NVDA": _quote(180.0)})  # through invalidation

    run_placebo_cycle(
        "cycle_1",
        cfg,
        broker,
        shadow_ledger,
        real_ledger,
        market_data,
        AS_OF,
        NOW,
        random.Random(0),
    )

    assert broker.list_positions() == []
    assert any(i["message"] == "stopout:NVDA" for i in shadow_ledger.incidents())
    # The close's own fill is already reflected by the time this cycle returns -- the next
    # cycle's mark-to-market would see it too, since it's read fresh from the ledger.
    assert shadow_ledger.total_realized_cash_flow() != 0.0
    real_ledger.close()
    shadow_ledger.close()


# -- run_placebo_carry --------------------------------------------------------------------


def test_run_placebo_carry_closes_every_placebo_position(tmp_path, config):
    """Every placebo proposal leaves `horizon` unset -- `run_carry`'s own safe default
    (no written multi-day thesis -> close) makes every placebo position intraday-only."""
    cfg = _placebo_config(config, tmp_path)
    shadow_ledger = _ledger(tmp_path, "shadow.sqlite3")
    broker = FakeBroker(now=NOW)
    _seed_open_shadow_position(broker, shadow_ledger, cfg, symbol="NVDA")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})

    result = run_placebo_carry(cfg, broker, shadow_ledger, market_data, AS_OF, NOW)

    assert result.closed == ["NVDA"]
    assert broker.list_positions() == []
    shadow_ledger.close()

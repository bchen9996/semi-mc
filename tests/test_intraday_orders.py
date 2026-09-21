"""`intraday/orders.py`: turning an accepted `open`/`add` proposal into a real order with
a real stop resting at the broker (S4, `BRIEF_INTRADAY.md`, Phase 3). Exercised only
against `FakeBroker` -- no network, matching this project's convention.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from semimc.config import IntradayConfig
from semimc.execution.broker import Asset, OrderRequest
from semimc.execution.fake_broker import FakeBroker
from semimc.intraday.ledger import IntradayLedger
from semimc.intraday.orders import (
    build_entry_order,
    intraday_client_order_id,
    submit_order_with_fault_isolation,
)
from semimc.intraday.proposals import TradeProposal

AS_OF = pd.Timestamp("2026-09-21")
CYCLE = "cycle_1"
NOW = dt.datetime(2026, 9, 21, 14, 30)


def _proposal(**overrides) -> TradeProposal:
    payload = {
        "action": "open",
        "symbol": "NVDA",
        "side": "buy",
        "size_pct": 0.05,
        "limit_price": 190.0,
        "thesis": "AI capex tailwind into earnings",
        "invalidation_price": 185.0,
        "time_stop_minutes": 60,
    }
    payload.update(overrides)
    return TradeProposal(**payload)


def _ledger(tmp_path) -> IntradayLedger:
    return IntradayLedger(tmp_path / "intraday.sqlite3")


def _order(**overrides) -> OrderRequest:
    payload = dict(
        symbol="NVDA",
        side="buy",
        type="limit",
        time_in_force="day",
        client_order_id="test-id",
        qty=1.0,
        limit_price=190.0,
        order_class="oto",
        stop_loss_price=185.0,
    )
    payload.update(overrides)
    return OrderRequest(**payload)


# -- intraday_client_order_id -------------------------------------------------------------


def test_intraday_client_order_id_is_deterministic():
    a = intraday_client_order_id(AS_OF, CYCLE, "NVDA", attempt=0)
    b = intraday_client_order_id(AS_OF, CYCLE, "NVDA", attempt=0)
    assert a == b


def test_intraday_client_order_id_changes_with_any_element():
    base = intraday_client_order_id(AS_OF, CYCLE, "NVDA", attempt=0)
    assert intraday_client_order_id(AS_OF + pd.Timedelta(days=1), CYCLE, "NVDA", 0) != base
    assert intraday_client_order_id(AS_OF, "cycle_2", "NVDA", 0) != base
    assert intraday_client_order_id(AS_OF, CYCLE, "AMD", 0) != base
    assert intraday_client_order_id(AS_OF, CYCLE, "NVDA", 1) != base


# -- build_entry_order --------------------------------------------------------------------


@pytest.mark.parametrize("action", ["close", "trim", "hold", "no_trade"])
def test_build_entry_order_rejects_every_non_open_add_action(action):
    proposal = TradeProposal(
        action=action, symbol="NVDA", size_pct=0.02 if action == "trim" else None
    )
    with pytest.raises(ValueError, match="open/add"):
        build_entry_order(
            proposal, AS_OF, CYCLE, price=190.0, equity=100_000.0, config=IntradayConfig()
        )


def test_build_entry_order_sizes_to_whole_shares():
    proposal = _proposal(size_pct=0.05)
    order = build_entry_order(
        proposal, AS_OF, CYCLE, price=190.0, equity=100_000.0, config=IntradayConfig()
    )
    # notional = 0.05 * 100_000 = 5_000; 5_000 / 190 = 26.31... -> floor to 26
    assert order.qty == 26
    assert order.notional is None


def test_build_entry_order_raises_when_size_rounds_to_zero_shares():
    proposal = _proposal(size_pct=0.001)
    with pytest.raises(ValueError, match="too small"):
        build_entry_order(
            proposal, AS_OF, CYCLE, price=1000.0, equity=1000.0, config=IntradayConfig()
        )


def test_build_entry_order_uses_a_collared_limit_price_past_the_quote_for_a_buy():
    proposal = _proposal(side="buy")
    config = IntradayConfig(risk={"equity_cost_bps_per_side": 8.0})
    order = build_entry_order(proposal, AS_OF, CYCLE, price=190.0, equity=100_000.0, config=config)
    assert order.limit_price == pytest.approx(190.0 * (1 + 8.0 / 10_000))


def test_build_entry_order_collars_the_other_direction_for_a_sell():
    proposal = _proposal(side="sell")
    config = IntradayConfig(risk={"equity_cost_bps_per_side": 8.0})
    order = build_entry_order(proposal, AS_OF, CYCLE, price=190.0, equity=100_000.0, config=config)
    assert order.limit_price == pytest.approx(190.0 * (1 - 8.0 / 10_000))


def test_build_entry_order_attaches_a_real_stop_at_the_invalidation_price():
    proposal = _proposal(invalidation_price=185.0)
    order = build_entry_order(
        proposal, AS_OF, CYCLE, price=190.0, equity=100_000.0, config=IntradayConfig()
    )
    assert order.order_class == "oto"
    assert order.stop_loss_price == pytest.approx(185.0)
    assert order.stop_loss_limit_price is None  # a plain stop, not a stop-limit


def test_build_entry_order_id_is_deterministic_per_symbol_and_attempt():
    proposal = _proposal()
    order = build_entry_order(
        proposal, AS_OF, CYCLE, price=190.0, equity=100_000.0, config=IntradayConfig(), attempt=2
    )
    assert order.client_order_id == intraday_client_order_id(AS_OF, CYCLE, "NVDA", attempt=2)


# -- submit_order_with_fault_isolation ------------------------------------------------------


def test_submit_records_an_incident_and_returns_none_for_a_non_tradable_asset(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker(assets={"NVDA": Asset(symbol="NVDA", tradable=False)})
    result = submit_order_with_fault_isolation(_order(), broker, ledger, NOW)
    assert result is None
    incidents = ledger.incidents()
    assert any("not tradable" in i["message"] for i in incidents)
    ledger.close()


def test_submit_records_an_incident_and_returns_none_on_rate_limit(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker(rate_limit_calls_remaining=1)
    result = submit_order_with_fault_isolation(_order(), broker, ledger, NOW)
    assert result is None
    incidents = ledger.incidents()
    assert any("rate limited" in i["message"] for i in incidents)
    ledger.close()


def test_submit_records_an_incident_and_returns_none_on_a_wash_trade(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker(wash_trade_symbols={"NVDA"}, partial_fill_fraction=0.0)
    # Seed an already-open opposite-side order in NVDA so the coming buy collides with it.
    broker.submit_order(_order(side="sell", client_order_id="seed-nvda-sell", order_class="simple"))

    result = submit_order_with_fault_isolation(_order(side="buy"), broker, ledger, NOW)
    assert result is None
    incidents = ledger.incidents()
    assert any("could interact" in i["message"] for i in incidents)
    ledger.close()


def test_submit_records_an_incident_and_returns_none_when_the_account_is_blocked(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker(account_status="ACCOUNT_BLOCKED")
    result = submit_order_with_fault_isolation(_order(), broker, ledger, NOW)
    assert result is None
    incidents = ledger.incidents()
    assert any("ACCOUNT_BLOCKED" in i["message"] for i in incidents)
    ledger.close()


def test_submit_records_an_incident_and_returns_none_when_the_market_is_closed(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker(market_open=False)
    result = submit_order_with_fault_isolation(_order(), broker, ledger, NOW)
    assert result is None
    incidents = ledger.incidents()
    assert any("market is closed" in i["message"] for i in incidents)
    ledger.close()


def test_a_timeout_after_a_successful_submit_recovers_by_looking_up_the_order(tmp_path):
    ledger = _ledger(tmp_path)
    order_id = "seed-timeout"
    broker = FakeBroker(timeout_after_success_ids={order_id})
    result = submit_order_with_fault_isolation(
        _order(client_order_id=order_id), broker, ledger, NOW
    )
    assert result is not None
    assert result.client_order_id == order_id
    assert ledger.get_order(order_id) is not None
    ledger.close()


def test_a_normal_submit_records_the_order_and_fill_and_returns_it(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()  # fills fully by default
    order = _order(client_order_id="normal-fill")

    result = submit_order_with_fault_isolation(order, broker, ledger, NOW, proposal_id=7)

    assert result is not None
    assert result.status == "filled"
    row = ledger.get_order("normal-fill")
    assert row is not None
    assert row["proposal_id"] == 7
    assert row["symbol"] == "NVDA"
    fills = ledger.fills_for_order("normal-fill")
    assert len(fills) == 1
    assert fills[0]["qty"] == pytest.approx(order.qty)
    ledger.close()

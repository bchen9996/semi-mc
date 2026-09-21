"""`execution/orders.py`: turning a frozen `Decision` into concrete, broker-agnostic
`OrderRequest`s. No broker involved here -- prices, positions, and fractionability are all
plain inputs, per the module's own "broker-agnostic" design.
"""

from __future__ import annotations

import pandas as pd
import pytest

from semimc.execution.broker import OrderRequest, Position
from semimc.execution.orders import build_orders, client_order_id, sequence_orders, target_notional
from semimc.strategy.decide import Decision


def _decision(weights: dict[str, float], as_of="2026-01-05", sleeve="s0") -> Decision:
    return Decision(as_of=pd.Timestamp(as_of), sleeve=sleeve, weights=pd.Series(weights))


def test_order_request_defaults_to_a_simple_order_with_no_protective_stop():
    """S4's OTO protective stop (Phase 3) is opt-in -- every existing caller of this
    module leaves these three fields untouched."""
    order = OrderRequest(
        symbol="NVDA",
        side="buy",
        type="limit",
        time_in_force="day",
        client_order_id="x",
        qty=1.0,
        limit_price=100.0,
    )
    assert order.order_class == "simple"
    assert order.stop_loss_price is None
    assert order.stop_loss_limit_price is None


def test_client_order_id_is_deterministic_and_per_symbol():
    decision = _decision({"NVDA": 0.5, "AMD": 0.5})
    first = client_order_id(decision, "NVDA")
    again = client_order_id(decision, "NVDA")
    other_symbol = client_order_id(decision, "AMD")
    assert first == again
    assert first != other_symbol
    assert len(first) <= 128


def test_target_notional_is_weights_times_equity():
    decision = _decision({"NVDA": 0.5, "AMD": 0.5})
    targets = target_notional(decision, equity=10_000.0)
    assert targets["NVDA"] == pytest.approx(5_000.0)
    assert targets["AMD"] == pytest.approx(5_000.0)


def test_build_orders_buys_from_a_flat_book(config):
    decision = _decision({"NVDA": 0.5, "AMD": 0.5})
    prices = pd.Series({"NVDA": 100.0, "AMD": 50.0})
    orders = build_orders(decision, positions={}, prices=prices, equity=10_000.0, config=config)
    by_symbol = {o.symbol: o for o in orders}
    assert by_symbol["NVDA"].side == "buy"
    assert by_symbol["NVDA"].qty == pytest.approx(50.0)
    assert by_symbol["AMD"].qty == pytest.approx(100.0)
    # collared marketable limit: buys priced *above* the last price.
    collar = config.paper.execution.collar_bps / 10_000
    assert by_symbol["NVDA"].limit_price == pytest.approx(100.0 * (1 + collar))


def test_build_orders_sells_priced_below_the_quote(config):
    decision = _decision({"NVDA": 0.0})
    positions = {
        "NVDA": Position(
            symbol="NVDA", qty=20.0, side="long", market_value=2000.0, avg_entry_price=100.0
        )
    }
    prices = pd.Series({"NVDA": 100.0})
    orders = build_orders(decision, positions, prices, equity=10_000.0, config=config)
    assert len(orders) == 1
    order = orders[0]
    assert order.side == "sell"
    assert order.qty == pytest.approx(20.0)
    collar = config.paper.execution.collar_bps / 10_000
    assert order.limit_price == pytest.approx(100.0 * (1 - collar))


def test_build_orders_skips_trades_below_the_minimum_threshold(config):
    """0.5% of $10,000 equity is $50 -- a $10 rebalance must not generate an order."""
    decision = _decision({"NVDA": 0.501})
    positions = {
        "NVDA": Position(
            symbol="NVDA", qty=50.0, side="long", market_value=5000.0, avg_entry_price=100.0
        )
    }
    prices = pd.Series({"NVDA": 100.0})
    orders = build_orders(decision, positions, prices, equity=10_000.0, config=config)
    assert orders == []


def test_build_orders_rounds_to_whole_shares_when_not_fractionable(config):
    decision = _decision({"NVDA": 1.0})
    prices = pd.Series({"NVDA": 33.0})  # 10000 / 33 = 303.03...
    orders = build_orders(
        decision, {}, prices, equity=10_000.0, config=config, fractionable={"NVDA": False}
    )
    assert orders[0].qty == pytest.approx(303.0)


def test_build_orders_ignores_a_symbol_with_no_usable_price(config):
    decision = _decision({"NVDA": 0.5, "AMD": 0.5})
    prices = pd.Series({"NVDA": 100.0})  # AMD price missing
    orders = build_orders(decision, {}, prices, equity=10_000.0, config=config)
    assert {o.symbol for o in orders} == {"NVDA"}


def test_build_orders_never_shorts_even_from_a_malformed_negative_weight(config):
    """Every real sleeve's weights are non-negative by construction, so this defensive cap
    should never bind in practice -- but a bug elsewhere must not be able to turn into a
    short sale here."""
    decision = _decision({"NVDA": -0.5})
    positions = {
        "NVDA": Position(
            symbol="NVDA", qty=10.0, side="long", market_value=1000.0, avg_entry_price=100.0
        )
    }
    prices = pd.Series({"NVDA": 100.0})
    orders = build_orders(decision, positions, prices, equity=10_000.0, config=config)
    assert len(orders) == 1
    assert orders[0].side == "sell"
    assert orders[0].qty == pytest.approx(10.0)  # capped at the held quantity, not 60


def test_sequence_orders_puts_sells_before_buys():
    buy = OrderRequest(
        symbol="NVDA",
        side="buy",
        type="limit",
        time_in_force="day",
        client_order_id="buy-1",
        qty=1.0,
        limit_price=100.0,
    )
    sell = OrderRequest(
        symbol="AMD",
        side="sell",
        type="limit",
        time_in_force="day",
        client_order_id="sell-1",
        qty=1.0,
        limit_price=50.0,
    )
    orders = sequence_orders([buy, sell])
    assert [o.side for o in orders] == ["sell", "buy"]

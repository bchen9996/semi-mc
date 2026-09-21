"""`FakeBroker`: the only broker the normal test suite is allowed to construct.

Covers the full `Broker` protocol surface and every failure-injection mode the
paper-trading brief names for sandbox testing (timeouts after a successful submit,
rate-limit errors, partial fills, rejections, wash-trade rejections, a closed market, and
a blocked account) -- built here, ahead of the real execution/pretrade logic (a later
phase), so that logic has a trustworthy double to run against from day one.
"""

from __future__ import annotations

import datetime as dt

import pytest

from semimc.execution.broker import (
    AccountBlockedError,
    Asset,
    AssetNotTradableError,
    MarketClosedError,
    Order,
    OrderRequest,
    RateLimitError,
    WashTradeError,
)
from semimc.execution.fake_broker import FakeBroker


def buy(symbol="NVDA", qty=10.0, client_order_id="co-1", **kwargs) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side="buy",
        type="limit",
        time_in_force="day",
        client_order_id=client_order_id,
        qty=qty,
        limit_price=100.0,
        **kwargs,
    )


# -- protocol surface ---------------------------------------------------------------------


def test_order_request_requires_exactly_one_of_qty_or_notional():
    with pytest.raises(ValueError, match="exactly one"):
        OrderRequest(
            symbol="NVDA",
            side="buy",
            type="market",
            time_in_force="day",
            client_order_id="x",
            qty=10,
            notional=100,
        )
    with pytest.raises(ValueError, match="exactly one"):
        OrderRequest(
            symbol="NVDA", side="buy", type="market", time_in_force="day", client_order_id="x"
        )


def test_an_oto_order_fills_its_entry_leg_like_a_simple_order():
    """S4's protective stop (Phase 3) -- FakeBroker never reads order_class/
    stop_loss_price at all; it fills the entry leg exactly as before and does not
    simulate the stop leg ever triggering (a disclosed gap, see the class docstring)."""
    broker = FakeBroker()
    order = broker.submit_order(buy(order_class="oto", stop_loss_price=95.0))
    assert order.status == "filled"
    assert order.filled_qty == pytest.approx(10.0)


def test_get_account_reflects_constructor_values():
    broker = FakeBroker(equity=50_000.0, cash=40_000.0, account_number="PA123")
    account = broker.get_account()
    assert account.equity == pytest.approx(50_000.0)
    assert account.cash == pytest.approx(40_000.0)
    assert account.account_number == "PA123"
    assert account.status == "ACTIVE"


def test_get_asset_defaults_to_fully_tradable():
    broker = FakeBroker()
    asset = broker.get_asset("NVDA")
    assert asset.tradable and asset.fractionable and asset.shortable and asset.easy_to_borrow


def test_get_calendar_returns_weekdays_only():
    broker = FakeBroker()
    days = broker.get_calendar(dt.date(2026, 1, 5), dt.date(2026, 1, 11))  # Mon-Sun
    assert [d.date.weekday() for d in days] == [0, 1, 2, 3, 4]


def test_submit_then_fetch_by_client_order_id():
    broker = FakeBroker()
    order = broker.submit_order(buy())
    assert order.status == "filled"
    assert order.filled_qty == pytest.approx(10.0)
    fetched = broker.get_order_by_client_id("co-1")
    assert fetched is order


def test_filled_order_is_not_in_open_orders():
    broker = FakeBroker()
    broker.submit_order(buy())
    assert broker.list_open_orders() == []


def test_fill_updates_positions():
    broker = FakeBroker()
    broker.submit_order(buy(qty=10.0))
    [position] = broker.list_positions()
    assert position.symbol == "NVDA"
    assert position.qty == pytest.approx(10.0)
    assert position.side == "long"


def test_sell_reduces_and_can_flip_a_position():
    broker = FakeBroker()
    broker.submit_order(buy(qty=10.0, client_order_id="co-buy"))
    broker.submit_order(
        OrderRequest(
            symbol="NVDA",
            side="sell",
            type="market",
            time_in_force="day",
            client_order_id="co-sell",
            qty=15.0,
        )
    )
    [position] = broker.list_positions()
    assert position.qty == pytest.approx(-5.0)
    assert position.side == "short"


def test_cancel_open_order_removes_it_from_open_orders():
    broker = FakeBroker(partial_fill_fraction=0.0)  # "accepted", never filled
    order = broker.submit_order(buy())
    assert broker.list_open_orders() != []
    broker.cancel_order(order.id)
    assert broker.list_open_orders() == []
    assert broker.get_order_by_client_id("co-1").status == "canceled"


def test_cancel_a_filled_order_raises():
    broker = FakeBroker()
    order = broker.submit_order(buy())
    with pytest.raises(ValueError, match="already filled"):
        broker.cancel_order(order.id)


def test_cancel_unknown_order_raises_key_error():
    with pytest.raises(KeyError):
        FakeBroker().cancel_order("no-such-id")


# -- idempotency ---------------------------------------------------------------------


def test_resubmitting_the_same_client_order_id_returns_the_existing_order():
    """Rerunning a job must never create duplicate orders."""
    broker = FakeBroker()
    first = broker.submit_order(buy())
    second = broker.submit_order(buy())  # identical client_order_id
    assert first is second
    assert len(broker.list_positions()) == 1
    assert broker.list_positions()[0].qty == pytest.approx(10.0)  # not double-filled


def test_timeout_after_success_still_records_the_order():
    """A caller must look an order up by id before retrying, not assume a timeout means
    nothing happened."""
    broker = FakeBroker(timeout_after_success_ids={"co-1"})
    with pytest.raises(TimeoutError):
        broker.submit_order(buy())
    recorded = broker.get_order_by_client_id("co-1")
    assert recorded is not None
    assert recorded.status == "filled"

    # A caller that finds the order and retries anyway must not get a duplicate or an error.
    retried = broker.submit_order(buy())
    assert retried is recorded


# -- failure injection named in the brief ---------------------------------------------


def test_rate_limit_error_then_recovers():
    broker = FakeBroker(rate_limit_calls_remaining=2)
    with pytest.raises(RateLimitError):
        broker.submit_order(buy(client_order_id="co-a"))
    with pytest.raises(RateLimitError):
        broker.submit_order(buy(client_order_id="co-b"))
    order = broker.submit_order(buy(client_order_id="co-c"))
    assert order.status == "filled"


def test_partial_fill():
    broker = FakeBroker(partial_fill_fraction=0.4)
    order = broker.submit_order(buy(qty=10.0))
    assert order.status == "partially_filled"
    assert order.filled_qty == pytest.approx(4.0)
    assert order in broker.list_open_orders()


def test_wash_trade_rejection_when_opposite_side_is_open():
    broker = FakeBroker(partial_fill_fraction=0.0, wash_trade_symbols={"NVDA"})
    broker.submit_order(buy(client_order_id="co-buy"))  # stays open (accepted)
    with pytest.raises(WashTradeError):
        broker.submit_order(
            OrderRequest(
                symbol="NVDA",
                side="sell",
                type="limit",
                time_in_force="day",
                client_order_id="co-sell",
                qty=10.0,
                limit_price=100.0,
            )
        )


def test_wash_trade_symbol_does_not_block_same_side_orders():
    broker = FakeBroker(partial_fill_fraction=0.0, wash_trade_symbols={"NVDA"})
    broker.submit_order(buy(client_order_id="co-a"))
    order = broker.submit_order(buy(client_order_id="co-b"))  # another buy: no conflict
    assert order.status == "accepted"


def test_closed_market_rejects_every_order():
    broker = FakeBroker(market_open=False)
    with pytest.raises(MarketClosedError):
        broker.submit_order(buy())


def test_blocked_account_rejects_every_order():
    broker = FakeBroker(account_status="ACCOUNT_BLOCKED")
    with pytest.raises(AccountBlockedError):
        broker.submit_order(buy())


def test_non_tradable_asset_is_rejected():
    broker = FakeBroker(assets={"NVDA": Asset(symbol="NVDA", tradable=False)})
    with pytest.raises(AssetNotTradableError):
        broker.submit_order(buy())


def test_order_type_matches_the_protocol() -> None:
    """A `FakeBroker` must return the same `Order` shape a real broker's client would."""
    order = FakeBroker().submit_order(buy())
    assert isinstance(order, Order)
    assert order.client_order_id == "co-1"

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
    BrokerError,
    Clock,
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


def test_an_oto_order_synthesizes_a_cancelable_resting_stop_leg():
    """S4's close/trim path (Phase 4) needs to find and cancel the resting stop before
    reversing -- confirms FakeBroker models it as a real, independently addressable
    order, not just an inert field on the parent."""
    broker = FakeBroker()
    entry = broker.submit_order(
        buy(client_order_id="co-oto", order_class="oto", stop_loss_price=95.0)
    )

    assert entry.legs is not None
    assert len(entry.legs) == 1
    leg = entry.legs[0]
    assert leg.symbol == "NVDA"
    assert leg.side == "sell"  # opposite of the buy entry
    assert leg.type == "stop"
    assert leg.status == "new"

    found = broker.get_order_by_client_id(leg.client_order_id)
    assert found is not None and found.id == leg.id

    broker.cancel_order(leg.id)
    assert broker.get_order_by_client_id(leg.client_order_id).status == "canceled"


def test_a_simple_order_has_no_legs():
    broker = FakeBroker()
    order = broker.submit_order(buy())
    assert order.legs is None


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


def sell(qty=10.0, client_order_id="co-sell") -> OrderRequest:
    return OrderRequest(
        symbol="NVDA",
        side="sell",
        type="limit",
        time_in_force="day",
        client_order_id=client_order_id,
        qty=qty,
        limit_price=100.0,
    )


def test_a_delayed_cancel_stays_open_until_enough_lookups():
    """Alpaca's pending_cancel: still open (and still holding shares) until it completes."""
    broker = FakeBroker(cancel_delay_lookups=2)
    leg = broker.submit_order(buy(order_class="oto", stop_loss_price=95.0)).legs[0]
    broker.cancel_order(leg.id)
    broker.cancel_order(leg.id)  # a second cancel while pending changes nothing
    assert leg in broker.list_open_orders()
    assert broker.get_order_by_client_id(leg.client_order_id).status == "accepted"
    assert broker.get_order_by_client_id(leg.client_order_id).status == "canceled"
    assert broker.list_open_orders() == []


def test_enforce_held_qty_rejects_a_sell_the_resting_stop_still_holds():
    broker = FakeBroker(enforce_held_qty=True, cancel_delay_lookups=1)
    leg = broker.submit_order(buy(order_class="oto", stop_loss_price=95.0)).legs[0]
    with pytest.raises(BrokerError, match=r"insufficient qty available .*available: 0"):
        broker.submit_order(sell())
    broker.cancel_order(leg.id)
    with pytest.raises(BrokerError, match="insufficient qty"):  # pending cancel still holds
        broker.submit_order(sell())
    broker.get_order_by_client_id(leg.client_order_id)  # the cancel completes
    assert broker.submit_order(sell()).status == "filled"
    assert broker.list_positions() == []


def test_enforce_held_qty_is_off_by_default():
    broker = FakeBroker()
    broker.submit_order(buy(order_class="oto", stop_loss_price=95.0))
    assert broker.submit_order(sell()).status == "filled"


def test_trigger_stop_fills_a_resting_stop_and_reduces_the_position():
    broker = FakeBroker()
    leg = broker.submit_order(buy(order_class="oto", stop_loss_price=95.0)).legs[0]
    filled = broker.trigger_stop(leg.client_order_id, 94.9)
    assert (filled.status, filled.filled_qty, filled.filled_avg_price) == ("filled", 10.0, 94.9)
    assert broker.list_positions() == []
    assert broker.list_open_orders() == []
    with pytest.raises(ValueError, match="not a resting stop"):
        broker.trigger_stop(leg.client_order_id, 94.9)


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


def test_a_given_clock_is_what_get_clock_reports_and_orders_still_follow_market_open():
    """The intraday shadow broker carries the real exchange clock: whether the market is
    open is real, while fills stay simulated."""
    closed = Clock(
        timestamp=dt.datetime(2026, 10, 6, 22, 56),
        is_open=False,
        next_open=dt.datetime(2026, 10, 7, 13, 30),
        next_close=dt.datetime(2026, 10, 7, 20, 0),
    )
    broker = FakeBroker(clock=closed)

    assert broker.get_clock() is closed
    assert broker.submit_order(buy()).status == "filled"


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


# -- state (de)serialization (S4's placebo agent, Phase 7) ---------------------------------


def test_to_state_from_state_round_trips_orders_positions_and_open_sides():
    broker = FakeBroker(account_number="SHADOW-1", account_status="ACTIVE")
    broker.submit_order(buy(client_order_id="co-oto", order_class="oto", stop_loss_price=95.0))

    state = broker.to_state()
    restored = FakeBroker.from_state(state, now=dt.datetime(2026, 3, 1, 9, 30))

    assert restored.account_number == "SHADOW-1"
    assert restored.account_status == "ACTIVE"
    assert restored.now == dt.datetime(2026, 3, 1, 9, 30)  # from the caller, not the state

    original_order = broker.get_order_by_client_id("co-oto")
    restored_order = restored.get_order_by_client_id("co-oto")
    assert restored_order is not None
    assert restored_order.status == original_order.status
    assert restored_order.filled_qty == pytest.approx(original_order.filled_qty)
    assert restored_order.legs is not None and len(restored_order.legs) == 1
    assert restored_order.legs[0].client_order_id == original_order.legs[0].client_order_id
    assert restored_order.legs[0].status == "new"

    assert restored.list_positions() == broker.list_positions()

    # The resting stop leg's own side is still tracked as open on the restored broker --
    # confirms cancel_resting_stops-style wash-trade checks survive the round trip.
    assert restored._open_sides == broker._open_sides


def test_from_state_none_returns_a_fresh_broker():
    broker = FakeBroker.from_state(None, now=dt.datetime(2026, 3, 1, 9, 30))
    assert broker.list_positions() == []
    assert broker.now == dt.datetime(2026, 3, 1, 9, 30)


def test_to_state_omits_equity_and_cash():
    """placebo.py's mark-to-market is the single source of truth for both -- persisting
    them here would just persist a number nothing keeps correct (FakeBroker.equity/.cash
    never move on their own; see the class docstring)."""
    state = FakeBroker(equity=123.0, cash=456.0).to_state()
    assert "equity" not in state
    assert "cash" not in state

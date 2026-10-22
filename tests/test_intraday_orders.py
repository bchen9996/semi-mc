"""`intraday/orders.py`: turning an accepted proposal into a real order with a real stop
resting at the broker -- opening/adding (Phase 3), or closing/trimming while keeping that
protection intact (Phase 4). S4, `BRIEF_INTRADAY.md`. Exercised only against `FakeBroker`
-- no network, matching this project's convention.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from semimc.config import IntradayConfig
from semimc.execution.broker import Asset, BrokerError, OrderRequest, Position
from semimc.execution.fake_broker import FakeBroker
from semimc.intraday.ledger import IntradayLedger
from semimc.intraday.orders import (
    OrderSync,
    build_close_order,
    build_entry_order,
    build_replacement_stop_order,
    build_trim_order,
    cancel_resting_stops,
    intraday_client_order_id,
    submit_exit,
    submit_order_with_fault_isolation,
    sync_open_orders,
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


def test_build_entry_order_takes_a_share_count_in_place_of_size_pct():
    """`jobs.py::run_cycle` rounds a tranched entry once and passes each piece's shares."""
    order = build_entry_order(
        _proposal(size_pct=0.05),
        AS_OF,
        CYCLE,
        price=190.0,
        equity=100_000.0,
        config=IntradayConfig(),
        qty=13,
    )
    assert order.qty == 13


def test_build_entry_order_raises_when_size_rounds_to_zero_shares():
    proposal = _proposal(size_pct=0.001)
    with pytest.raises(ValueError, match="too small"):
        build_entry_order(
            proposal, AS_OF, CYCLE, price=1000.0, equity=1000.0, config=IntradayConfig()
        )


def test_a_later_tranche_buys_one_share_when_a_price_rise_rounds_it_to_zero():
    # One share's worth at the first tranche's price (1,000), then the price rose to 1,010.
    proposal = _proposal(size_pct=0.01)
    order = build_entry_order(
        proposal,
        AS_OF,
        CYCLE,
        price=1010.0,
        equity=100_000.0,
        config=IntradayConfig(),
        tranche=2,
        at_least_one_share=True,
    )
    assert order.qty == 1


def test_build_entry_order_uses_a_collared_limit_price_past_the_quote_for_a_buy():
    proposal = _proposal(side="buy")
    config = IntradayConfig(risk={"equity_cost_bps_per_side": 8.0})
    order = build_entry_order(proposal, AS_OF, CYCLE, price=190.0, equity=100_000.0, config=config)
    # round_to_tick: 190.0 * 1.0008 = 190.152, rounded to Alpaca's $0.01 increment.
    assert order.limit_price == pytest.approx(190.15)


def test_build_entry_order_collars_the_other_direction_for_a_sell():
    proposal = _proposal(side="sell")
    config = IntradayConfig(risk={"equity_cost_bps_per_side": 8.0})
    order = build_entry_order(proposal, AS_OF, CYCLE, price=190.0, equity=100_000.0, config=config)
    # round_to_tick: 190.0 * 0.9992 = 189.848, rounded to Alpaca's $0.01 increment.
    assert order.limit_price == pytest.approx(189.85)


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


def test_submit_of_an_oto_order_also_records_its_resting_stop_leg(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    order = _order(client_order_id="entry-with-stop", order_class="oto", stop_loss_price=185.0)

    submit_order_with_fault_isolation(order, broker, ledger, NOW, proposal_id=3)

    [leg] = ledger.open_stop_legs_for("NVDA")
    assert leg["client_order_id"] == "entry-with-stop::stop"
    assert leg["proposal_id"] == 3
    ledger.close()


# -- cancel_resting_stops -------------------------------------------------------------------


def test_cancel_resting_stops_cancels_a_real_leg(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    entry = broker.submit_order(_order(client_order_id="entry-1"))  # order_class="oto" by default
    leg = entry.legs[0]
    ledger.record_order(
        entry.client_order_id, "NVDA", "buy", status=entry.status, broker_order_id=entry.id
    )
    ledger.record_order(
        leg.client_order_id, "NVDA", "sell", status=leg.status, broker_order_id=leg.id, leg="stop"
    )

    result = cancel_resting_stops("NVDA", broker, ledger)

    assert result == [leg.client_order_id]
    assert broker.get_order_by_client_id(leg.client_order_id).status == "canceled"
    assert ledger.get_order(leg.client_order_id)["status"] == "canceled"
    ledger.close()


def test_cancel_resting_stops_cancels_every_tranches_leg(tmp_path):
    """2026-10-02: a two-tranche NVDA close canceled only the second tranche's stop,
    leaving the first one's 3-share sell stop resting with no position behind it."""
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    legs = []
    for client_order_id in ("entry-t1", "entry-t2"):
        submit_order_with_fault_isolation(
            _order(client_order_id=client_order_id), broker, ledger, NOW
        )
        legs.append(f"{client_order_id}::stop")

    result = cancel_resting_stops("NVDA", broker, ledger)

    assert result == legs
    assert all(broker.get_order_by_client_id(leg).status == "canceled" for leg in legs)
    assert ledger.open_stop_legs_for("NVDA") == []
    ledger.close()


def test_cancel_resting_stops_returns_nothing_when_there_is_nothing_resting(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    assert cancel_resting_stops("NVDA", broker, ledger) == []
    ledger.close()


def test_cancel_resting_stops_tolerates_an_already_gone_order(tmp_path):
    """The ledger thinks a stop is resting, but the broker no longer has it (already
    canceled/filled) -- the same forgiving read real Alpaca's own cancel_order gets."""
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    ledger.record_order(
        "stale-stop", "NVDA", "sell", status="new", broker_order_id="not-a-real-id", leg="stop"
    )
    result = cancel_resting_stops("NVDA", broker, ledger)
    assert result == ["stale-stop"]
    assert ledger.open_stop_legs_for("NVDA") == []  # no longer listed as resting
    ledger.close()


# -- build_close_order ---------------------------------------------------------------------


def test_build_close_order_sells_a_long_position():
    position = Position(
        symbol="NVDA", qty=26.0, side="long", market_value=4940.0, avg_entry_price=190.0
    )
    order = build_close_order(position, AS_OF, CYCLE, price=190.0, config=IntradayConfig())
    assert order.side == "sell"
    assert order.qty == pytest.approx(26.0)
    assert order.order_class == "simple"
    assert order.stop_loss_price is None


def test_build_close_order_buys_back_a_short_position():
    position = Position(
        symbol="NVDA", qty=-10.0, side="short", market_value=-1900.0, avg_entry_price=190.0
    )
    order = build_close_order(position, AS_OF, CYCLE, price=190.0, config=IntradayConfig())
    assert order.side == "buy"
    assert order.qty == pytest.approx(10.0)


def test_build_close_order_without_a_price_is_a_market_order():
    """What a due time stop or carry sends on the real account when no price could be read
    (2026-10-15); with a price it stays a collared limit."""
    position = Position(
        symbol="NVDA", qty=26.0, side="long", market_value=4940.0, avg_entry_price=190.0
    )
    priced = build_close_order(position, AS_OF, CYCLE, price=190.0, config=IntradayConfig())
    unpriced = build_close_order(position, AS_OF, CYCLE, price=None, config=IntradayConfig())
    assert (priced.type, unpriced.type) == ("limit", "market")
    assert unpriced.limit_price is None
    assert (unpriced.side, unpriced.qty) == ("sell", pytest.approx(26.0))
    assert unpriced.client_order_id == priced.client_order_id


# -- build_trim_order ----------------------------------------------------------------------


def test_build_trim_order_rejects_a_non_trim_proposal():
    proposal = _proposal(action="open")
    position = Position(
        symbol="NVDA", qty=26.0, side="long", market_value=4940.0, avg_entry_price=190.0
    )
    with pytest.raises(ValueError, match="trim"):
        build_trim_order(
            proposal, position, AS_OF, CYCLE, price=190.0, equity=100_000.0, config=IntradayConfig()
        )


def test_build_trim_order_caps_at_the_held_quantity():
    # size_pct implies 5_000 / 190 = 26.3 shares -- more than the 10 actually held.
    proposal = TradeProposal(action="trim", symbol="NVDA", size_pct=0.05)
    position = Position(
        symbol="NVDA", qty=10.0, side="long", market_value=1900.0, avg_entry_price=190.0
    )
    order = build_trim_order(
        proposal, position, AS_OF, CYCLE, price=190.0, equity=100_000.0, config=IntradayConfig()
    )
    assert order.side == "sell"
    assert order.qty == pytest.approx(10.0)


def test_build_trim_order_sizes_below_the_cap_normally():
    proposal = TradeProposal(action="trim", symbol="NVDA", size_pct=0.02)
    position = Position(
        symbol="NVDA", qty=100.0, side="long", market_value=19_000.0, avg_entry_price=190.0
    )
    order = build_trim_order(
        proposal, position, AS_OF, CYCLE, price=190.0, equity=100_000.0, config=IntradayConfig()
    )
    # 0.02 * 100_000 = 2_000; 2_000 / 190 = 10.5 -> floor to 10
    assert order.qty == pytest.approx(10.0)


def test_build_trim_order_raises_when_size_rounds_to_zero_shares():
    proposal = TradeProposal(action="trim", symbol="NVDA", size_pct=0.001)
    position = Position(
        symbol="NVDA", qty=100.0, side="long", market_value=19_000.0, avg_entry_price=190.0
    )
    with pytest.raises(ValueError, match="too small"):
        build_trim_order(
            proposal, position, AS_OF, CYCLE, price=1000.0, equity=1000.0, config=IntradayConfig()
        )


# -- build_replacement_stop_order -----------------------------------------------------------


def test_build_replacement_stop_order_is_a_plain_standalone_stop():
    order = build_replacement_stop_order(
        "NVDA", "sell", qty=16.0, stop_price=185.0, as_of=AS_OF, cycle=CYCLE
    )
    assert order.type == "stop"
    assert order.order_class == "simple"
    assert order.stop_price == pytest.approx(185.0)
    assert order.qty == pytest.approx(16.0)
    assert order.client_order_id.endswith("::stop")


# -- the real broker's asynchronous cancel (2026-10-03) ------------------------------------


class _Sleeps:
    """Records `cancel_resting_stops`'s waits instead of sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _held(broker, ledger, client_order_id="entry-1", qty=6.0):
    """A filled OTO entry and its resting stop leg, recorded the way run_cycle does."""
    return submit_order_with_fault_isolation(
        _order(client_order_id=client_order_id, qty=qty), broker, ledger, NOW
    )


def _close(position):
    return build_close_order(position, AS_OF, CYCLE, price=190.0, config=IntradayConfig())


def _exit(broker, ledger, build=_close, stop_price=185.0):
    return submit_exit(
        "NVDA",
        build,
        broker,
        ledger,
        NOW,
        reason="trader_close",
        as_of=AS_OF,
        cycle=CYCLE,
        stop_price=stop_price,
        sleep=_Sleeps(),
    )


def _incidents(ledger, severity):
    return [i["message"] for i in ledger.incidents() if i["severity"] == severity]


def test_cancel_resting_stops_waits_for_a_pending_cancel_to_complete(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker(cancel_delay_lookups=3)
    _held(broker, ledger)
    sleeps = _Sleeps()

    cancel_resting_stops("NVDA", broker, ledger, NOW, sleep=sleeps)

    assert ledger.get_order("entry-1::stop")["status"] == "canceled"
    assert sleeps.calls == [0.25, 0.25]  # three lookups, two waits between them
    ledger.close()


def test_cancel_resting_stops_leaves_a_cancel_that_never_completes_open(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker(cancel_delay_lookups=10**6)
    _held(broker, ledger)
    sleeps = _Sleeps()

    cancel_resting_stops("NVDA", broker, ledger, NOW, sleep=sleeps)

    assert ledger.get_order("entry-1::stop")["status"] == "accepted"  # pending_cancel
    assert len(sleeps.calls) == 19  # 20 lookups, 5 s in all
    ledger.close()


def test_cancel_resting_stops_records_a_stop_that_fired_before_the_cancel(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    _held(broker, ledger)
    broker.trigger_stop("entry-1::stop", 184.9)

    cancel_resting_stops("NVDA", broker, ledger, NOW, sleep=_Sleeps())

    assert ledger.get_order("entry-1::stop")["status"] == "filled"
    [fill] = ledger.fills_for_order("entry-1::stop")
    assert (fill["qty"], fill["price"]) == (6.0, 184.9)
    cancel_resting_stops("NVDA", broker, ledger, NOW, sleep=_Sleeps())  # nothing open now
    assert len(ledger.fills_for_order("entry-1::stop")) == 1
    ledger.close()


def test_submit_turns_any_broker_rejection_into_an_incident(tmp_path):
    """Alpaca's "insufficient qty available" is a plain BrokerError; it used to escape
    and end the whole cycle."""
    ledger = _ledger(tmp_path)
    broker = FakeBroker(enforce_held_qty=True)
    _held(broker, ledger)  # its stop holds all 6 shares
    sell = _order(client_order_id="sell-1", side="sell", qty=6.0, order_class="simple")

    assert submit_order_with_fault_isolation(sell, broker, ledger, NOW) is None
    [message] = _incidents(ledger, "error")
    assert message.startswith("NVDA: sell order rejected by the broker: insufficient qty")
    ledger.close()


# -- submit_exit ------------------------------------------------------------------------------


def test_submit_exit_waits_out_the_cancel_before_closing(tmp_path):
    """The race the shadow broker hides: a close sent while the stop is still canceling
    is rejected, because the stop holds every share."""
    ledger = _ledger(tmp_path)
    broker = FakeBroker(enforce_held_qty=True, cancel_delay_lookups=2)
    _held(broker, ledger)

    attempt = _exit(broker, ledger)

    assert attempt.failure is None
    assert attempt.order.status == "filled"
    assert attempt.order.qty == 6.0
    assert broker.list_positions() == []
    assert ledger.get_order("entry-1::stop")["status"] == "canceled"
    assert _incidents(ledger, "error") == []
    ledger.close()


def test_submit_exit_holds_back_while_a_stop_is_still_canceling(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker(enforce_held_qty=True, cancel_delay_lookups=10**6)
    _held(broker, ledger)

    attempt = _exit(broker, ledger)

    assert attempt.order is None
    assert attempt.failure == (
        "NVDA: 1 order(s) still canceling after 5s; exit held back to the next cycle"
    )
    assert _incidents(ledger, "error") == [attempt.failure]
    assert broker.list_positions()[0].qty == 6.0
    assert len(broker.list_open_orders()) == 1  # just the old stop, still canceling
    ledger.close()


def test_submit_exit_closes_only_what_a_fired_stop_left(tmp_path):
    """One tranche's stop fired before the close: closing the old 6 shares would sell 3
    that are gone, opening a short."""
    ledger = _ledger(tmp_path)
    broker = FakeBroker(enforce_held_qty=True)
    _held(broker, ledger, "entry-t1", qty=3.0)
    _held(broker, ledger, "entry-t2", qty=3.0)
    broker.trigger_stop("entry-t1::stop", 184.9)

    attempt = _exit(broker, ledger)

    assert attempt.order.qty == 3.0
    assert broker.list_positions() == []
    assert ledger.get_order("entry-t1::stop")["status"] == "filled"
    assert ledger.get_order("entry-t2::stop")["status"] == "canceled"
    ledger.close()


def test_submit_exit_sends_nothing_when_the_stop_already_closed_it(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    _held(broker, ledger)
    broker.trigger_stop("entry-1::stop", 184.9)

    attempt = _exit(broker, ledger)

    assert (attempt.order, attempt.failure) == (None, None)
    assert _incidents(ledger, "warning") == [
        "NVDA: nothing left to exit once its stops were canceled"
    ]
    assert broker.list_positions() == []
    ledger.close()


class _RejectsExits(FakeBroker):
    """Accepts entries and stops; rejects every limit sell."""

    def submit_order(self, request: OrderRequest):
        if request.side == "sell" and request.type == "limit":
            raise BrokerError("simulated rejection")
        return super().submit_order(request)


def test_submit_exit_re_places_the_stop_when_the_exit_is_rejected(tmp_path):
    ledger = _ledger(tmp_path)
    broker = _RejectsExits()
    _held(broker, ledger)

    attempt = _exit(broker, ledger, stop_price=185.0)

    assert attempt.order is None
    assert attempt.failure == "NVDA: exit order failed"
    [restored] = ledger.open_stop_legs_for("NVDA")
    expected_id = intraday_client_order_id(AS_OF, CYCLE, "NVDA") + "::stop"
    assert restored["client_order_id"] == expected_id
    assert broker.get_order_by_client_id(expected_id).qty == 6.0
    assert _incidents(ledger, "warning") == [
        "NVDA: no exit went out after its stops were canceled; re-placed a stop for 6 "
        "shares at 185.00"
    ]
    ledger.close()


def test_submit_exit_re_places_the_stop_when_a_trim_is_too_small(tmp_path):
    """A trim too small for a whole share used to leave the position's stops canceled."""
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    _held(broker, ledger)
    tiny = TradeProposal(action="trim", symbol="NVDA", size_pct=0.0001)

    def trim(position):
        return build_trim_order(tiny, position, AS_OF, CYCLE, 190.0, 100_000.0, IntradayConfig())

    attempt = _exit(broker, ledger, build=trim)

    assert attempt.order is None
    assert "too small to trim a whole share" in attempt.failure
    assert len(ledger.open_stop_legs_for("NVDA")) == 1
    ledger.close()


def test_submit_exit_says_so_when_no_stop_can_be_re_placed(tmp_path):
    ledger = _ledger(tmp_path)
    broker = _RejectsExits()
    _held(broker, ledger)

    _exit(broker, ledger, stop_price=None)

    assert ledger.open_stop_legs_for("NVDA") == []
    assert _incidents(ledger, "error")[-1] == (
        "NVDA: no exit went out after its stops were canceled, and no invalidation price is "
        "on file to re-place one; position unprotected"
    )
    ledger.close()


def test_submit_exit_adds_no_stop_a_position_never_had(tmp_path):
    ledger = _ledger(tmp_path)
    broker = _RejectsExits()
    submit_order_with_fault_isolation(
        _order(client_order_id="plain", qty=6.0, order_class="simple"), broker, ledger, NOW
    )

    attempt = _exit(broker, ledger)

    assert attempt.failure == "NVDA: exit order failed"
    assert ledger.open_stop_legs_for("NVDA") == []
    assert broker.list_open_orders() == []
    ledger.close()


def test_submit_exit_cancels_an_earlier_exit_that_never_filled(tmp_path):
    """A close whose limit missed still holds the shares, so the retry would be rejected
    "insufficient qty available" if it were left working."""
    ledger = _ledger(tmp_path)
    broker = FakeBroker(enforce_held_qty=True)
    _held(broker, ledger)
    broker.partial_fill_fraction = 0.0  # exits now rest unfilled
    first = _exit(broker, ledger).order
    assert first.status == "accepted"

    retry = submit_exit(
        "NVDA",
        _close,
        broker,
        ledger,
        NOW,
        reason="time_stop",
        as_of=AS_OF,
        cycle="cycle_2",
        stop_price=185.0,
        sleep=_Sleeps(),
    )

    assert retry.order.client_order_id == f"{first.client_order_id}-r1"  # not the old order
    assert ledger.get_order(first.client_order_id)["status"] == "canceled"
    assert ledger.get_order(retry.order.client_order_id)["exit_reason"] == "time_stop"
    assert _incidents(ledger, "error") == []
    ledger.close()


def test_submit_exit_cancels_an_entry_tranche_still_working(tmp_path):
    """An open buy in the symbol is an opposite-side order Alpaca can refuse a sell over."""
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    _held(broker, ledger, "entry-t1", qty=3.0)
    broker.partial_fill_fraction = 0.0
    _held(broker, ledger, "entry-t2", qty=3.0)  # still working

    attempt = _exit(broker, ledger)

    assert ledger.get_order("entry-t2")["status"] == "canceled"
    assert attempt.order.qty == 3.0
    ledger.close()


# -- sync_open_orders ----------------------------------------------------------------------


def _traceable_entry(broker, ledger, qty=6.0) -> int:
    """An entry the monitor and reflections can trace back to its proposal."""
    proposal_id = ledger.record_proposal(
        AS_OF, CYCLE, "NVDA", "open", "accepted", payload=_proposal().model_dump()
    )
    submit_order_with_fault_isolation(
        _order(client_order_id="entry-1", qty=qty), broker, ledger, NOW, proposal_id=proposal_id
    )
    return proposal_id


def _sync(broker, ledger):
    return sync_open_orders(broker, ledger, IntradayConfig(), AS_OF, NOW)


def test_sync_records_an_entry_fill_that_arrives_after_submission(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker(partial_fill_fraction=0.0)  # open when submit returns, as on Alpaca
    _held(broker, ledger)
    assert ledger.latest_filled_entry_for("NVDA") is None
    broker.fill_order("entry-1", 190.0)

    synced = _sync(broker, ledger)

    assert synced.filled == ["entry-1"]
    assert synced.updated == ["entry-1"]
    assert ledger.latest_filled_entry_for("NVDA")["client_order_id"] == "entry-1"
    [fill] = ledger.fills_for_order("entry-1")
    assert (fill["qty"], fill["price"]) == (6.0, 190.0)
    assert ledger.get_order("entry-1::stop")["status"] == "new"
    ledger.close()


def test_sync_records_each_partial_fill_once(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker(partial_fill_fraction=0.0)
    _held(broker, ledger)

    broker.fill_order("entry-1", 190.0, qty=2.0)
    _sync(broker, ledger)
    assert _sync(broker, ledger).filled == []  # nothing new
    broker.fill_order("entry-1", 191.0)
    _sync(broker, ledger)

    assert [f["qty"] for f in ledger.fills_for_order("entry-1")] == [2.0, 4.0]
    assert ledger.get_order("entry-1")["status"] == "filled"
    ledger.close()


def test_sync_writes_the_reflection_for_a_close_that_fills_later(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    _traceable_entry(broker, ledger)  # fills at once, at 190
    broker.partial_fill_fraction = 0.0  # the close stays working, as on the real account
    close_id = _exit(broker, ledger).order.client_order_id
    assert ledger.unreflected_exits() == []  # nothing filled yet
    broker.fill_order(close_id, 192.0)

    synced = _sync(broker, ledger)

    assert synced.reflected == [close_id]
    [reflection] = ledger.reflections_for(AS_OF)
    assert reflection["content"]["close_reason"] == "trader_close"
    assert reflection["content"]["avg_exit_price"] == pytest.approx(192.0)
    assert _sync(broker, ledger).reflected == []  # once only
    ledger.close()


def test_sync_records_a_stop_that_fired_between_cycles(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    _traceable_entry(broker, ledger)
    broker.trigger_stop("entry-1::stop", 184.9)

    synced = _sync(broker, ledger)

    assert ledger.get_order("entry-1::stop")["status"] == "filled"
    assert synced.reflected == ["entry-1::stop"]
    [reflection] = ledger.reflections_for(AS_OF)
    assert reflection["content"]["close_reason"] == "invalidation"
    assert reflection["content"]["invalidation_respected"] is True
    assert _incidents(ledger, "warning") == ["stopout:NVDA"]
    assert "NVDA" in ledger.last_stopout_times()  # the cooldown applies
    ledger.close()


def test_sync_reflects_one_stop_out_once_however_many_legs_fired(tmp_path):
    """2026-10-16: NVDA's open (two tranches) and its add each carried a stop. All three
    fired together and the stop-out was recorded as three trades, each priced off the
    add's entry alone. It is one trade, priced from every entry."""
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    open_id = ledger.record_proposal(
        AS_OF, CYCLE, "NVDA", "open", "accepted", payload=_proposal().model_dump()
    )
    add_id = ledger.record_proposal(
        AS_OF, "cycle_2", "NVDA", "add", "accepted", payload=_proposal(action="add").model_dump()
    )
    entries = [
        ("entry-t1", 9.0, 217.61, open_id, 216.19),
        ("entry-t2", 9.0, 217.43, open_id, 216.20),
        ("entry-add", 4.0, 217.33, add_id, 216.46),
    ]
    for client_order_id, qty, price, proposal_id, _stop_fill in entries:
        submit_order_with_fault_isolation(
            _order(client_order_id=client_order_id, qty=qty, limit_price=price),
            broker,
            ledger,
            NOW,
            proposal_id=proposal_id,
        )
    for client_order_id, _qty, _price, _proposal_id, stop_fill in entries:
        broker.trigger_stop(f"{client_order_id}::stop", stop_fill)

    synced = _sync(broker, ledger)

    legs = [f"{client_order_id}::stop" for client_order_id, *_ in entries]
    assert sorted(synced.reflected) == sorted(legs)
    [reflection] = ledger.reflections_for(AS_OF)
    content = reflection["content"]
    entry_notional = sum(qty * price for _, qty, price, _, _ in entries)
    exit_notional = sum(qty * stop_fill for _, qty, _, _, stop_fill in entries)
    assert content["qty"] == 22.0
    assert content["avg_entry_price"] == pytest.approx(entry_notional / 22.0)
    assert content["pnl_gross"] == pytest.approx(exit_notional - entry_notional)
    assert sorted(content["client_order_ids"]) == sorted(legs)
    assert _incidents(ledger, "warning") == ["stopout:NVDA"]
    assert ledger.unreflected_exits() == []
    assert _sync(broker, ledger).reflected == []  # once only
    ledger.close()


class _LookupFails(FakeBroker):
    def get_order_by_client_id(self, client_order_id: str):
        raise BrokerError("simulated outage")


def test_sync_leaves_alone_an_order_it_cannot_look_up(tmp_path):
    """Unknown to the broker (a Stage 1 shadow row once Stage 2 starts), or a lookup that
    fails: nothing changes and the job carries on."""
    ledger = _ledger(tmp_path)
    ledger.record_order(
        "shadow-stop", "NVDA", "sell", status="new", broker_order_id="gone", leg="stop"
    )

    assert _sync(FakeBroker(), ledger) == OrderSync(updated=[], filled=[], reflected=[])
    assert _sync(_LookupFails(), ledger) == OrderSync(updated=[], filled=[], reflected=[])
    assert ledger.get_order("shadow-stop")["status"] == "new"
    ledger.close()


def test_a_short_entry_rests_its_stop_above_the_entry():
    order = build_entry_order(
        _proposal(side="sell", limit_price=190.0, invalidation_price=194.0),
        AS_OF,
        CYCLE,
        price=190.0,
        equity=100_000.0,
        config=IntradayConfig(),
    )
    assert order.side == "sell"
    assert order.order_class == "oto"
    assert order.stop_loss_price == 194.0
    assert order.limit_price < 190.0  # a marketable sell, collared below the price

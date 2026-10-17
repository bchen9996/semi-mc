"""`intraday/monitor.py`: deterministic invalidation/time-stop enforcement (S4,
`BRIEF_INTRADAY.md`, Phase 5). No model call anywhere in this module -- these tests
confirm a position closes (or doesn't) from broker/ledger state alone. Exercised only
against `FakeBroker`, matching this project's convention.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from semimc.config import IntradayConfig
from semimc.execution.broker import BrokerError, OrderRequest
from semimc.execution.fake_broker import FakeBroker
from semimc.intraday.ledger import IntradayLedger
from semimc.intraday.monitor import check_invalidation_and_time_stops
from semimc.intraday.proposals import TradeProposal

AS_OF = pd.Timestamp("2026-09-21")
CYCLE = "cycle_1"
NOW = dt.datetime(2026, 9, 21, 14, 30)


def _ledger(tmp_path) -> IntradayLedger:
    return IntradayLedger(tmp_path / "intraday.sqlite3")


def _open_position(
    broker: FakeBroker,
    ledger: IntradayLedger,
    symbol: str = "NVDA",
    side: str = "buy",
    qty: float = 10.0,
    price: float = 190.0,
    invalidation_price: float = 185.0,
    time_stop_minutes: int = 60,
    filled_at: dt.datetime | None = None,
    order_class: str = "simple",
) -> int:
    """Realistic setup: a filled entry order at the broker, recorded in the ledger with
    a traceable `proposal_id` whose payload carries the invalidation/time-stop levels --
    exactly what `check_invalidation_and_time_stops` needs to find."""
    proposal = TradeProposal(
        action="open",
        symbol=symbol,
        side=side,
        size_pct=0.05,
        limit_price=price,
        thesis="test thesis",
        invalidation_price=invalidation_price,
        time_stop_minutes=time_stop_minutes,
    )
    proposal_id = ledger.record_proposal(
        AS_OF, CYCLE, symbol, "open", "accepted", payload=proposal.model_dump()
    )
    order = broker.submit_order(
        OrderRequest(
            symbol=symbol,
            side=side,
            type="limit",
            time_in_force="day",
            client_order_id=f"entry-{symbol}",
            qty=qty,
            limit_price=price,
            order_class=order_class,
            stop_loss_price=invalidation_price if order_class == "oto" else None,
        )
    )
    ledger.record_order(
        order.client_order_id,
        symbol,
        side,
        status=order.status,
        qty=qty,
        broker_order_id=order.id,
        proposal_id=proposal_id,
        leg="entry",
    )
    if order.legs:
        leg = order.legs[0]
        ledger.record_order(
            leg.client_order_id,
            symbol,
            leg.side,
            status=leg.status,
            broker_order_id=leg.id,
            proposal_id=proposal_id,
            leg="stop",
        )
    ledger.record_fill(order.client_order_id, qty=qty, price=price, filled_at=filled_at or NOW)
    return proposal_id


def test_leaves_an_unbreached_long_position_alone(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    _open_position(broker, ledger, invalidation_price=185.0, time_stop_minutes=60)

    result = check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {"NVDA": 190.0}, AS_OF, CYCLE, NOW
    )

    assert result.closed == []
    assert result.still_open == ["NVDA"]
    assert broker.list_positions()[0].qty == 10.0
    ledger.close()


def test_closes_a_long_position_through_its_invalidation(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    _open_position(broker, ledger, side="buy", invalidation_price=185.0)

    result = check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {"NVDA": 184.0}, AS_OF, CYCLE, NOW
    )

    assert result.closed == ["NVDA"]
    assert broker.list_positions() == []  # fully closed
    incidents = ledger.incidents()
    assert any(i["message"] == "stopout:NVDA" for i in incidents)
    ledger.close()


def test_closes_a_short_position_through_its_invalidation(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    _open_position(broker, ledger, side="sell", invalidation_price=195.0)

    result = check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {"NVDA": 196.0}, AS_OF, CYCLE, NOW
    )

    assert result.closed == ["NVDA"]
    ledger.close()


def test_closes_on_an_elapsed_time_stop_without_touching_the_price(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    _open_position(
        broker,
        ledger,
        invalidation_price=185.0,
        time_stop_minutes=60,
        filled_at=NOW - dt.timedelta(minutes=90),
    )

    result = check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {"NVDA": 190.0}, AS_OF, CYCLE, NOW
    )

    assert result.closed == ["NVDA"]
    # A time-stop close is not a stop-out -- no cooldown incident.
    incidents = ledger.incidents()
    assert not any(i["message"] == "stopout:NVDA" for i in incidents)
    ledger.close()


def test_a_time_stop_not_yet_elapsed_leaves_the_position_open(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    _open_position(broker, ledger, time_stop_minutes=60, filled_at=NOW - dt.timedelta(minutes=30))

    result = check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {"NVDA": 190.0}, AS_OF, CYCLE, NOW
    )

    assert result.still_open == ["NVDA"]


def test_cancels_a_resting_oto_stop_leg_before_closing(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    _open_position(broker, ledger, invalidation_price=185.0, order_class="oto")
    [leg] = ledger.open_stop_legs_for("NVDA")  # sanity check on the test's own setup

    check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {"NVDA": 180.0}, AS_OF, CYCLE, NOW
    )

    assert broker.get_order_by_client_id(leg["client_order_id"]).status == "canceled"
    ledger.close()


def test_a_position_with_no_traceable_entry_is_left_alone(tmp_path):
    """A position exists at the broker but the ledger has no record of what opened it
    (e.g. pre-dates this ledger) -- never guess at an invalidation level."""
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    broker.submit_order(
        OrderRequest(
            symbol="AMD",
            side="buy",
            type="limit",
            time_in_force="day",
            client_order_id="untracked",
            qty=5.0,
            limit_price=100.0,
        )
    )

    result = check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {"AMD": 50.0}, AS_OF, CYCLE, NOW
    )

    assert result.closed == []
    assert result.still_open == ["AMD"]
    ledger.close()


def test_records_a_mechanical_reflection_when_closing_through_invalidation(tmp_path):
    """Phase 9a: a confirmed close writes a reflection via `reflect.py`, tagged
    `"invalidation"`. `FakeBroker(now=NOW)` -- not the bare default -- matters here
    specifically because this test is the first to actually read the *closing* order's
    own `filled_at`, which `submit_order_with_fault_isolation` derives from the broker's
    clock when the SDK response carries none; the existing tests in this file never
    cared, since none of them inspect a close's own fill timestamp."""
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _open_position(broker, ledger, side="buy", invalidation_price=185.0, price=190.0)

    check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {"NVDA": 184.0}, AS_OF, CYCLE, NOW
    )

    reflections = ledger.reflections_for(AS_OF)
    assert len(reflections) == 1
    content = reflections[0]["content"]
    assert content["close_reason"] == "invalidation"
    assert content["invalidation_respected"] is True
    assert content["category"] is None  # Phase 9b's eod_review fills this in
    ledger.close()


def test_missing_a_current_price_leaves_the_position_open_and_logs_an_incident(tmp_path):
    """The default, which the shadow book and the placebo keep: a `FakeBroker` has no
    market to fill a market order against."""
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    _open_position(broker, ledger, time_stop_minutes=60, filled_at=NOW - dt.timedelta(minutes=90))

    result = check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {}, AS_OF, CYCLE, NOW
    )

    assert result.still_open == ["NVDA"]
    assert any("no current price" in i["message"] for i in ledger.incidents())
    ledger.close()


# -- no current price on the real account (added 2026-10-15) --------------------------------


def _exit_rows(ledger: IntradayLedger) -> list[dict]:
    return [row for row in ledger.orders_for_proposal(1) if row["leg"] == "exit"]


def test_a_due_time_stop_with_no_price_closes_with_a_market_order(tmp_path):
    """2026-10-15, Stage 2's first day: every quote went stale and then missing from
    about 11:55 to 13:00 CT, and both positions stayed open past their time stops."""
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _open_position(
        broker,
        ledger,
        time_stop_minutes=60,
        filled_at=NOW - dt.timedelta(minutes=90),
        order_class="oto",
    )
    [stop] = ledger.open_stop_legs_for("NVDA")
    broker.partial_fill_fraction = 0.0  # a real market order fills a moment after it's sent

    result = check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {}, AS_OF, CYCLE, NOW, market_exit_without_price=True
    )

    assert result.closed == ["NVDA"]
    [exit_row] = _exit_rows(ledger)
    assert exit_row["exit_reason"] == "time_stop"
    exit_order = broker.get_order_by_client_id(exit_row["client_order_id"])
    assert exit_order.type == "market"
    assert exit_order.limit_price is None
    assert broker.get_order_by_client_id(stop["client_order_id"]).status == "canceled"
    incidents = ledger.incidents()
    assert any("closing with a market order" in i["message"] for i in incidents)
    assert not any(i["severity"] == "error" for i in incidents)
    ledger.close()


class _ClockUnreadable(FakeBroker):
    def get_clock(self):
        raise BrokerError("clock endpoint unreachable")


@pytest.mark.parametrize("closed_or_unreadable", ["closed", "unreadable"])
def test_no_market_exit_unless_the_clock_says_the_market_is_open(tmp_path, closed_or_unreadable):
    """Alpaca holds a market order sent while the market is closed and fills it at the
    next open, at whatever price that turns out to be."""
    ledger = _ledger(tmp_path)
    broker = _ClockUnreadable(now=NOW) if closed_or_unreadable == "unreadable" else FakeBroker()
    _open_position(broker, ledger, time_stop_minutes=60, filled_at=NOW - dt.timedelta(minutes=90))
    if closed_or_unreadable == "closed":
        broker.market_open = False

    result = check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {}, AS_OF, CYCLE, NOW, market_exit_without_price=True
    )

    assert result.still_open == ["NVDA"]
    assert _exit_rows(ledger) == []
    assert any(
        i["severity"] == "error" and "no current price" in i["message"] for i in ledger.incidents()
    )
    ledger.close()


# -- monitor cadence (found 2026-09-23) ----------------------------------------------------


def test_records_no_gap_for_the_days_first_call(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()

    result = check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {}, AS_OF, CYCLE, NOW
    )

    assert result.gap_seconds is None
    assert ledger.monitor_checks_for(AS_OF)[0]["cycle"] == CYCLE
    ledger.close()


def test_records_the_gap_since_the_previous_call_this_same_day(tmp_path):
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    check_invalidation_and_time_stops(IntradayConfig(), broker, ledger, {}, AS_OF, CYCLE, NOW)

    result = check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {}, AS_OF, "cycle_2", NOW + dt.timedelta(minutes=20)
    )

    assert result.gap_seconds == pytest.approx(1200.0)
    ledger.close()


class _RejectsExits(FakeBroker):
    """Accepts entries and stops; rejects every limit sell."""

    def submit_order(self, request: OrderRequest):
        if request.side == "sell" and request.type == "limit":
            raise BrokerError("simulated rejection")
        return super().submit_order(request)


def test_a_rejected_close_is_reported_as_failed_and_its_stop_put_back(tmp_path):
    """2026-10-03: a close that didn't go out was still reported as closed, with the
    position's stop already canceled."""
    ledger = _ledger(tmp_path)
    broker = _RejectsExits()
    _open_position(broker, ledger, invalidation_price=185.0, order_class="oto")

    result = check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {"NVDA": 184.0}, AS_OF, CYCLE, NOW
    )

    assert result.closed == []
    assert result.exits_failed == result.still_open == ["NVDA"]
    assert broker.list_positions()[0].qty == 10.0
    [restored] = ledger.open_stop_legs_for("NVDA")
    assert restored["client_order_id"] != "entry-NVDA::stop"
    ledger.close()


@pytest.mark.parametrize(("price", "reason"), [(184.0, "invalidation"), (190.0, "time_stop")])
def test_a_close_records_why_it_was_sent(tmp_path, price, reason):
    """Kept on the exit's row so its reflection can be written if the fill comes later."""
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    _open_position(
        broker, ledger, invalidation_price=185.0, filled_at=NOW - dt.timedelta(minutes=90)
    )

    check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {"NVDA": price}, AS_OF, CYCLE, NOW
    )

    [exit_row] = [row for row in ledger.orders_for_proposal(1) if row["leg"] == "exit"]
    assert exit_row["exit_reason"] == reason
    ledger.close()


# -- the time stop counts from the proposal's first fill (owner decision 2026-10-16) --------


def test_a_later_tranche_does_not_restart_the_time_stop(tmp_path):
    """2026-10-15: NVDA's tranches filled at 10:01 and 10:30 CT, and its 90-minute time
    stop came due at 12:01 instead of 11:31."""
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    proposal_id = _open_position(
        broker, ledger, time_stop_minutes=90, filled_at=NOW - dt.timedelta(minutes=95)
    )
    tranche_2 = broker.submit_order(
        OrderRequest(
            symbol="NVDA",
            side="buy",
            type="limit",
            time_in_force="day",
            client_order_id="entry-NVDA-t2",
            qty=10.0,
            limit_price=190.0,
        )
    )
    ledger.record_order(
        tranche_2.client_order_id,
        "NVDA",
        "buy",
        status=tranche_2.status,
        qty=10.0,
        broker_order_id=tranche_2.id,
        proposal_id=proposal_id,
        leg="entry",
        tranche_index=2,
        tranche_total=2,
    )
    ledger.record_fill(
        tranche_2.client_order_id, qty=10.0, price=190.0, filled_at=NOW - dt.timedelta(minutes=65)
    )

    result = check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {"NVDA": 190.0}, AS_OF, CYCLE, NOW
    )

    assert result.closed == ["NVDA"]  # 95 minutes since the first tranche, not 65
    ledger.close()


def test_an_add_restarts_the_time_stop(tmp_path):
    """An add is a new decision with its own time stop."""
    ledger = _ledger(tmp_path)
    broker = FakeBroker()
    _open_position(broker, ledger, time_stop_minutes=90, filled_at=NOW - dt.timedelta(minutes=95))
    add = TradeProposal(
        action="add",
        symbol="NVDA",
        side="buy",
        size_pct=0.02,
        limit_price=190.0,
        thesis="adding on strength",
        invalidation_price=185.0,
        time_stop_minutes=90,
    )
    add_id = ledger.record_proposal(AS_OF, "cycle_2", "NVDA", "add", "accepted", add.model_dump())
    order = broker.submit_order(
        OrderRequest(
            symbol="NVDA",
            side="buy",
            type="limit",
            time_in_force="day",
            client_order_id="add-NVDA",
            qty=4.0,
            limit_price=190.0,
        )
    )
    ledger.record_order(
        order.client_order_id,
        "NVDA",
        "buy",
        status=order.status,
        qty=4.0,
        broker_order_id=order.id,
        proposal_id=add_id,
        leg="entry",
    )
    ledger.record_fill(
        order.client_order_id, qty=4.0, price=190.0, filled_at=NOW - dt.timedelta(minutes=30)
    )

    result = check_invalidation_and_time_stops(
        IntradayConfig(), broker, ledger, {"NVDA": 190.0}, AS_OF, CYCLE, NOW
    )

    assert result.still_open == ["NVDA"]  # 30 minutes since the add's fill
    ledger.close()

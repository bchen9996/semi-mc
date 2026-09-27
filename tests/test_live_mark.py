"""``live/mark.py``: the fill-polling fallback, stale-order pruning, and position
reconciliation. All against ``FakeBroker`` -- ``partial_fill_fraction`` and direct mutation
of its private ``_orders`` dict (a test double; white-box is fine here) simulate a fill
arriving between two ``mark`` calls, something no single ``submit_order`` call can produce
on its own.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pandas as pd
import pytest

from semimc.config import PaperSleeveConfig
from semimc.execution.broker import Order, OrderRequest
from semimc.execution.fake_broker import FakeBroker
from semimc.live.ledger import Ledger
from semimc.live.mark import _CarriedPosition, run_mark

SUBMITTED_AT = dt.datetime(2026, 1, 5, 10, 0)


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    yield ledger
    ledger.close()


def _tradeable_config(config, account_number="FAKE-PAPER-0001"):
    paper = config.paper.model_copy(
        update={
            "sleeves": {**config.paper.sleeves, "s0": PaperSleeveConfig(trading_enabled=True)},
            "registered_accounts": [account_number],
        }
    )
    return config.model_copy(update={"paper": paper})


def _request(symbol="NVDA", qty=10.0, client_order_id="s0-nvda-1") -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side="buy",
        type="limit",
        time_in_force="day",
        client_order_id=client_order_id,
        qty=qty,
        limit_price=100.0,
    )


def test_a_new_partial_fill_records_only_the_delta(config, ledger):
    cfg = _tradeable_config(config)
    broker = FakeBroker(partial_fill_fraction=0.5, now=SUBMITTED_AT)
    request = _request()
    submitted = broker.submit_order(request)
    assert submitted.status == "partially_filled"
    assert submitted.filled_qty == pytest.approx(5.0)

    ledger.record_order("s0", "hash-1", request, submitted)
    ledger.record_fill(request.client_order_id, qty=5.0, price=100.0, filled_at=SUBMITTED_AT)

    # A later poll finds the order fully filled -- FakeBroker can't produce this from a
    # second submit_order call (duplicate ids return the original unchanged), so simulate
    # what a real broker's own fill progression would look like.
    fully_filled = dataclasses.replace(
        submitted, status="filled", filled_qty=10.0, filled_avg_price=100.0
    )
    broker._orders[request.client_order_id] = fully_filled

    now = SUBMITTED_AT + dt.timedelta(minutes=2)
    result = run_mark("s0", cfg, broker, ledger, pd.Timestamp("2026-01-05"), now)

    assert result.fills_recorded == 1
    fills = ledger.fills_for_order(request.client_order_id)
    assert len(fills) == 2
    assert fills[-1]["qty"] == pytest.approx(5.0)  # the delta, not the cumulative 10.0
    assert ledger.get_order(request.client_order_id)["status"] == "filled"
    assert ledger.open_orders("s0") == []


def test_a_stale_unfilled_order_is_canceled_for_reprice(config, ledger):
    cfg = _tradeable_config(config)
    broker = FakeBroker(partial_fill_fraction=0.0, now=SUBMITTED_AT)  # stays "accepted"
    request = _request()
    submitted = broker.submit_order(request)
    assert submitted.status == "accepted"
    ledger.record_order("s0", "hash-1", request, submitted)

    # reprice_interval_minutes defaults to 15 -- 30 minutes later is well past it.
    now = SUBMITTED_AT + dt.timedelta(minutes=30)
    result = run_mark("s0", cfg, broker, ledger, pd.Timestamp("2026-01-05"), now)

    assert request.client_order_id in result.orders_canceled_for_reprice
    assert broker.get_order_by_client_id(request.client_order_id).status == "canceled"
    assert ledger.get_order(request.client_order_id)["status"] == "canceled"


def test_a_cancel_race_does_not_abort_the_rest_of_the_run(config, ledger, monkeypatch):
    """Phase 8: the order fills at the broker in the moment between mark's poll and its own
    cancel-for-staleness call -- a real, narrow race. cancel_order raising must not abort
    reconciliation/the snapshot, or leave the ledger thinking the order is still open."""
    cfg = _tradeable_config(config)
    broker = FakeBroker(partial_fill_fraction=0.0, now=SUBMITTED_AT)  # stays "accepted"
    request = _request()
    submitted = broker.submit_order(request)
    ledger.record_order("s0", "hash-1", request, submitted)

    def _cancel_raises(order_id: str) -> None:
        raise ValueError(f"order {order_id} is already filled")

    monkeypatch.setattr(broker, "cancel_order", _cancel_raises)

    now = SUBMITTED_AT + dt.timedelta(minutes=30)
    result = run_mark("s0", cfg, broker, ledger, pd.Timestamp("2026-01-05"), now)

    assert result.orders_canceled_for_reprice == []
    incidents = ledger.incidents("s0")
    assert any(i["severity"] == "error" and "cancel failed" in i["message"] for i in incidents)
    assert ledger.latest_snapshot("s0") is not None  # the run still finishes


def test_an_order_still_within_the_reprice_window_is_left_alone(config, ledger):
    cfg = _tradeable_config(config)
    broker = FakeBroker(partial_fill_fraction=0.0, now=SUBMITTED_AT)
    request = _request()
    submitted = broker.submit_order(request)
    ledger.record_order("s0", "hash-1", request, submitted)

    now = SUBMITTED_AT + dt.timedelta(minutes=5)
    result = run_mark("s0", cfg, broker, ledger, pd.Timestamp("2026-01-05"), now)

    assert result.orders_canceled_for_reprice == []
    assert broker.get_order_by_client_id(request.client_order_id).status == "accepted"


def test_a_position_mismatch_is_caught_by_reconciliation(config, ledger):
    cfg = _tradeable_config(config)
    broker = FakeBroker(now=SUBMITTED_AT)  # no positions at all in the broker

    # The ledger thinks s0 bought 10 NVDA, but the broker has never seen that order --
    # exactly the kind of drift reconciliation exists to catch.
    request = _request()
    fake_order = Order(
        id="fake-broker-order-1",
        client_order_id=request.client_order_id,
        symbol="NVDA",
        side="buy",
        type="limit",
        time_in_force="day",
        status="filled",
        qty=10.0,
        notional=None,
        filled_qty=10.0,
        filled_avg_price=100.0,
        submitted_at=SUBMITTED_AT,
        filled_at=SUBMITTED_AT,
    )
    ledger.record_order("s0", "hash-1", request, fake_order)
    ledger.record_fill(request.client_order_id, qty=10.0, price=100.0, filled_at=SUBMITTED_AT)

    now = SUBMITTED_AT + dt.timedelta(minutes=1)
    result = run_mark("s0", cfg, broker, ledger, pd.Timestamp("2026-01-05"), now)

    assert result.reconciliation_status == "discrepancy"
    assert result.discrepancies == [{"symbol": "NVDA", "expected": 10.0, "actual": 0.0}]
    incidents = ledger.incidents("s0")
    assert any(i["severity"] == "error" for i in incidents)


def test_mark_reconciles_against_every_sleeves_fills_not_just_this_ones(config, ledger):
    """Found 2026-09-26: s0 and s3 share one real Alpaca paper account, so once s3 held its
    first real position, mark(s0) compared s0's own (always empty) fills against the whole
    account's s3-driven holdings and failed on every single run. marking s0 while only s3
    has ever traded must reconcile clean."""
    cfg = _tradeable_config(config)
    broker = FakeBroker(now=SUBMITTED_AT)
    request = _request(client_order_id="s3-nvda-1")
    submitted = broker.submit_order(request)
    assert submitted.status == "filled"  # FakeBroker's default: fully filled immediately
    ledger.record_order("s3", "hash-1", request, submitted)
    ledger.record_fill(
        request.client_order_id,
        qty=submitted.filled_qty,
        price=submitted.filled_avg_price,
        filled_at=SUBMITTED_AT,
    )

    now = SUBMITTED_AT + dt.timedelta(minutes=1)
    result = run_mark("s0", cfg, broker, ledger, pd.Timestamp("2026-01-05"), now)

    assert result.reconciliation_status == "ok"
    assert result.discrepancies == []


def test_mark_carries_forward_a_disabled_sleeves_own_snapshot_not_the_accounts(config, ledger):
    """Found 2026-09-26: once the reconciliation fix above stopped crashing mark(s0) before
    it ever reached this function's own final snapshot step, that step would otherwise still
    record the *account's* real, s3-driven equity/cash/positions under s0's own label the
    moment mark(s0) next succeeded -- corrupting the s0-vs-s3 comparison this shared account
    exists to make. Unlike the reconciliation test above, s0 here has never placed an order
    at all -- the actual discriminator is s0's own (empty) fills versus the account-wide
    total, not `trading_enabled` (see the wind-down test below for why that distinction
    matters), so this exercises the real branch rather than a same-config stand-in for it.

    s0's own seeded prior snapshot deliberately holds a position (AAPL) -- not today's real
    production history (s0 has always been flat), but chosen to prove the fix carries a
    disabled sleeve's own state forward *verbatim*, not just resets it to flat. Blanking
    positions on carry-forward would happen to look right for today's actual s0 (it really
    is flat) while being wrong in general, and this test would not catch that mistake if it
    only used an already-flat starting snapshot."""
    paper = config.paper.model_copy(
        update={"sleeves": {**config.paper.sleeves, "s3": PaperSleeveConfig(trading_enabled=True)}}
    )
    cfg = config.model_copy(update={"paper": paper})
    broker = FakeBroker(now=SUBMITTED_AT)
    request = _request(client_order_id="s3-nvda-1")
    submitted = broker.submit_order(request)
    ledger.record_order("s3", "hash-1", request, submitted)
    ledger.record_fill(
        request.client_order_id,
        qty=submitted.filled_qty,
        price=submitted.filled_avg_price,
        filled_at=SUBMITTED_AT,
    )
    ledger.record_snapshot(
        "s0",
        pd.Timestamp("2026-01-04"),
        105_000.0,
        95_000.0,
        {"AAPL": _CarriedPosition(qty=5.0, market_value=850.0)},
    )

    now = SUBMITTED_AT + dt.timedelta(minutes=1)
    run_mark("s0", cfg, broker, ledger, pd.Timestamp("2026-01-05"), now)

    snap = ledger.latest_snapshot("s0")
    assert snap["as_of"] == str(pd.Timestamp("2026-01-05"))
    assert snap["equity"] == pytest.approx(105_000.0)
    assert snap["cash"] == pytest.approx(95_000.0)
    assert snap["positions"] == {"AAPL": {"qty": 5.0, "market_value": 850.0}}
    # And not, e.g., s3's real NVDA fill from this same run -- s0's carried-forward snapshot
    # must not pick up any trace of the other sleeve's actual activity.
    assert "NVDA" not in snap["positions"]


def test_mark_skips_the_snapshot_for_a_disabled_sleeve_with_no_prior_history(config, ledger):
    """No previous snapshot to carry forward, and some other sleeve has already traded here
    (so this sleeve's own fills diverge from the account-wide total, same as every other
    disabled-sleeve test) -- there is nothing genuine to report for this sleeve, so record
    nothing rather than fabricate a starting figure this module has no config source for.

    Deliberately distinct from a genuinely fresh, untouched account (nobody has traded at
    all): there, this sleeve's own empty fills *do* equal the account-wide total (also
    empty), so the fix correctly reads the broker directly instead -- a real, brand new
    Alpaca paper account really does start flat and it is truthful to record that, for
    whichever sleeve asks first. That path already has coverage
    (test_mark_always_records_a_fresh_snapshot); this test is specifically the "someone
    else already traded" case."""
    paper = config.paper.model_copy(
        update={"sleeves": {**config.paper.sleeves, "s3": PaperSleeveConfig(trading_enabled=True)}}
    )
    cfg = config.model_copy(update={"paper": paper})
    broker = FakeBroker(now=SUBMITTED_AT)
    request = _request(client_order_id="s3-nvda-1")
    submitted = broker.submit_order(request)
    ledger.record_order("s3", "hash-1", request, submitted)
    ledger.record_fill(
        request.client_order_id,
        qty=submitted.filled_qty,
        price=submitted.filled_avg_price,
        filled_at=SUBMITTED_AT,
    )

    now = SUBMITTED_AT + dt.timedelta(minutes=1)
    run_mark("s0", cfg, broker, ledger, pd.Timestamp("2026-01-05"), now)

    assert ledger.latest_snapshot("s0") is None


def test_mark_keeps_reading_the_broker_for_a_sole_trader_even_once_disabled(config, ledger):
    """A config flag answers "may this sleeve place new orders," not "does the account's
    real state still belong to it." A sleeve disabled at a review checkpoint
    (`paper.review_months`) while still the account's only historical trader must keep
    reading the broker directly -- its real position is still there and its market value
    still moves day to day. Freezing it at whatever its last snapshot showed the moment it
    was disabled would be a *new* staleness bug traded for the one just fixed."""
    paper = config.paper.model_copy(
        update={"sleeves": {**config.paper.sleeves, "s3": PaperSleeveConfig(trading_enabled=False)}}
    )
    cfg = config.model_copy(update={"paper": paper})
    broker = FakeBroker(now=SUBMITTED_AT)
    request = _request(client_order_id="s3-nvda-1")
    submitted = broker.submit_order(request)
    ledger.record_order("s3", "hash-1", request, submitted)
    ledger.record_fill(
        request.client_order_id,
        qty=submitted.filled_qty,
        price=submitted.filled_avg_price,
        filled_at=SUBMITTED_AT,
    )
    # A stale prior snapshot -- if the fix wrongly fell back to carry-forward here, this is
    # the (wrong) value the assertion below would catch it returning instead.
    ledger.record_snapshot("s3", pd.Timestamp("2026-01-04"), 100_000.0, 100_000.0, {})

    now = SUBMITTED_AT + dt.timedelta(minutes=1)
    run_mark("s3", cfg, broker, ledger, pd.Timestamp("2026-01-05"), now)

    snap = ledger.latest_snapshot("s3")
    account = broker.get_account()
    # A carry-forward would have reproduced the stale seeded snapshot exactly: equity
    # 100_000.0 and an empty positions dict. Reading the broker directly instead shows the
    # real fill (NVDA) and the account's actual current equity, whatever that is.
    assert snap["equity"] == pytest.approx(account.equity)
    assert "NVDA" in snap["positions"]


def test_an_open_order_missing_at_the_broker_is_surfaced_and_logged(config, ledger):
    """Phase 7: distinct from a *position* reconciliation mismatch -- this is the ledger
    believing an order is still open when the broker has never heard of it at all (e.g. a
    ledger restored from an older commit). Must be visible on the result, not just logged,
    so the CLI can fail the job on it."""
    cfg = _tradeable_config(config)
    broker = FakeBroker(now=SUBMITTED_AT)  # never told about this order

    request = _request()
    phantom_order = Order(
        id="phantom-broker-order-1",
        client_order_id=request.client_order_id,
        symbol="NVDA",
        side="buy",
        type="limit",
        time_in_force="day",
        status="accepted",
        qty=10.0,
        notional=None,
        filled_qty=0.0,
        filled_avg_price=None,
        submitted_at=SUBMITTED_AT,
        filled_at=None,
    )
    ledger.record_order("s0", "hash-1", request, phantom_order)

    now = SUBMITTED_AT + dt.timedelta(minutes=1)
    result = run_mark("s0", cfg, broker, ledger, pd.Timestamp("2026-01-05"), now)

    assert result.orders_not_found == [request.client_order_id]
    incidents = ledger.incidents("s0")
    assert any(
        i["severity"] == "error" and "not found at broker" in i["message"] for i in incidents
    )


def test_mark_always_records_a_fresh_snapshot(config, ledger):
    cfg = _tradeable_config(config)
    broker = FakeBroker(now=SUBMITTED_AT)
    run_mark("s0", cfg, broker, ledger, pd.Timestamp("2026-01-05"), SUBMITTED_AT)
    assert ledger.latest_snapshot("s0") is not None


class _AlpacaTimestampBroker(FakeBroker):
    """FakeBroker, but reporting `submitted_at` the way the real Alpaca adapter does:
    aware UTC (`execution/alpaca_broker.py`'s docstring). FakeBroker's own timestamps are
    naive, which is what hid the bug below from every other test in this file."""

    def get_order_by_client_id(self, client_order_id):
        order = super().get_order_by_client_id(client_order_id)
        if order is None or order.submitted_at is None:
            return order
        local = order.submitted_at.replace(tzinfo=_EASTERN)
        return dataclasses.replace(order, submitted_at=local.astimezone(dt.UTC))


_EASTERN = __import__("zoneinfo").ZoneInfo("America/New_York")


@pytest.mark.parametrize(("minutes_later", "canceled"), [(30, True), (5, False)])
def test_reprice_compares_alpacas_aware_utc_submitted_at_against_a_naive_local_now(
    config, ledger, minutes_later, canceled
):
    # Found 2026-09-27: `cli.py::_paper_now` is naive local (Eastern) time, while a real
    # Alpaca order's `submitted_at` is aware UTC -- `now - order.submitted_at` raised
    # TypeError whenever a real order was still open at mark time.
    cfg = _tradeable_config(config)
    broker = _AlpacaTimestampBroker(partial_fill_fraction=0.0, now=SUBMITTED_AT)
    request = _request()
    ledger.record_order("s0", "hash-1", request, broker.submit_order(request))

    now = SUBMITTED_AT + dt.timedelta(minutes=minutes_later)
    result = run_mark("s0", cfg, broker, ledger, pd.Timestamp("2026-01-05"), now)

    assert (request.client_order_id in result.orders_canceled_for_reprice) is canceled

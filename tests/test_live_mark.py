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
from semimc.live.mark import run_mark

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

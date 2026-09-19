"""`live/ledger.py`: the SQLite record every live paper-trading job (a later phase) reads
from and writes to. Tested as a plain SQLite file in `tmp_path` -- no network, matching
this project's convention.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pandas as pd
import pytest

from semimc.execution.broker import Order, OrderRequest, Position
from semimc.live.ledger import Ledger
from semimc.strategy.decide import Decision


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "ledger.sqlite3") as ledger:
        yield ledger


def _decision(as_of="2026-01-05", sleeve="s0") -> Decision:
    return Decision(
        as_of=pd.Timestamp(as_of),
        sleeve=sleeve,
        weights=pd.Series({"NVDA": 0.5, "AMD": 0.5}),
        diagnostics={"kind": "equal_weight"},
    )


def _order_request(client_order_id: str, symbol: str = "NVDA") -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side="buy",
        type="limit",
        time_in_force="day",
        client_order_id=client_order_id,
        qty=10.0,
        limit_price=100.0,
    )


def _order(client_order_id: str, status: str = "new", symbol: str = "NVDA") -> Order:
    return Order(
        id=f"broker-{client_order_id}",
        client_order_id=client_order_id,
        symbol=symbol,
        side="buy",
        type="limit",
        time_in_force="day",
        status=status,
        qty=10.0,
        notional=None,
        submitted_at=dt.datetime(2026, 1, 5, 10, 0),
    )


# -- decisions ------------------------------------------------------------------------


def test_record_and_get_decision_round_trips(ledger):
    decision = _decision()
    ledger.record_decision(decision, data_vintage="2026-01-05-close", git_commit="abc123")
    row = ledger.get_decision("s0", pd.Timestamp("2026-01-05"))
    assert row["sleeve"] == "s0"
    assert row["weights"] == {"NVDA": 0.5, "AMD": 0.5}
    assert row["diagnostics"] == {"kind": "equal_weight"}
    assert row["data_vintage"] == "2026-01-05-close"
    assert row["git_commit"] == "abc123"


def test_get_decision_returns_none_when_absent(ledger):
    assert ledger.get_decision("s0", pd.Timestamp("2026-01-05")) is None


def test_record_decision_is_idempotent_by_snapshot_hash(ledger):
    decision = _decision()
    first_id = ledger.record_decision(decision)
    second_id = ledger.record_decision(decision)
    assert first_id == second_id
    count = ledger._conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert count == 1


def test_a_different_decision_same_day_is_a_new_row(ledger):
    """Different weights -> different snapshot_hash -> a genuinely new decision, not
    treated as a duplicate of the first."""
    first = _decision()
    second = Decision(
        as_of=first.as_of, sleeve=first.sleeve, weights=pd.Series({"NVDA": 0.6, "AMD": 0.4})
    )
    ledger.record_decision(first)
    ledger.record_decision(second)
    count = ledger._conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert count == 2


# -- orders ---------------------------------------------------------------------------


def test_record_order_and_get_order_round_trips(ledger):
    request = _order_request("s0-20260105-NVDA-abc")
    order = _order("s0-20260105-NVDA-abc", status="new")
    ledger.record_order("s0", "hash123", request, order)
    row = ledger.get_order("s0-20260105-NVDA-abc")
    assert row["symbol"] == "NVDA"
    assert row["status"] == "new"
    assert row["qty"] == pytest.approx(10.0)


def test_resubmitting_the_same_client_order_id_updates_in_place(ledger):
    request = _order_request("s0-20260105-NVDA-abc")
    ledger.record_order("s0", "hash123", request, _order("s0-20260105-NVDA-abc", status="new"))
    ledger.record_order("s0", "hash123", request, _order("s0-20260105-NVDA-abc", status="filled"))
    row = ledger.get_order("s0-20260105-NVDA-abc")
    assert row["status"] == "filled"
    count = ledger._conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert count == 1


def test_open_orders_filters_to_unsettled_statuses(ledger):
    ledger.record_order(
        "s0", "hash123", _order_request("open-1"), _order("open-1", status="accepted")
    )
    ledger.record_order(
        "s0", "hash123", _order_request("done-1", symbol="AMD"), _order("done-1", status="filled")
    )
    open_ids = {row["client_order_id"] for row in ledger.open_orders("s0")}
    assert open_ids == {"open-1"}


# -- fills ----------------------------------------------------------------------------


def test_fills_for_order_returns_every_fill_in_order(ledger):
    ledger.record_fill("order-1", qty=4.0, price=99.5, filled_at=dt.datetime(2026, 1, 5, 10, 1))
    ledger.record_fill("order-1", qty=6.0, price=99.8, filled_at=dt.datetime(2026, 1, 5, 10, 5))
    fills = ledger.fills_for_order("order-1")
    assert [f["qty"] for f in fills] == [4.0, 6.0]


# -- snapshots ------------------------------------------------------------------------


def test_record_snapshot_and_latest_snapshot(ledger):
    positions = {
        "NVDA": Position(
            symbol="NVDA", qty=10.0, side="long", market_value=1000.0, avg_entry_price=100.0
        )
    }
    ledger.record_snapshot(
        "s0", pd.Timestamp("2026-01-05"), equity=10_500.0, cash=9_500.0, positions=positions
    )
    ledger.record_snapshot(
        "s0", pd.Timestamp("2026-01-06"), equity=10_600.0, cash=9_600.0, positions=positions
    )
    latest = ledger.latest_snapshot("s0")
    assert latest["as_of"] == "2026-01-06 00:00:00"
    assert latest["equity"] == pytest.approx(10_600.0)
    assert latest["positions"]["NVDA"]["qty"] == pytest.approx(10.0)


def test_record_snapshot_upserts_same_day(ledger):
    positions = {
        "NVDA": Position(
            symbol="NVDA", qty=10.0, side="long", market_value=1000.0, avg_entry_price=100.0
        )
    }
    ledger.record_snapshot(
        "s0", pd.Timestamp("2026-01-05"), equity=10_000.0, cash=9_000.0, positions=positions
    )
    ledger.record_snapshot(
        "s0", pd.Timestamp("2026-01-05"), equity=10_100.0, cash=9_100.0, positions=positions
    )
    count = ledger._conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert count == 1
    assert ledger.latest_snapshot("s0")["equity"] == pytest.approx(10_100.0)


# -- adjustments, reconciliation, incidents --------------------------------------------


def test_record_adjustment(ledger):
    ledger.record_adjustment(
        "s0", pd.Timestamp("2026-01-05"), kind="fee", note="ACH fee", amount=-1.5
    )
    row = ledger._conn.execute("SELECT * FROM adjustments").fetchone()
    assert row["kind"] == "fee"
    assert row["amount"] == pytest.approx(-1.5)


def test_record_reconciliation(ledger):
    ledger.record_reconciliation(
        "s0", pd.Timestamp("2026-01-05"), status="mismatch", discrepancies=[{"symbol": "NVDA"}]
    )
    row = ledger._conn.execute("SELECT * FROM reconciliations").fetchone()
    assert row["status"] == "mismatch"
    assert "NVDA" in row["discrepancies_json"]


def test_record_and_list_incidents(ledger):
    ledger.record_incident("warning", "kill switch triggered", sleeve="s0")
    ledger.record_incident("info", "startup ok")
    assert [i["severity"] for i in ledger.incidents("s0")] == ["warning"]
    assert len(ledger.incidents()) == 2


def test_ledger_context_manager_closes_the_connection(tmp_path):
    with Ledger(tmp_path / "closes.sqlite3") as ledger:
        ledger.record_incident("info", "hello")
    with pytest.raises(sqlite3.ProgrammingError):
        ledger.incidents()

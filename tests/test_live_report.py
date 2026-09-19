"""``live/report.py``: reading the ledger back into a summary. No broker involved --
seeded entirely through the ledger's own write methods.
"""

from __future__ import annotations

import pandas as pd
import pytest

from semimc.execution.broker import Order, OrderRequest, Position
from semimc.live.ledger import Ledger
from semimc.live.report import build_report, render_markdown
from semimc.strategy.decide import decide_s0


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    yield ledger
    ledger.close()


def test_report_with_no_history_is_still_renderable(ledger):
    report = build_report("s0", ledger)
    assert report.latest_snapshot is None
    assert report.equity_history.empty
    assert report.recent_orders == []
    assert report.last_reconciliation is None

    markdown = render_markdown(report)
    assert "No snapshot recorded yet." in markdown
    assert "No reconciliation recorded yet." in markdown


def test_report_reflects_a_seeded_ledger(ledger):
    decision = decide_s0(["NVDA", "AMD"], pd.Timestamp("2026-01-05"))
    ledger.record_decision(decision)

    request = OrderRequest(
        symbol="NVDA",
        side="buy",
        type="limit",
        time_in_force="day",
        client_order_id="s0-nvda-1",
        qty=10.0,
        limit_price=100.0,
    )
    order = Order(
        id="broker-1",
        client_order_id="s0-nvda-1",
        symbol="NVDA",
        side="buy",
        type="limit",
        time_in_force="day",
        status="filled",
        qty=10.0,
        notional=None,
        filled_qty=10.0,
        filled_avg_price=100.0,
    )
    ledger.record_order("s0", decision.snapshot_hash, request, order)
    ledger.record_fill("s0-nvda-1", qty=10.0, price=100.0, filled_at=pd.Timestamp("2026-01-05"))
    ledger.record_snapshot(
        "s0",
        pd.Timestamp("2026-01-05"),
        equity=10_500.0,
        cash=9_500.0,
        positions={
            "NVDA": Position(
                symbol="NVDA", qty=10.0, side="long", market_value=1000.0, avg_entry_price=100.0
            )
        },
    )
    ledger.record_reconciliation("s0", pd.Timestamp("2026-01-05"), "ok", [])
    ledger.record_incident("warning", "test incident", "s0")

    report = build_report("s0", ledger)
    assert report.latest_snapshot["equity"] == pytest.approx(10_500.0)
    assert len(report.equity_history) == 1
    assert len(report.recent_orders) == 1
    assert len(report.recent_fills) == 1
    assert report.last_reconciliation["status"] == "ok"
    assert len(report.open_incidents) == 1

    markdown = render_markdown(report)
    assert "NVDA" in markdown
    assert "10,500.00" in markdown
    assert "test incident" in markdown

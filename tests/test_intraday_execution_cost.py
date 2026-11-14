"""`intraday/execution_cost.py`: what S4's fills paid against the quote midpoint as each
order went in. Measured only. Every expected figure is written out by hand in the test."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from semimc.intraday.execution_cost import (
    price_execution,
    record_execution_costs,
    summarize_execution,
)
from semimc.intraday.ledger import IntradayLedger

AS_OF = pd.Timestamp("2026-11-16")
T0 = dt.datetime(2026, 11, 16, 15, 0, tzinfo=dt.UTC)


def _ledger(tmp_path) -> IntradayLedger:
    return IntradayLedger(tmp_path / "intraday.sqlite3")


def _order(ledger, order_id, side, leg, qty, price, submitted_at, filled_at, symbol="NVDA"):
    ledger.record_order(
        order_id, symbol, side, status="filled", qty=qty, leg=leg, submitted_at=submitted_at
    )
    ledger.record_fill(order_id, qty=qty, price=price, filled_at=filled_at)


def _exit_row(ledger, ids=("exit-1",), as_of=AS_OF):
    content = {"side": "buy", "qty": 10.0, "avg_entry_price": 100.0, "client_order_ids": list(ids)}
    reflection_id = ledger.record_reflection(as_of, "NVDA", content, client_order_id=ids[0])
    return next(r for r in ledger.all_reflections() if r["id"] == reflection_id)


class _Quotes:
    """`AlpacaMarketDataClient.midpoint_before`, from a table of (symbol, moment) -> mid."""

    def __init__(self, mids: dict[dt.datetime, float]) -> None:
        self.mids = mids
        self.requests: list[tuple[str, dt.datetime]] = []

    def midpoint_before(self, symbol: str, at: dt.datetime) -> float | None:
        self.requests.append((symbol, at))
        return self.mids.get(at)

    def __call__(self, symbol: str, at: dt.datetime) -> float | None:
        return self.midpoint_before(symbol, at)


def test_a_round_trip_pays_what_its_fills_crossed_against_the_midpoint(tmp_path):
    ledger = _ledger(tmp_path)
    t1 = T0 + dt.timedelta(hours=1)
    _order(ledger, "entry-1", "buy", "entry", 10.0, 100.05, T0, T0)
    _order(ledger, "exit-1", "sell", "exit", 10.0, 100.95, t1, t1)

    block = price_execution(ledger, _exit_row(ledger), _Quotes({T0: 100.0, t1: 101.0}))

    # The buy filled 0.05 above a 100.00 midpoint: 5 bps. The sell filled 0.05 below a
    # 101.00 midpoint: 0.05 / 101 = 4.95 bps. Both are costs, so both are positive.
    assert block["entry_bps"] == pytest.approx(5.0)
    assert block["exit_bps"] == pytest.approx(0.05 / 101.0 * 10_000)
    assert block["per_side_bps"] == pytest.approx((5.0 + 0.05 / 101.0 * 10_000) / 2)
    ledger.close()


def test_a_fill_inside_the_spread_is_a_negative_cost(tmp_path):
    ledger = _ledger(tmp_path)
    t1 = T0 + dt.timedelta(hours=1)
    _order(ledger, "entry-1", "buy", "entry", 10.0, 99.98, T0, T0)
    _order(ledger, "exit-1", "sell", "exit", 10.0, 101.0, t1, t1)

    block = price_execution(ledger, _exit_row(ledger), _Quotes({T0: 100.0, t1: 101.0}))

    # Bought 0.02 below the midpoint: -2 bps, an improvement. Sold at the midpoint: 0.
    assert block["entry_bps"] == pytest.approx(-2.0)
    assert block["exit_bps"] == pytest.approx(0.0)
    ledger.close()


def test_a_resting_entry_is_measured_from_when_it_went_in_not_when_it_filled(tmp_path):
    ledger = _ledger(tmp_path)
    filled = T0 + dt.timedelta(minutes=3)
    t1 = T0 + dt.timedelta(hours=1)
    _order(ledger, "entry-1", "buy", "entry", 10.0, 100.20, T0, filled)
    _order(ledger, "exit-1", "sell", "exit", 10.0, 101.0, t1, t1)
    quotes = _Quotes({T0: 100.0, filled: 100.20, t1: 101.0})

    block = price_execution(ledger, _exit_row(ledger), quotes)

    # The market moved up 20 bps while the order waited: that drift is part of its cost.
    assert block["entry_bps"] == pytest.approx(20.0)
    assert ("NVDA", filled) not in quotes.requests
    ledger.close()


def test_a_stop_is_measured_from_when_it_filled_not_when_it_was_placed(tmp_path):
    ledger = _ledger(tmp_path)
    stopped = T0 + dt.timedelta(minutes=40)
    _order(ledger, "entry-1", "buy", "entry", 10.0, 100.0, T0, T0)
    # Placed with the entry at T0, triggered 40 minutes later as the price fell.
    _order(ledger, "stop-1", "sell", "stop", 10.0, 97.90, T0, stopped)
    quotes = _Quotes({T0: 100.0, stopped: 98.0})

    block = price_execution(ledger, _exit_row(ledger, ids=("stop-1",)), quotes)

    # 0.10 below a 98.00 midpoint: 10.2 bps. From its placing, it would read 210 bps,
    # the whole fall, which is the trade's loss, not what the fill paid.
    assert block["exit_bps"] == pytest.approx(0.10 / 98.0 * 10_000)
    ledger.close()


def test_a_tranched_entry_is_weighted_by_quantity(tmp_path):
    ledger = _ledger(tmp_path)
    t_half = T0 + dt.timedelta(minutes=30)
    t1 = T0 + dt.timedelta(hours=1)
    _order(ledger, "entry-1", "buy", "entry", 10.0, 100.10, T0, T0)
    _order(ledger, "entry-2", "buy", "entry", 30.0, 100.0, t_half, t_half)
    _order(ledger, "exit-1", "sell", "exit", 40.0, 101.0, t1, t1)

    block = price_execution(
        ledger, _exit_row(ledger), _Quotes({T0: 100.0, t_half: 100.0, t1: 101.0})
    )

    # 10 shares at 10 bps and 30 at 0: (10 * 10 + 30 * 0) / 40 = 2.5 bps.
    assert block["entry_bps"] == pytest.approx(2.5)
    ledger.close()


def test_a_trade_with_a_fill_it_cant_price_waits_for_a_later_run(tmp_path):
    ledger = _ledger(tmp_path)
    t1 = T0 + dt.timedelta(hours=1)
    _order(ledger, "entry-1", "buy", "entry", 10.0, 100.05, T0, T0)
    _order(ledger, "exit-1", "sell", "exit", 10.0, 100.95, t1, t1)
    _exit_row(ledger)

    assert record_execution_costs(ledger, _Quotes({T0: 100.0})) == 0
    [row] = ledger.all_reflections()
    assert "execution" not in row["content"]

    assert record_execution_costs(ledger, _Quotes({T0: 100.0, t1: 101.0})) == 1
    [row] = ledger.all_reflections()
    assert row["content"]["execution"]["entry_bps"] == pytest.approx(5.0)
    ledger.close()


def test_a_priced_trade_is_not_priced_again_and_the_window_is_respected(tmp_path):
    ledger = _ledger(tmp_path)
    t1 = T0 + dt.timedelta(hours=1)
    _order(ledger, "entry-1", "buy", "entry", 10.0, 100.05, T0, T0)
    _order(ledger, "exit-1", "sell", "exit", 10.0, 100.95, t1, t1)
    _exit_row(ledger)
    quotes = _Quotes({T0: 100.0, t1: 101.0})

    assert record_execution_costs(ledger, quotes, since=dt.date(2026, 11, 17)) == 0
    assert quotes.requests == []
    assert record_execution_costs(ledger, quotes, since=dt.date(2026, 11, 16)) == 1
    asked = len(quotes.requests)
    assert record_execution_costs(ledger, quotes, since=dt.date(2026, 11, 16)) == 0
    assert len(quotes.requests) == asked
    ledger.close()


def test_the_summary_averages_per_side_over_exits_and_counts_the_unpriced():
    rows = [
        {"content": {"execution": {"entry_bps": 4.0, "exit_bps": 2.0, "per_side_bps": 3.0}}},
        {"content": {"execution": {"entry_bps": 6.0, "exit_bps": 8.0, "per_side_bps": 7.0}}},
        {"content": {}},
    ]

    summary = summarize_execution(rows)

    # Per side 3 and 7: mean 5, sample standard deviation 2.828, over root 2: 2.0.
    assert (summary.exits, summary.unpriced) == (2, 1)
    assert summary.entry_bps == pytest.approx(5.0)
    assert summary.exit_bps == pytest.approx(5.0)
    assert summary.per_side_bps == pytest.approx(5.0)
    assert summary.stderr_bps == pytest.approx(2.0)


def test_the_summary_of_nothing_priced_has_no_figures():
    summary = summarize_execution([{"content": {}}])
    assert (summary.exits, summary.unpriced, summary.per_side_bps) == (0, 1, None)

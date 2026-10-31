"""`intraday/hedge.py`: what a SOXX hedge would have done for each closed trade. Measured,
never traded. Every expected figure is written out by hand, in the test."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from semimc.config import IntradayConfig
from semimc.intraday.hedge import (
    price_trade_hedge,
    record_trade_hedges,
    reflections_since,
    summarize_hedged,
    trailing_beta,
)
from semimc.intraday.ledger import IntradayLedger

AS_OF = pd.Timestamp("2026-09-21")
T0 = dt.datetime(2026, 9, 21, 14, 0, tzinfo=dt.UTC)
BETA = 1.5
COST_BPS = 8.0


def _ledger(tmp_path) -> IntradayLedger:
    return IntradayLedger(tmp_path / "intraday.sqlite3")


def _fill(ledger, order_id, side, leg, qty, price, at, symbol="NVDA") -> None:
    ledger.record_order(order_id, symbol, side, status="filled", qty=qty, leg=leg)
    ledger.record_fill(order_id, qty=qty, price=price, filled_at=at)


def _exit_row(ledger, side="buy", qty=10.0, entry=100.0, exit_price=102.0, ids=("exit-1",)):
    sign = 1.0 if side == "buy" else -1.0
    content = {
        "side": side,
        "qty": qty,
        "avg_entry_price": entry,
        "avg_exit_price": exit_price,
        "pnl_gross": sign * (exit_price - entry) * qty,
        "pnl_net": sign * (exit_price - entry) * qty - 1.0,
        "client_order_ids": list(ids),
    }
    reflection_id = ledger.record_reflection(AS_OF, "NVDA", content, client_order_id=ids[0])
    return next(r for r in ledger.all_reflections() if r["id"] == reflection_id)


def _prices(*points: tuple[dt.datetime, float]) -> pd.Series:
    return pd.Series([p for _, p in points], index=[pd.Timestamp(t) for t, _ in points])


def _bar(at: dt.datetime, price: float):
    return SimpleNamespace(timestamp=at, open=price)


def _hedge(ledger, row, prices, beta=BETA):
    return price_trade_hedge(ledger, row, prices, lambda symbol, day: beta, COST_BPS)


def test_a_long_is_hedged_with_a_short_sized_by_its_beta(tmp_path):
    ledger = _ledger(tmp_path)
    t1 = T0 + dt.timedelta(hours=1)
    _fill(ledger, "entry-1", "buy", "entry", 10.0, 100.0, T0)
    _fill(ledger, "exit-1", "sell", "exit", 10.0, 102.0, t1)
    row = _exit_row(ledger)

    hedge = _hedge(ledger, row, _prices((T0, 200.0), (t1, 190.0)))

    # $1,000 of stock at beta 1.5 is $1,500 of SOXX, short. SOXX fell 5%, so the short made
    # $75. The cost is both sides at 8 bps: 1,500 * 2 * 0.0008.
    assert hedge["beta"] == BETA
    assert hedge["notional"] == pytest.approx(1_500.0)
    assert hedge["pnl"] == pytest.approx(75.0)
    assert hedge["cost"] == pytest.approx(2.4)
    ledger.close()


def test_a_short_is_hedged_with_a_long(tmp_path):
    ledger = _ledger(tmp_path)
    t1 = T0 + dt.timedelta(hours=1)
    _fill(ledger, "entry-1", "sell", "entry", 10.0, 100.0, T0)
    _fill(ledger, "exit-1", "buy", "exit", 10.0, 98.0, t1)
    row = _exit_row(ledger, side="sell", exit_price=98.0)

    hedge = _hedge(ledger, row, _prices((T0, 200.0), (t1, 190.0)))

    assert hedge["pnl"] == pytest.approx(-75.0)  # long $1,500 of SOXX through a 5% fall
    ledger.close()


def test_each_tranche_is_hedged_from_its_own_fill(tmp_path):
    ledger = _ledger(tmp_path)
    t1, t2 = T0 + dt.timedelta(minutes=30), T0 + dt.timedelta(minutes=60)
    _fill(ledger, "entry-1", "buy", "entry", 5.0, 100.0, T0)
    _fill(ledger, "entry-2", "buy", "entry", 5.0, 102.0, t1)
    _fill(ledger, "exit-1", "sell", "exit", 10.0, 103.0, t2)
    row = _exit_row(ledger, entry=101.0, exit_price=103.0)

    hedge = _hedge(ledger, row, _prices((T0, 200.0), (t1, 195.0), (t2, 190.0)))

    first = 1.5 * 500.0 * (1 - 190.0 / 200.0)
    second = 1.5 * 510.0 * (1 - 190.0 / 195.0)
    assert hedge["notional"] == pytest.approx(1.5 * 1_010.0)
    assert hedge["pnl"] == pytest.approx(first + second)
    ledger.close()


def test_a_partial_exit_takes_only_its_share_of_the_entries(tmp_path):
    ledger = _ledger(tmp_path)
    t1 = T0 + dt.timedelta(hours=1)
    _fill(ledger, "entry-1", "buy", "entry", 10.0, 100.0, T0)
    _fill(ledger, "trim-1", "sell", "exit", 4.0, 102.0, t1)
    row = _exit_row(ledger, qty=4.0, ids=("trim-1",))

    hedge = _hedge(ledger, row, _prices((T0, 200.0), (t1, 190.0)))

    assert hedge["notional"] == pytest.approx(1.5 * 400.0)
    assert hedge["pnl"] == pytest.approx(30.0)  # 40% of the whole position's $75
    ledger.close()


def test_a_position_held_overnight_is_hedged_through_the_gap(tmp_path):
    """2026-10-29's MRVL short. Most of its gain was the sector's gap, so a hedge takes it
    back: that's what the measure is for."""
    ledger = _ledger(tmp_path)
    t1 = T0 + dt.timedelta(hours=18)
    _fill(ledger, "entry-1", "sell", "entry", 10.0, 100.0, T0)
    _fill(ledger, "exit-1", "buy", "exit", 10.0, 90.0, t1)
    row = _exit_row(ledger, side="sell", exit_price=90.0)

    hedge = _hedge(ledger, row, _prices((T0, 200.0), (t1, 180.0)))

    assert hedge["pnl"] == pytest.approx(-150.0)  # long $1,500 of SOXX through a 10% gap
    ledger.close()


def test_no_beta_leaves_the_exit_unpriced(tmp_path):
    ledger = _ledger(tmp_path)
    t1 = T0 + dt.timedelta(hours=1)
    _fill(ledger, "entry-1", "buy", "entry", 10.0, 100.0, T0)
    _fill(ledger, "exit-1", "sell", "exit", 10.0, 102.0, t1)
    row = _exit_row(ledger)

    assert _hedge(ledger, row, _prices((T0, 200.0), (t1, 190.0)), beta=None) is None
    ledger.close()


def test_a_price_older_than_the_feed_ever_is_not_used(tmp_path):
    """A stale bar means a gap in the feed, and the trade waits for a later run."""
    ledger = _ledger(tmp_path)
    t1 = T0 + dt.timedelta(hours=1)
    _fill(ledger, "entry-1", "buy", "entry", 10.0, 100.0, T0)
    _fill(ledger, "exit-1", "sell", "exit", 10.0, 102.0, t1)
    row = _exit_row(ledger)
    stale = _prices((T0, 200.0), (t1 - dt.timedelta(minutes=16), 190.0))

    assert _hedge(ledger, row, stale) is None
    ledger.close()


def test_a_reflection_with_no_entry_fills_is_left_unpriced(tmp_path):
    ledger = _ledger(tmp_path)
    t1 = T0 + dt.timedelta(hours=1)
    _fill(ledger, "exit-1", "sell", "exit", 10.0, 102.0, t1)
    row = _exit_row(ledger)

    assert _hedge(ledger, row, _prices((T0, 200.0), (t1, 190.0))) is None
    ledger.close()


class _Market:
    """`market_data.bars` and `MarketData.training_window`, enough for the job."""

    def __init__(self, bars):
        self.bar_calls = []
        self.as_of_seen = []
        self._bars = bars

    def bars(self, symbols, start, end, minutes=5):
        self.bar_calls.append({"symbols": symbols, "start": start, "end": end, "minutes": minutes})
        return {"SOXX": self._bars}

    def training_window(self, tickers, as_of, window_days=None, min_observations=None):
        self.as_of_seen.append(as_of)
        soxx = np.tile([0.01, -0.02, 0.015, -0.005, 0.02, -0.01], 10)
        return pd.DataFrame({tickers[0]: BETA * soxx, tickers[1]: soxx})


def test_record_trade_hedges_stores_each_block_once(tmp_path):
    ledger = _ledger(tmp_path)
    t1 = T0 + dt.timedelta(hours=1)
    _fill(ledger, "entry-1", "buy", "entry", 10.0, 100.0, T0)
    _fill(ledger, "exit-1", "sell", "exit", 10.0, 102.0, t1)
    _exit_row(ledger)
    market = _Market([_bar(T0, 200.0), _bar(t1, 190.0)])

    first = record_trade_hedges(ledger, market, market, IntradayConfig())
    second = record_trade_hedges(ledger, market, market, IntradayConfig())

    assert (first, second) == (1, 0)
    assert len(market.bar_calls) == 1  # nothing left to price, so no second fetch
    assert market.bar_calls[0]["minutes"] == 1
    assert market.bar_calls[0]["symbols"] == ["SOXX"]
    [row] = ledger.all_reflections()
    assert row["content"]["hedge"]["pnl"] == pytest.approx(75.0)
    assert row["content"]["pnl_gross"] == pytest.approx(20.0)  # the rest of the row is kept
    ledger.close()


def test_record_trade_hedges_leaves_exits_before_the_start_alone(tmp_path):
    ledger = _ledger(tmp_path)
    t1 = T0 + dt.timedelta(hours=1)
    _fill(ledger, "entry-1", "buy", "entry", 10.0, 100.0, T0)
    _fill(ledger, "exit-1", "sell", "exit", 10.0, 102.0, t1)
    _exit_row(ledger)
    market = _Market([_bar(T0, 200.0), _bar(t1, 190.0)])

    priced = record_trade_hedges(ledger, market, market, IntradayConfig(), dt.date(2026, 10, 15))

    assert priced == 0
    assert market.bar_calls == []
    ledger.close()


def test_record_trade_hedges_reads_a_reflection_from_before_client_order_ids(tmp_path):
    """Six of the placebo's first 36 exits, from 10-15 and 10-16, name their exit only in
    the reflection's own column. They made +$1,120, so dropping them skews the comparison."""
    ledger = _ledger(tmp_path)
    t1 = T0 + dt.timedelta(hours=1)
    _fill(ledger, "entry-1", "buy", "entry", 10.0, 100.0, T0)
    _fill(ledger, "exit-1", "sell", "exit", 10.0, 102.0, t1)
    content = {
        "side": "buy",
        "qty": 10.0,
        "avg_entry_price": 100.0,
        "pnl_gross": 20.0,
        "pnl_net": 19.0,
    }
    ledger.record_reflection(AS_OF, "NVDA", content, client_order_id="exit-1")
    market = _Market([_bar(T0, 200.0), _bar(t1, 190.0)])

    assert record_trade_hedges(ledger, market, market, IntradayConfig()) == 1
    ledger.close()


def test_record_trade_hedges_prices_nothing_without_bars(tmp_path):
    ledger = _ledger(tmp_path)
    t1 = T0 + dt.timedelta(hours=1)
    _fill(ledger, "entry-1", "buy", "entry", 10.0, 100.0, T0)
    _fill(ledger, "exit-1", "sell", "exit", 10.0, 102.0, t1)
    _exit_row(ledger)
    market = _Market([])

    assert record_trade_hedges(ledger, market, market, IntradayConfig()) == 0
    assert "hedge" not in ledger.all_reflections()[0]["content"]
    ledger.close()


def test_a_beta_never_sees_the_day_of_the_trade():
    market = _Market([])
    day = pd.Timestamp("2026-09-21")

    beta = trailing_beta(market, "NVDA", day, IntradayConfig())

    assert beta == pytest.approx(BETA)
    assert market.as_of_seen == [day - pd.Timedelta(days=1)]


def test_a_beta_comes_from_the_real_price_cache_through_training_window(market):
    """The stubs above stand in for `MarketData`; this holds them to its real signature."""
    config = IntradayConfig()
    symbol = next(c for c in market.log_returns.columns if c != "SOXX")
    day = market.last_date
    hedge = config.hedge
    window = market.training_window(
        [symbol, "SOXX"],
        day - pd.Timedelta(days=1),
        window_days=hedge.beta_window_days,
        min_observations=hedge.beta_min_observations,
    )
    assert window.index.max() < day

    beta = trailing_beta(market, symbol, day, config)

    assert beta == pytest.approx(window[symbol].cov(window["SOXX"]) / window["SOXX"].var())


@pytest.mark.parametrize("error", [KeyError("no return data"), ValueError("too few rows")])
def test_a_symbol_without_history_has_no_beta(error):
    def training_window(*args, **kwargs):
        raise error

    market = SimpleNamespace(training_window=training_window)

    assert trailing_beta(market, "NVDA", pd.Timestamp("2026-09-21"), IntradayConfig()) is None


def _row(gross, net, hedge_pnl=None, cost=1.0, notional=1_000.0):
    content = {"pnl_gross": gross, "pnl_net": net, "qty": 10.0, "avg_entry_price": notional / 10.0}
    if hedge_pnl is not None:
        content["hedge"] = {"pnl": hedge_pnl, "cost": cost, "notional": notional, "beta": 1.0}
    return {"content": content, "as_of": str(AS_OF)}


def test_the_summary_compares_in_basis_points_of_each_exit():
    summary = summarize_hedged([_row(10.0, 8.0, 5.0), _row(-20.0, -22.0, 10.0, notional=2_000.0)])

    # +15 on $1,000 is +150 bps; -10 on $2,000 is -50 bps. Mean 50, sample std 141.42.
    assert summary.exits == 2
    assert summary.mean_bps == pytest.approx(50.0)
    assert summary.stderr_bps == pytest.approx(100.0)
    assert summary.hedged_gross == pytest.approx(-10.0 + 15.0)
    assert summary.hedged_net == pytest.approx(-14.0 + 15.0 - 2.0)


def test_the_summary_counts_only_priced_exits_in_its_dollars():
    summary = summarize_hedged([_row(10.0, 8.0, 5.0), _row(500.0, 490.0)])

    assert (summary.exits, summary.unpriced) == (1, 1)
    assert summary.gross == pytest.approx(10.0)
    assert summary.stderr_bps is None


def test_an_empty_summary_has_no_mean():
    assert summarize_hedged([]).mean_bps is None


def test_reflections_since_keeps_the_start_day():
    rows = [{"as_of": "2026-10-14 00:00:00"}, {"as_of": "2026-10-15 00:00:00"}]

    assert reflections_since(rows, dt.date(2026, 10, 15)) == rows[1:]
    assert reflections_since(rows, None) == rows

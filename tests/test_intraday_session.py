"""`intraday/session.py`: the Trader's `session` section, built from 5-minute bars. Pure
computation, so every test hands it synthetic bars with known values. 2026-09-30 is a
Wednesday in daylight time: 09:30 New York is 13:30 UTC."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest

from semimc.intraday.session import session_snapshot, symbol_session

DAY = dt.date(2026, 9, 30)
PRIOR = [dt.date(2026, 9, 25), dt.date(2026, 9, 28), dt.date(2026, 9, 29)]


@dataclass
class _Bar:
    timestamp: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None


def _et(day: dt.date, hhmm: str) -> dt.datetime:
    """A New York wall-clock time as aware UTC (all dates here are in daylight time)."""
    hour, minute = map(int, hhmm.split(":"))
    return dt.datetime(day.year, day.month, day.day, hour + 4, minute, tzinfo=dt.UTC)


def _slots(start: str, count: int) -> list[str]:
    hour, minute = map(int, start.split(":"))
    out = []
    for i in range(count):
        total = hour * 60 + minute + 5 * i
        out.append(f"{total // 60:02d}:{total % 60:02d}")
    return out


def _flat_day(day: dt.date, count: int, price: float, volume: float, start="09:30") -> list[_Bar]:
    return [
        _Bar(_et(day, hhmm), price, price + 0.5, price - 0.5, price, volume)
        for hhmm in _slots(start, count)
    ]


def _today(closes: list[float], volume: float = 200.0) -> list[_Bar]:
    """Today's bars from 09:30, one per close; each bar spans close -/+ 0.5."""
    return [
        _Bar(_et(DAY, hhmm), close, close + 0.5, close - 0.5, close, volume)
        for hhmm, close in zip(_slots("09:30", len(closes)), closes, strict=True)
    ]


def test_open_high_low_last_and_change_ignore_pre_market_bars():
    premarket = _Bar(_et(DAY, "08:00"), 500.0, 999.0, 1.0, 500.0, 50_000)
    bars = [premarket, *_today([100, 102, 101, 103])]
    out = symbol_session(bars, _et(DAY, "09:52"), 30, 10)
    assert out is not None
    assert out["open"] == 100
    assert out["high"] == 103.5
    assert out["low"] == 99.5
    assert out["last"] == 103
    assert out["last_bar_start_et"] == "09:45"
    assert out["change_from_open_pct"] == pytest.approx(0.03)
    assert out["volume_so_far_iex"] == 800


def test_bars_after_now_are_not_used():
    bars = _today([100, 101, 102, 150])  # the 09:45 bar starts after `now`
    out = symbol_session(bars, _et(DAY, "09:44"), 30, 10)
    assert out["last"] == 102
    assert out["high"] == 102.5


def test_opening_range_is_marked_forming_until_its_minutes_have_passed():
    bars = _today([100, 101, 102])
    out = symbol_session(bars, _et(DAY, "09:47"), 30, 10)
    assert out["opening_range"]["complete"] is False
    assert out["opening_range"]["high"] == 102.5
    assert out["opening_range"]["low"] == 99.5


@pytest.mark.parametrize(
    ("last_close", "expected"), [(110.0, "above"), (101.0, "inside"), (90.0, "below")]
)
def test_last_price_is_placed_against_the_completed_opening_range(last_close, expected):
    # Six opening-range bars (09:30-09:55) between 99.5 and 102.5, then the 10:00 bar.
    bars = _today([100, 101, 102, 101, 100, 101, last_close])
    out = symbol_session(bars, _et(DAY, "10:07"), 30, 10)
    assert out["opening_range"] == {"minutes": 30, "complete": True, "high": 102.5, "low": 99.5}
    assert out["last_vs_opening_range"] == expected


def test_relative_volume_compares_the_same_time_of_day_on_earlier_sessions():
    # Earlier sessions: 100 per bar all morning, then a huge afternoon that must not count.
    history = []
    for day in PRIOR[1:]:
        history += _flat_day(day, 4, 50.0, 100.0)
        history += _flat_day(day, 6, 50.0, 1_000_000.0, start="14:00")
    out = symbol_session(history + _today([100, 100, 100, 100]), _et(DAY, "09:52"), 30, 10)
    assert out["relative_volume"] == pytest.approx(2.0)  # 800 today vs 400 typically
    assert out["relative_volume_sessions"] == 2


def test_relative_volume_uses_only_the_most_recent_lookback_sessions():
    history = (
        _flat_day(PRIOR[0], 4, 50.0, 1_000.0)  # oldest, outside a 2-session lookback
        + _flat_day(PRIOR[1], 4, 50.0, 100.0)
        + _flat_day(PRIOR[2], 4, 50.0, 300.0)
    )
    out = symbol_session(history + _today([100, 100, 100, 100]), _et(DAY, "09:52"), 30, 2)
    assert out["relative_volume_sessions"] == 2
    assert out["relative_volume"] == pytest.approx(800 / 800)  # mean of 400 and 1200


def test_relative_volume_is_none_without_history():
    out = symbol_session(_today([100, 101]), _et(DAY, "09:40"), 30, 10)
    assert out["relative_volume"] is None
    assert out["relative_volume_sessions"] == 0


def test_vwap_weights_each_bars_vwap_by_volume_and_falls_back_to_typical_price():
    bars = [
        _Bar(_et(DAY, "09:30"), 100, 101, 99, 100, 100, vwap=100.0),
        _Bar(_et(DAY, "09:35"), 100, 103, 101, 102, 300, vwap=None),  # typical = 102
    ]
    out = symbol_session(bars, _et(DAY, "09:41"), 30, 10)
    assert out["vwap"] == pytest.approx((100 * 100 + 102 * 300) / 400)


def test_naive_bar_timestamps_are_read_as_utc():
    bars = [_Bar(_et(DAY, "09:30").replace(tzinfo=None), 100, 101, 99, 100, 10)]
    out = symbol_session(bars, _et(DAY, "09:36"), 30, 10)
    assert out["open"] == 100


def test_a_symbol_without_a_bar_today_is_left_out_of_the_snapshot():
    snapshot = session_snapshot(
        {"NVDA": _today([100, 101]), "AMD": _flat_day(PRIOR[2], 4, 50.0, 100.0), "MU": []},
        _et(DAY, "09:42"),
        30,
        10,
    )
    assert list(snapshot) == ["NVDA"]

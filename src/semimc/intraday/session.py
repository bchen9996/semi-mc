"""The Trader's `session` section: what each symbol has done so far today, from intraday
bars (added 2026-09-30).

Until then the Trader saw one midpoint per symbol and nothing else. On 2026-09-30, the
first on-time Stage 1 session, the Researcher's only candidate came with the trigger "only
after the first 15-30 minutes, if the post-earnings move holds direction on above-average
volume". The Trader answered `hold`/`no_trade` sixteen times, nearly every answer citing
"no volume data" or "no opening range data". Nothing in its context could ever satisfy a
trigger of that kind, so the agent could not act on its own Researcher's ideas.

Pure computation over bars the caller fetched (`AlpacaMarketDataClient.bars`): no broker,
no network, no model. Regular-session bars only (09:30-16:00 New York); pre- and
post-market bars are dropped so they can't set the open or the day's range.

**Relative volume is IEX against IEX.** The free feed's volume is IEX's own slice of the
tape, a few percent of all trading, so comparing it with consolidated daily volume would
be meaningless. Instead, today's cumulative IEX volume is divided by the average
cumulative IEX volume over the same bar slots on the previous `lookback_sessions`
sessions. Matching the time of day matters because volume is U-shaped through the day: a
straight-line pro-rating of daily volume would read every morning as unusually busy.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd

_EXCHANGE_TZ = "America/New_York"
_OPEN = dt.time(9, 30)
_CLOSE = dt.time(16, 0)


def _frame(bars: list[Any]) -> pd.DataFrame:
    """Bars (anything with timestamp/open/high/low/close/volume, `vwap` optional) as a
    frame indexed by bar-start time in New York, regular session only."""
    if not bars:
        return pd.DataFrame()
    frame = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp(b.timestamp) for b in bars],
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
            "volume": [float(b.volume) for b in bars],
            "vwap": [
                float(b.vwap) if getattr(b, "vwap", None) is not None else float("nan")
                for b in bars
            ],
        }
    )
    stamps = frame["timestamp"]
    if stamps.dt.tz is None:
        stamps = stamps.dt.tz_localize("UTC")
    frame.index = stamps.dt.tz_convert(_EXCHANGE_TZ)
    frame = frame.drop(columns="timestamp").sort_index()
    times = frame.index.time
    return frame[(times >= _OPEN) & (times < _CLOSE)]


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None or pd.isna(value) else round(float(value), digits)


def symbol_session(
    bars: list[Any],
    now: dt.datetime,
    opening_range_minutes: int,
    lookback_sessions: int,
) -> dict[str, Any] | None:
    """One symbol's session so far, or ``None`` if it has no regular-session bar today.

    Prices are in dollars; ``change_from_open_pct`` and the relative volume are plain
    ratios (0.012 is +1.2%; 1.0 is normal volume for the time of day). ``vwap`` uses each
    bar's own VWAP weighted by volume, falling back to the bar's typical price
    (high + low + close) / 3 where a bar has none."""
    now_ny = pd.Timestamp(now if now.tzinfo else now.replace(tzinfo=dt.UTC)).tz_convert(
        _EXCHANGE_TZ
    )
    frame = _frame(bars)
    if frame.empty:
        return None
    frame = frame[frame.index <= now_ny]
    dates = frame.index.date
    today = frame[dates == now_ny.date()]
    if today.empty:
        return None

    open_at = pd.Timestamp(dt.datetime.combine(now_ny.date(), _OPEN)).tz_localize(_EXCHANGE_TZ)
    range_end = open_at + pd.Timedelta(minutes=opening_range_minutes)
    opening = today[today.index < range_end]
    last = float(today["close"].iloc[-1])
    range_high = float(opening["high"].max()) if not opening.empty else None
    range_low = float(opening["low"].min()) if not opening.empty else None
    if range_high is None or range_low is None:
        position = None
    elif last > range_high:
        position = "above"
    elif last < range_low:
        position = "below"
    else:
        position = "inside"

    typical = (today["high"] + today["low"] + today["close"]) / 3.0
    bar_vwap = today["vwap"].fillna(typical)
    volume_so_far = float(today["volume"].sum())
    vwap = (
        float((bar_vwap * today["volume"]).sum() / volume_so_far)
        if volume_so_far > 0
        else float(typical.mean())
    )

    # Compare like with like: each earlier session's volume over the same bar slots.
    last_slot = today.index[-1].time()
    earlier = frame[(dates < now_ny.date()) & (frame.index.time <= last_slot)]
    by_day = earlier.groupby(earlier.index.date)["volume"].sum()
    by_day = by_day.iloc[-lookback_sessions:] if lookback_sessions > 0 else by_day.iloc[:0]
    typical_volume = float(by_day.mean()) if not by_day.empty else None
    relative_volume = None
    if typical_volume is not None and typical_volume > 0:
        relative_volume = volume_so_far / typical_volume

    open_price = float(today["open"].iloc[0])
    return {
        "open": _round(open_price),
        "high": _round(today["high"].max()),
        "low": _round(today["low"].min()),
        "last": _round(last),
        "last_bar_start_et": today.index[-1].strftime("%H:%M"),
        "change_from_open_pct": _round((last - open_price) / open_price if open_price else None),
        "vwap": _round(vwap),
        "opening_range": {
            "minutes": opening_range_minutes,
            "complete": bool(now_ny >= range_end),
            "high": _round(range_high),
            "low": _round(range_low),
        },
        "last_vs_opening_range": position,
        "volume_so_far_iex": int(volume_so_far),
        "relative_volume": _round(relative_volume, 2),
        "relative_volume_sessions": int(len(by_day)),
    }


def session_snapshot(
    bars_by_symbol: dict[str, list[Any]],
    now: dt.datetime,
    opening_range_minutes: int,
    lookback_sessions: int,
) -> dict[str, dict[str, Any]]:
    """`symbol_session` for every symbol with a bar today; symbols without one are left
    out (the Trader prompt says what an absent symbol means)."""
    out: dict[str, dict[str, Any]] = {}
    for symbol, bars in sorted(bars_by_symbol.items()):
        summary = symbol_session(bars, now, opening_range_minutes, lookback_sessions)
        if summary is not None:
            out[symbol] = summary
    return out

"""Earnings calendar handling.

Two jobs:

1. map a reported earnings *date* plus its timing to the trading day the market actually
   reacts on -- a before-open report moves that same session, an after-close report moves
   the next one;
2. measure each ticker's empirical distribution of earnings-day **abnormal** returns,
   ``r - beta * benchmark``, so an earnings jump is not double counted with whatever the
   market did that day.

The calendar is a user-editable CSV (``config/earnings_calendar.csv``) with columns
``ticker,date,timing`` where timing is ``bmo`` (before market open), ``amc`` (after market
close), or ``unknown``. ``unknown`` is treated as ``amc``, which is the dominant
convention for this universe, and the assumption is logged.

The provider route (``yfinance``) only returns a few recent quarters, so it can seed the
CSV but cannot be the source of truth for a backtest running to 2012. When no calendar is
available the earnings model refuses to run rather than inventing dates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)

CALENDAR_COLUMNS = ["ticker", "date", "timing"]
VALID_TIMINGS = {"bmo", "amc", "unknown"}

TEMPLATE_HEADER = (
    "# semimc earnings calendar.\n"
    "# One row per reported quarter. timing is bmo (before open), amc (after close),\n"
    "# or unknown (treated as amc). Dates are the REPORT date, not the reaction date.\n"
    "ticker,date,timing\n"
)


@dataclass(frozen=True)
class EarningsCalendar:
    """Earnings report dates by ticker."""

    events: pd.DataFrame  # columns: ticker, date, timing

    @property
    def tickers(self) -> list[str]:
        return sorted(self.events["ticker"].unique().tolist()) if not self.events.empty else []

    def is_empty(self) -> bool:
        return self.events.empty

    def for_ticker(self, ticker: str) -> pd.DataFrame:
        return self.events[self.events["ticker"] == ticker].sort_values("date")

    def truncated(self, as_of: pd.Timestamp) -> EarningsCalendar:
        """Rows dated at or before ``as_of`` -- the point-in-time view of past reports."""
        stamp = pd.Timestamp(as_of).normalize()
        return EarningsCalendar(self.events[self.events["date"] <= stamp].copy())

    def reaction_days(self, ticker: str, trading_days: pd.DatetimeIndex) -> pd.DatetimeIndex:
        """Trading days on which this ticker's reports were priced.

        Parameters
        ----------
        ticker
            Symbol to look up.
        trading_days
            The sessions available, typically a price index. A report with no subsequent
            session in this index (for instance the most recent one) is dropped.

        Returns
        -------
        pd.DatetimeIndex
            Unique, sorted reaction sessions.
        """
        rows = self.for_ticker(ticker)
        if rows.empty or len(trading_days) == 0:
            return pd.DatetimeIndex([], name="date")

        calendar = pd.DatetimeIndex(trading_days).sort_values()
        out: list[pd.Timestamp] = []
        for date, timing in zip(rows["date"], rows["timing"], strict=True):
            stamp = pd.Timestamp(date).normalize()
            side = "left" if timing == "bmo" else "right"
            position = calendar.searchsorted(stamp, side=side)
            if position < len(calendar):
                out.append(calendar[position])
        return pd.DatetimeIndex(sorted(set(out)), name="date")

    def future_reaction_offsets(
        self,
        ticker: str,
        origin: pd.Timestamp,
        horizon: int,
        trading_days_per_year: int = 252,
    ) -> list[int]:
        """Horizon step indices (0-based) on which a scheduled report lands.

        The *date* of a future report is treated as known at the origin; its *reaction*
        is not. That is close to reality -- companies confirm the next report date weeks
        ahead and the quarterly cadence is highly predictable -- but it is an assumption,
        and it is stated in MODEL_NOTES.

        Calendar days are mapped to horizon steps by the ratio of trading days to
        calendar days, since the simulation advances in trading days and the calendar
        stores calendar dates.
        """
        rows = self.for_ticker(ticker)
        if rows.empty:
            return []
        start = pd.Timestamp(origin).normalize()
        days_per_step = 365.25 / trading_days_per_year
        offsets: list[int] = []
        for date, timing in zip(rows["date"], rows["timing"], strict=True):
            stamp = pd.Timestamp(date).normalize()
            if stamp <= start:
                continue
            calendar_days = (stamp - start).days + (0 if timing == "bmo" else 1)
            step = int(round(calendar_days / days_per_step)) - 1
            if 0 <= step < horizon:
                offsets.append(step)
        return sorted(set(offsets))


def load_calendar(path: str | Path) -> EarningsCalendar:
    """Read the earnings CSV. A missing or header-only file yields an empty calendar."""
    csv_path = Path(path)
    if not csv_path.exists():
        LOGGER.info("no earnings calendar at %s", csv_path)
        return EarningsCalendar(pd.DataFrame(columns=CALENDAR_COLUMNS))

    frame = pd.read_csv(csv_path, comment="#")
    if frame.empty:
        return EarningsCalendar(pd.DataFrame(columns=CALENDAR_COLUMNS))

    missing = [c for c in ("ticker", "date") if c not in frame.columns]
    if missing:
        raise ValueError(f"earnings calendar {csv_path} is missing columns: {missing}")

    frame = frame.copy()
    frame["ticker"] = frame["ticker"].astype(str).str.strip().str.upper()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    if "timing" not in frame.columns:
        frame["timing"] = "unknown"
    frame["timing"] = frame["timing"].astype(str).str.strip().str.lower().fillna("unknown")

    bad_timing = ~frame["timing"].isin(VALID_TIMINGS)
    if bad_timing.any():
        LOGGER.warning(
            "%d earnings rows have an unrecognised timing; treating them as 'unknown'",
            int(bad_timing.sum()),
        )
        frame.loc[bad_timing, "timing"] = "unknown"

    bad_date = frame["date"].isna()
    if bad_date.any():
        LOGGER.warning("dropping %d earnings rows with unparseable dates", int(bad_date.sum()))
        frame = frame[~bad_date]

    n_unknown = int((frame["timing"] == "unknown").sum())
    if n_unknown:
        LOGGER.info("%d earnings rows have unknown timing; treating them as after-close", n_unknown)

    frame = frame.drop_duplicates(subset=["ticker", "date"]).sort_values(["ticker", "date"])
    return EarningsCalendar(frame.loc[:, CALENDAR_COLUMNS].reset_index(drop=True))


def write_template(path: str | Path) -> Path:
    """Create an empty, commented calendar file for the user to fill in."""
    csv_path = Path(path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(TEMPLATE_HEADER, encoding="utf-8")
    return csv_path


def fetch_calendar_from_provider(tickers: list[str]) -> pd.DataFrame:
    """Pull whatever earnings dates yfinance exposes.

    Coverage is a handful of recent quarters and the timing field is usually absent, so
    the result is written out for review rather than used directly. Timing comes back as
    ``unknown`` unless the timestamp's hour makes it obvious.
    """
    import yfinance as yf

    rows: list[dict] = []
    for ticker in tickers:
        try:
            dates = yf.Ticker(ticker).get_earnings_dates(limit=40)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            LOGGER.warning("earnings fetch failed for %s: %s", ticker, exc)
            continue
        if dates is None or len(dates) == 0:
            LOGGER.warning("no earnings dates returned for %s", ticker)
            continue
        for stamp in pd.DatetimeIndex(dates.index):
            local = stamp.tz_convert("America/New_York") if stamp.tzinfo else stamp
            # Midnight means yfinance gave a date with no time, so timing is genuinely
            # unknown rather than "before the open".
            timing = "unknown" if local.hour == 0 else ("bmo" if local.hour < 12 else "amc")
            rows.append(
                {"ticker": ticker, "date": local.normalize().tz_localize(None), "timing": timing}
            )

    if not rows:
        return pd.DataFrame(columns=CALENDAR_COLUMNS)
    frame = pd.DataFrame(rows).drop_duplicates(subset=["ticker", "date"])
    return frame.sort_values(["ticker", "date"]).reset_index(drop=True)


def abnormal_earnings_returns(
    log_returns: pd.Series,
    benchmark_returns: pd.Series,
    reaction_days: pd.DatetimeIndex,
    beta: float,
) -> pd.Series:
    """Earnings-day abnormal log returns: ``r - beta * benchmark``.

    Parameters
    ----------
    log_returns
        The ticker's daily log returns.
    benchmark_returns
        Benchmark daily log returns over the same index.
    reaction_days
        Sessions on which reports were priced.
    beta
        The ticker's beta to that benchmark, estimated on non-earnings days.

    Returns
    -------
    pd.Series
        Abnormal returns indexed by reaction day. Empty when nothing overlaps.
    """
    if len(reaction_days) == 0:
        return pd.Series(dtype=float)
    days = log_returns.index.intersection(reaction_days)
    if len(days) == 0:
        return pd.Series(dtype=float)
    bench = benchmark_returns.reindex(days)
    used_beta = 0.0 if not np.isfinite(beta) else beta
    return (log_returns.reindex(days) - used_beta * bench.fillna(0.0)).dropna()


def strip_earnings_returns(
    log_returns: pd.Series,
    benchmark_returns: pd.Series,
    reaction_days: pd.DatetimeIndex,
    beta: float,
) -> tuple[pd.Series, int]:
    """Replace each earnings-day return with ``beta * benchmark`` for that day.

    This removes the jump from a base model's training data so the overlay does not
    double count it. It also removes that day's ordinary idiosyncratic noise -- about
    four days a year -- which is the accepted cost, documented in MODEL_NOTES.

    Returns
    -------
    stripped, n_replaced
    """
    if len(reaction_days) == 0:
        return log_returns.copy(), 0
    days = log_returns.index.intersection(reaction_days)
    if len(days) == 0:
        return log_returns.copy(), 0
    used_beta = 0.0 if not np.isfinite(beta) else beta
    out = log_returns.copy()
    out.loc[days] = used_beta * benchmark_returns.reindex(days).fillna(0.0)
    return out, int(len(days))

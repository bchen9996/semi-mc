"""Cross-validation for panel data sampled on a calendar, not on rows.

Two mistakes are easy to make with monthly-sampled, multi-ticker panels and a plain
``sklearn.model_selection.TimeSeriesSplit``, and both would leak information:

**Splitting rows instead of dates.** A panel row is (date, ticker). A fold boundary drawn
on row index would put part of one month's cross-section in the training fold and the rest
of the same month's cross-section in the test fold -- the model would then be evaluated on
tickers it saw contemporaneous peers for during training, a much easier problem than the
one it will face live.

**No gap between train and test.** A target built from a forward ``H``-day return is a
function of prices up to ``H`` trading days after its signal date. A test fold immediately
following a training fold shares that overlap window, so the "test" score partly measures
training performance.

Both are fixed the same way: split on the **set of unique dates**, and leave a gap of at
least the forecast horizon (in sampling periods) between the last training date and the
first test date.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def date_grouped_gapped_splits(
    dates: pd.DatetimeIndex | pd.Series,
    n_folds: int = 5,
    gap_periods: int = 1,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Expanding-window splits over unique dates, with a gap between train and test.

    Parameters
    ----------
    dates
        The panel's date column (repeated per ticker is fine -- only unique values are
        used to build folds).
    n_folds
        Number of test folds carved from the back of the date range.
    gap_periods
        Sampling periods skipped between the end of train and the start of test. Pass at
        least ``ceil(horizon_days / days_per_sampling_period)`` so an overlapping target
        window cannot appear in both folds.

    Returns
    -------
    list[(train_dates, test_dates)]
        Each entry holds the **dates** (not row positions) in that split. The caller joins
        these back to row positions, since a date-to-row mapping is the caller's own panel.
    """
    unique_dates = pd.DatetimeIndex(sorted(pd.Series(dates).dropna().unique()))
    n = len(unique_dates)
    if n_folds < 2:
        raise ValueError("n_folds must be at least 2")
    if n < n_folds * 3:
        raise ValueError(f"only {n} unique dates for {n_folds} folds; too few to split sensibly")

    fold_size = n // (n_folds + 1)
    splits: list[tuple[np.ndarray, np.ndarray]] = []
    for fold in range(1, n_folds + 1):
        test_start = fold_size * fold
        test_stop = min(test_start + fold_size, n) if fold < n_folds else n
        train_stop = max(test_start - gap_periods, 0)
        if train_stop < 3 or test_stop <= test_start:
            continue
        train_dates = unique_dates[:train_stop].to_numpy()
        test_dates = unique_dates[test_start:test_stop].to_numpy()
        splits.append((train_dates, test_dates))
    return splits


def rows_for_dates(panel: pd.DataFrame, date_column: str, dates: np.ndarray) -> pd.DataFrame:
    """Rows of ``panel`` whose ``date_column`` is in ``dates``. A small, named helper so
    callers don't repeat the same boolean mask everywhere."""
    return panel[panel[date_column].isin(dates)]


def approx_trading_days_per_period(freq: str, sample_periods: int = 13) -> float:
    """Median calendar gap between consecutive ``freq`` periods, in trading days.

    Converts via the same 252/365.25 calendar-to-trading-day idiom
    ``backtest.walkforward.select_non_overlapping`` already uses for the same kind of
    approximation. This only needs to be right enough to size a CV gap or a HAC lag, not to
    schedule a real calendar -- it under-counts for sub-weekly offsets (``"B"``, ``"D"``),
    which already skip weekends themselves, but this project's realistic choices are
    weekly-through-quarterly, so that case is documented rather than special-cased.
    """
    dates = pd.date_range(pd.Timestamp("2000-01-01"), periods=sample_periods + 1, freq=freq)
    gaps = np.diff(dates).astype("timedelta64[D]").astype(float)
    return float(np.median(gaps)) * 252.0 / 365.25


def min_gap_periods(horizon_days: int, freq: str = "ME") -> int:
    """The smallest ``gap_periods`` that keeps a ``horizon_days``-long forward-return
    window from spanning a train/test fold boundary, per :func:`date_grouped_gapped_splits`
    's own rule (``gap_periods >= ceil(horizon_days / days_per_sampling_period)``)."""
    return max(1, math.ceil(horizon_days / approx_trading_days_per_period(freq)))

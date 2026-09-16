"""The four signals built directly from data the package already handles point-in-time.

Momentum, reversal and relative strength are pure transforms of a return panel already cut
at ``as_of`` -- there is no separate release-date concern because a price on day ``t`` is,
by definition, known on day ``t``. Earnings drift borrows the earnings module's own
point-in-time machinery (``calendar.truncated``). The fifth candidate signal, industry
cycle, is not here: it is a **sector-level** regressor for the sector-tilt regression in
``forecasting/sector.py``, not a per-ticker input to the panel regression, and it is the one
signal that genuinely needs ``signals/external.py``'s release-date handling. The sixth,
customer lead-lag, lives in ``signals/supply_chain.py`` since it has its own data shape.

Every function here takes trading-day windows measured in **positions**, not calendar days
-- consistent with how the rest of the package measures a horizon -- so a lookback of 252
means exactly 252 rows of the return panel, never "roughly a year".
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..data.earnings import EarningsCalendar, abnormal_earnings_returns
from ..data.returns import estimate_beta


def _position_at_or_before(index: pd.DatetimeIndex, as_of: pd.Timestamp) -> int:
    """Index of the last row at or before ``as_of``, or -1 when none exists."""
    stamp = pd.Timestamp(as_of).normalize()
    position = index.searchsorted(stamp, side="right") - 1
    return int(position)


def momentum_signal(
    log_returns: pd.DataFrame,
    as_of: pd.Timestamp,
    lookback_days: int = 252,
    skip_days: int = 21,
) -> pd.Series:
    """Cumulative log return from ``as_of - lookback`` to ``as_of - skip``, per ticker.

    The skip excludes the most recent month, which is where short-term reversal lives
    (Jegadeesh 1990) and would otherwise partly cancel the momentum effect it is measured
    alongside. :func:`reversal_signal` is exactly that excluded window, kept separate
    rather than blended in.
    """
    end = _position_at_or_before(log_returns.index, as_of)
    start = end - lookback_days
    stop = end - skip_days
    if start < 0 or stop <= start:
        return pd.Series(np.nan, index=log_returns.columns, name="momentum")
    return log_returns.iloc[start:stop].sum().rename("momentum")


def reversal_signal(
    log_returns: pd.DataFrame,
    as_of: pd.Timestamp,
    skip_days: int = 21,
) -> pd.Series:
    """Cumulative log return over the most recent ``skip_days`` -- the window momentum excludes."""
    end = _position_at_or_before(log_returns.index, as_of)
    start = end - skip_days
    if start < 0:
        return pd.Series(np.nan, index=log_returns.columns, name="reversal")
    return log_returns.iloc[start:end].sum().rename("reversal")


def relative_strength_signal(
    log_returns: pd.DataFrame,
    sector_returns: pd.Series,
    as_of: pd.Timestamp,
    lookback_days: int = 126,
) -> pd.Series:
    """3-6 month return relative to the sector benchmark: ticker cumulative minus SOXX's.

    Deliberately correlated with momentum by construction -- both are trailing cumulative
    returns -- and that overlap is exactly what the addendum asks to check; it is measured
    in ``forecasting/panel.py`` rather than removed here, so both signals are still
    available to the regression to separate.
    """
    end = _position_at_or_before(log_returns.index, as_of)
    start = end - lookback_days
    if start < 0:
        return pd.Series(np.nan, index=log_returns.columns, name="relative_strength")
    ticker_leg = log_returns.iloc[start:end].sum()
    sector_leg = float(sector_returns.iloc[start:end].sum())
    return (ticker_leg - sector_leg).rename("relative_strength")


def earnings_drift_signal(
    calendar: EarningsCalendar,
    log_returns: pd.DataFrame,
    benchmark_returns: pd.Series,
    as_of: pd.Timestamp,
    active_days: int = 60,
    min_beta_observations: int = 60,
) -> pd.Series:
    """Most recent earnings-day abnormal return, active for ``active_days`` after the report.

    A stand-in for analyst-consensus earnings surprise, which this project has no paid
    source for: the market's own reaction *is* a surprise measure, on the logic that an
    in-line quarter does not move the stock much and a genuine surprise does. Reuses the
    Phase 4 earnings module's abnormal-return machinery (``r - beta * benchmark``) so this
    signal and the earnings-jump overlay agree on what "abnormal" means.

    Outside the active window a ticker's value is ``NaN`` -- the signal is inactive, not
    zero, so a regression can tell "no recent report" apart from "reported and shrugged".
    """
    if calendar.is_empty():
        return pd.Series(np.nan, index=log_returns.columns, name="earnings_drift")
    past = calendar.truncated(as_of)
    end = _position_at_or_before(log_returns.index, as_of)
    if end < 0:
        return pd.Series(np.nan, index=log_returns.columns, name="earnings_drift")

    out = {}
    for ticker in log_returns.columns:
        series = log_returns[ticker].iloc[: end + 1].dropna()
        reaction_days = past.reaction_days(ticker, pd.DatetimeIndex(series.index))
        if len(reaction_days) == 0:
            out[ticker] = np.nan
            continue
        last_reaction = reaction_days.max()
        age = end - series.index.get_indexer([last_reaction])[0]
        if age < 0 or age > active_days:
            out[ticker] = np.nan
            continue

        ordinary = series.drop(index=series.index.intersection(reaction_days))
        if len(ordinary) < min_beta_observations:
            out[ticker] = np.nan
            continue
        beta = estimate_beta(ordinary, benchmark_returns)
        abnormal = abnormal_earnings_returns(
            series, benchmark_returns, pd.DatetimeIndex([last_reaction]), beta
        )
        out[ticker] = float(abnormal.iloc[0]) if len(abnormal) else np.nan

    return pd.Series(out, name="earnings_drift").reindex(log_returns.columns)


def signal_panel(
    log_returns: pd.DataFrame,
    sector_returns: pd.Series,
    benchmark_returns: pd.Series,
    calendar: EarningsCalendar,
    as_of: pd.Timestamp,
    momentum_lookback_days: int = 252,
    momentum_skip_days: int = 21,
    relative_strength_lookback_days: int = 126,
    earnings_drift_active_days: int = 60,
) -> pd.DataFrame:
    """All four ticker-level signals at one origin, as columns of one frame.

    Returns
    -------
    pd.DataFrame
        Index: ticker. Columns: ``momentum``, ``reversal``, ``relative_strength``,
        ``earnings_drift``. A ``NaN`` cell means that signal is unavailable for that
        ticker at this origin (too little history, or -- for earnings drift -- no report
        inside the active window), never a silently substituted zero.
    """
    columns = [
        momentum_signal(log_returns, as_of, momentum_lookback_days, momentum_skip_days),
        reversal_signal(log_returns, as_of, momentum_skip_days),
        relative_strength_signal(
            log_returns, sector_returns, as_of, relative_strength_lookback_days
        ),
        earnings_drift_signal(
            calendar, log_returns, benchmark_returns, as_of, earnings_drift_active_days
        ),
    ]
    return pd.concat(columns, axis=1)

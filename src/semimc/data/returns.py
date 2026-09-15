"""Return construction, calendar alignment, and descriptive statistics.

Conventions used throughout the package (Section 8 of the brief):

* **log returns** for simulation: ``r_t = ln(P_t / P_{t-1})``, additive across time;
* **simple returns** for portfolio aggregation: ``R_t = P_t / P_{t-1} - 1``, additive
  across holdings.

Nothing here fills or interpolates prices. Where a ticker has no observation on a day
another ticker traded, its return stays ``NaN`` and the caller decides -- explicitly --
whether to drop the day or the ticker.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252


def log_returns(prices: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    """Daily log returns from a price frame or series.

    Parameters
    ----------
    prices
        Adjusted closes. Index: dates. Columns (if a frame): tickers.

    Returns
    -------
    Same container type as the input, with the first row dropped. Units: log points
    per day (0.01 is approximately a 1% move).
    """
    out = np.log(prices / prices.shift(1))
    return out.iloc[1:]


def simple_returns(prices: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    """Daily simple returns. Used for portfolio aggregation, never for simulation."""
    return (prices / prices.shift(1) - 1.0).iloc[1:]


def to_simple(log_return: np.ndarray | float) -> np.ndarray | float:
    """Convert log returns to simple returns elementwise."""
    return np.expm1(log_return)


def to_log(simple_return: np.ndarray | float) -> np.ndarray | float:
    """Convert simple returns to log returns elementwise."""
    return np.log1p(simple_return)


def common_calendar(
    returns: pd.DataFrame,
    tickers: list[str] | None = None,
    min_observations: int = 1,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Restrict a return panel to days on which every requested ticker traded.

    Unequal history lengths are the norm here -- ARM has only traded since September 2023,
    while TXN goes back decades -- so this is deliberately explicit: it returns both the
    aligned panel and a per-ticker count of the observations dropped, for logging.

    Parameters
    ----------
    returns
        Daily returns. Index: dates. Columns: tickers.
    tickers
        Subset to align. Defaults to every column.
    min_observations
        Tickers with fewer non-null observations than this are excluded from the
        alignment entirely rather than truncating everyone else's history to their
        short life.

    Returns
    -------
    aligned, dropped
        ``aligned`` is the complete-case panel; ``dropped`` maps each excluded or
        truncated ticker to the number of its observations that were lost.
    """
    columns = list(tickers) if tickers is not None else list(returns.columns)
    available = [c for c in columns if c in returns.columns]

    counts = returns.loc[:, available].notna().sum()
    kept = [c for c in available if counts[c] >= min_observations]
    excluded = {c: int(counts[c]) for c in available if c not in kept}

    panel = returns.loc[:, kept].dropna(how="any")
    dropped = dict(excluded)
    for ticker in kept:
        lost = int(counts[ticker] - panel[ticker].notna().sum())
        if lost:
            dropped[ticker] = lost
    return panel, dropped


def annualized_volatility(
    returns: pd.Series | pd.DataFrame,
    trading_days_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float | pd.Series:
    """Annualized standard deviation of daily log returns (square-root-of-time)."""
    return returns.std(ddof=1) * np.sqrt(trading_days_per_year)


def ewma_volatility(
    returns: pd.Series,
    lam: float = 0.94,
    trading_days_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """RiskMetrics EWMA volatility, annualized.

    ``sigma^2_t = (1 - lam) * sum_{i>=0} lam^i * r_{t-i}^2``, computed about zero rather
    than about the sample mean: at daily frequency the mean is a rounding error next to
    the variance, and centring on it would make the estimate drift with the sample
    period, which is exactly what the drift policy exists to prevent.

    Parameters
    ----------
    returns
        Daily log returns, oldest first.
    lam
        Decay factor. 0.94 is the RiskMetrics daily default.

    Returns
    -------
    float
        Annualized volatility.
    """
    values = np.asarray(returns.dropna(), dtype=float)
    if values.size == 0:
        return float("nan")
    weights = (1.0 - lam) * lam ** np.arange(values.size - 1, -1, -1)
    variance = float(np.sum(weights * values**2) / np.sum(weights))
    return float(np.sqrt(variance * trading_days_per_year))


def estimate_beta(asset: pd.Series, benchmark: pd.Series) -> float:
    """OLS beta of ``asset`` on ``benchmark`` over their overlapping dates.

    Returns ``nan`` when fewer than 30 overlapping observations are available, rather
    than a beta estimated from a handful of days.
    """
    joined = pd.concat([asset, benchmark], axis=1, join="inner").dropna()
    if len(joined) < 30:
        return float("nan")
    x = joined.iloc[:, 1].to_numpy()
    y = joined.iloc[:, 0].to_numpy()
    variance = np.var(x, ddof=1)
    if variance <= 0:
        return float("nan")
    return float(np.cov(y, x, ddof=1)[0, 1] / variance)


def estimate_betas(
    returns: pd.DataFrame,
    benchmark: pd.Series,
    tickers: list[str] | None = None,
) -> pd.Series:
    """Beta of each column on a benchmark series."""
    columns = list(tickers) if tickers is not None else list(returns.columns)
    return pd.Series(
        {t: estimate_beta(returns[t], benchmark) for t in columns if t in returns.columns},
        name="beta",
        dtype=float,
    )


def residualize(target: pd.Series, factor: pd.Series) -> pd.Series:
    """Residual of ``target`` after removing its OLS exposure to ``factor``.

    Used to build the sector factor (SOXX residualized on SPY) so the market and sector
    factors are close to orthogonal and their loadings are separately interpretable.
    """
    joined = pd.concat([target, factor], axis=1, join="inner").dropna()
    if len(joined) < 30:
        return pd.Series(dtype=float)
    y = joined.iloc[:, 0]
    x = joined.iloc[:, 1]
    beta = estimate_beta(y, x)
    alpha = y.mean() - beta * x.mean()
    return y - (alpha + beta * x)


def summary_statistics(
    returns: pd.DataFrame,
    market: pd.Series | None = None,
    sector: pd.Series | None = None,
    trading_days_per_year: int = TRADING_DAYS_PER_YEAR,
) -> pd.DataFrame:
    """Per-ticker descriptive table (Phase 2 of the brief).

    Columns: observation count, first/last date, annualized volatility, skew, excess
    kurtosis, betas to the market and sector benchmarks, and the worst and best single
    days. Statistics are computed per ticker on that ticker's own observations, so a
    short history like ARM's is described honestly instead of being padded.
    """
    rows = []
    for ticker in returns.columns:
        series = returns[ticker].dropna()
        if series.empty:
            continue
        rows.append(
            {
                "ticker": ticker,
                "observations": int(series.size),
                "first_date": series.index.min().date(),
                "last_date": series.index.max().date(),
                "ann_vol": float(annualized_volatility(series, trading_days_per_year)),
                "skew": float(series.skew()),
                "excess_kurtosis": float(series.kurtosis()),  # pandas reports excess already
                "beta_market": estimate_beta(series, market) if market is not None else np.nan,
                "beta_sector": estimate_beta(series, sector) if sector is not None else np.nan,
                "worst_day": float(np.expm1(series.min())),
                "best_day": float(np.expm1(series.max())),
            }
        )
    return pd.DataFrame(rows).set_index("ticker") if rows else pd.DataFrame()


def correlation_matrix(returns: pd.DataFrame, min_periods: int = 60) -> pd.DataFrame:
    """Pairwise-complete correlation matrix of daily log returns."""
    return returns.corr(min_periods=min_periods)

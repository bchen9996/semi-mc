"""Daily realized-variance proxies from adjusted OHLC prices.

A close-to-close squared return is an unbiased but extremely noisy variance proxy: one
observation per day carries about as much information as a chi-squared with one degree of
freedom, which is why volatility regressions fitted on it look weak even when the
underlying volatility is highly predictable. Range estimators use the whole session's path
and cut that noise by roughly a factor of five.

They also miss something. A range estimator only sees the open-to-close path, and for
semiconductors a large share of the variance arrives **overnight** -- an earnings print
after the close, or an export-control announcement while the market is shut. So the proxy
used here is the sum of two pieces:

``RV_t = r_overnight^2 + intraday_range_estimator_t``

where ``r_overnight = ln(O_t / C_{t-1})``. Garman-Klass is the default intraday term;
Rogers-Satchell is available because it stays unbiased when the intraday path has a drift,
at the cost of being noisier.

Everything here needs open, high and low to be adjusted on the same basis as the close.
:func:`adjustment_consistency` checks that and reports violations rather than repairing
them -- a mixed-basis row is a data bug, and silently patching it would hide it.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)

# ln(H/L)^2 has expectation 4 ln 2 sigma^2 for a driftless Brownian session, which is where
# both constants below come from.
_GK_CLOSE_COEFFICIENT = 2.0 * np.log(2.0) - 1.0

MIN_VARIANCE = 1e-10  # a floor, so a zero-range day cannot produce log(0)


def overnight_log_return(open_price: pd.Series, close: pd.Series) -> pd.Series:
    """``ln(O_t / C_{t-1})`` -- the gap between yesterday's close and today's open."""
    previous_close = close.shift(1)
    return np.log(open_price / previous_close)


def garman_klass(
    open_price: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series
) -> pd.Series:
    """Garman-Klass intraday variance, ``0.5 ln(H/L)^2 - (2 ln2 - 1) ln(C/O)^2``.

    Roughly seven times more efficient than a squared close-to-close return under the
    estimator's assumptions (driftless geometric Brownian motion within the session, no
    jumps, continuously observed extremes). Real extremes are observed discretely, which
    biases it slightly *down*; that bias is small next to the noise it removes.
    """
    log_hl = np.log(high / low)
    log_co = np.log(close / open_price)
    return 0.5 * log_hl**2 - _GK_CLOSE_COEFFICIENT * log_co**2


def rogers_satchell(
    open_price: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series
) -> pd.Series:
    """Rogers-Satchell intraday variance.

    ``ln(H/C) ln(H/O) + ln(L/C) ln(L/O)``. Unlike Garman-Klass it stays unbiased when the
    session has a drift, which matters on trending days; the price is higher variance of
    the estimator itself.
    """
    log_hc = np.log(high / close)
    log_ho = np.log(high / open_price)
    log_lc = np.log(low / close)
    log_lo = np.log(low / open_price)
    return log_hc * log_ho + log_lc * log_lo


def realized_variance(
    ohlc: dict[str, pd.DataFrame],
    ticker: str,
    estimator: str = "garman_klass",
) -> pd.Series:
    """Daily variance proxy for one ticker: overnight squared return plus intraday range.

    Parameters
    ----------
    ohlc
        Wide frames keyed ``open``/``high``/``low``/``close``, as
        :meth:`semimc.data.cache.PriceCache.ohlc` returns.
    ticker
        Column to extract.
    estimator
        ``garman_klass`` or ``rogers_satchell``.

    Returns
    -------
    pd.Series
        Daily variance in squared log-return units, indexed by date. Days without a usable
        range are dropped, never filled.

    Raises
    ------
    ValueError
        When the ticker has no intraday range at all, rather than falling back to a
        close-to-close proxy under the same name.
    """
    missing = [k for k in ("open", "high", "low", "close") if k not in ohlc]
    if missing:
        raise ValueError(f"realized variance needs {missing} frames; refetch prices")
    frames = {k: ohlc[k] for k in ("open", "high", "low", "close")}
    for key, frame in frames.items():
        if ticker not in frame.columns:
            raise ValueError(f"no {key} data cached for {ticker}")

    open_price = frames["open"][ticker]
    high = frames["high"][ticker]
    low = frames["low"][ticker]
    close = frames["close"][ticker]

    if not open_price.notna().any() or not high.notna().any():
        raise ValueError(
            f"{ticker} has no intraday range in the cache. Run `semimc fetch --force` to "
            "pick up adjusted open/high/low; HAR will not substitute a close-to-close proxy."
        )

    intraday = (
        garman_klass(open_price, high, low, close)
        if estimator == "garman_klass"
        else rogers_satchell(open_price, high, low, close)
    )
    if estimator not in ("garman_klass", "rogers_satchell"):
        raise ValueError(f"unknown range estimator {estimator!r}")

    overnight = overnight_log_return(open_price, close)
    total = overnight**2 + intraday

    # Garman-Klass can go slightly negative when the close-to-open move exceeds the
    # session range, which happens on stale or mis-stamped quotes. Those days are dropped
    # rather than floored, so a data problem shows up as a gap instead of a fake number.
    negative = int((total <= 0).sum())
    if negative:
        LOGGER.debug("%s: %d non-positive variance proxies dropped", ticker, negative)
    return total[total > 0].dropna().rename(f"{ticker}_rv")


def replace_earnings_variance(
    variance: pd.Series,
    reaction_days: pd.DatetimeIndex,
    window: int = 22,
) -> tuple[pd.Series, int]:
    """Swap earnings-day variance for the trailing median of ordinary days.

    The earnings overlay already injects the report-day gap. If HAR also learned from that
    day's enormous realized variance it would forecast an elevated *diffusion* volatility
    for the whole horizon, and the jump would be counted twice -- once as a jump and once
    as a permanently wider diffusion.

    The replacement is the trailing ``window``-day median of the *non-earnings* days
    before it, which is robust to the very outliers being removed.

    Returns
    -------
    adjusted, n_replaced
    """
    if len(reaction_days) == 0:
        return variance.copy(), 0
    days = variance.index.intersection(reaction_days)
    if len(days) == 0:
        return variance.copy(), 0

    ordinary = variance.drop(index=days)
    trailing = ordinary.rolling(window=window, min_periods=max(3, window // 4)).median()
    # Each earnings day takes the last ordinary median strictly before it.
    replacement = trailing.reindex(variance.index).ffill()

    out = variance.copy()
    usable = days[replacement.reindex(days).notna().to_numpy()]
    out.loc[usable] = replacement.reindex(usable)
    return out, int(len(usable))


def har_features(variance: pd.Series) -> pd.DataFrame:
    """Daily, weekly and monthly average variance -- the three HAR regressors.

    The heterogeneous autoregressive model's premise is that different classes of trader
    act on different horizons, and that each class's activity feeds the next. Averaging
    realized variance over 1, 5 and 22 days is a deliberately crude stand-in for that,
    and it reliably outperforms a long-memory model that is far harder to estimate.

    Returns
    -------
    pd.DataFrame
        Columns ``rv_d``, ``rv_w``, ``rv_m``, aligned to the input index. Rows without a
        full monthly window are dropped.
    """
    clean = variance.dropna()
    return pd.DataFrame(
        {
            "rv_d": clean,
            "rv_w": clean.rolling(5).mean(),
            "rv_m": clean.rolling(22).mean(),
        }
    ).dropna()


def forward_average_variance(variance: pd.Series, horizon: int) -> pd.Series:
    """Average daily variance over the ``horizon`` days *after* each date.

    This is the HAR regression's target. Indexed by the date the forecast is made from, so
    row ``t`` holds the average of ``t+1 .. t+horizon`` -- shifting by ``-horizon`` after
    the rolling mean is what keeps the target strictly forward-looking.
    """
    clean = variance.dropna()
    forward = clean.rolling(window=horizon).mean().shift(-horizon)
    return forward.dropna()


def qlike(forecast_variance: np.ndarray, realized: np.ndarray) -> float:
    """Mean QLIKE loss, ``RV/sigma^2 - ln(RV/sigma^2) - 1``. Lower is better.

    QLIKE is the standard choice for comparing variance forecasts because it is robust to
    noise in the proxy: using a noisy but *unbiased* proxy for the true variance leaves the
    ranking of two forecasts unchanged. Mean squared error on variances does not have that
    property, and in practice rewards whichever forecast happens to be smoother.

    It is also strongly asymmetric -- underforecasting variance is penalised much harder
    than overforecasting it, which is the right way round for a risk model.
    """
    f = np.asarray(forecast_variance, dtype=float)
    r = np.asarray(realized, dtype=float)
    mask = np.isfinite(f) & np.isfinite(r) & (f > 0) & (r > 0)
    if not mask.any():
        return float("nan")
    ratio = r[mask] / f[mask]
    return float(np.mean(ratio - np.log(ratio) - 1.0))


def adjustment_consistency(
    ohlc: dict[str, pd.DataFrame], ticker: str
) -> dict[str, int | float | str]:
    """Check that open, high and low share the close's adjustment basis.

    If one field were adjusted for a split and another were not, the mismatch would show up
    as the close sitting outside the session range on the split date. Counting those rows
    is therefore a direct test of the thing the addendum asks to confirm.

    Returns
    -------
    dict
        ``n_rows``, ``n_close_outside_range``, ``n_open_outside_range``,
        ``n_high_below_low``, and a ``verdict`` string.
    """
    frames = {k: ohlc.get(k) for k in ("open", "high", "low", "close")}
    if any(f is None or ticker not in f.columns for f in frames.values()):
        return {"n_rows": 0, "verdict": "no OHLC cached"}

    joined = pd.DataFrame({k: f[ticker] for k, f in frames.items()}).dropna()
    if joined.empty:
        return {"n_rows": 0, "verdict": "no OHLC cached"}

    tolerance = 1e-6
    close_outside = (joined["close"] > joined["high"] * (1 + tolerance)) | (
        joined["close"] < joined["low"] * (1 - tolerance)
    )
    open_outside = (joined["open"] > joined["high"] * (1 + tolerance)) | (
        joined["open"] < joined["low"] * (1 - tolerance)
    )
    inverted = joined["high"] < joined["low"]

    n_bad = int(close_outside.sum() + open_outside.sum() + inverted.sum())
    return {
        "n_rows": int(len(joined)),
        "n_close_outside_range": int(close_outside.sum()),
        "n_open_outside_range": int(open_outside.sum()),
        "n_high_below_low": int(inverted.sum()),
        "verdict": "consistent" if n_bad == 0 else f"{n_bad} rows violate the range invariant",
    }

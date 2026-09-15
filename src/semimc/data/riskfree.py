"""Risk-free rate: a constant for live forecasts, a historical series for backtests.

Using today's rate at a 2012 forecast origin would be a leak -- a small one, but the drift
policy is built on ``rf + beta * ERP``, and rates moved from 0.05% to 5% across the
backtest window. :class:`RiskFreeCurve` therefore serves an as-of lookup backed by the
3-month T-bill series (FRED ``DTB3``), falling back to the configured constant only when
no series is available, and saying so loudly when it does.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
DEFAULT_SERIES_ID = "DTB3"  # 3-month Treasury bill, secondary market, discount basis


class RiskFreeCurve:
    """As-of lookup of the annualized risk-free rate, expressed as a decimal.

    Parameters
    ----------
    series
        Daily rates indexed by date, as decimals (0.042 for 4.2%). May be ``None``, in
        which case every lookup returns ``fallback``.
    fallback
        Rate used when the series is missing or has no observation at or before the
        requested date.
    """

    def __init__(
        self, series: pd.Series | None, fallback: float, expect_point_in_time: bool = True
    ) -> None:
        self.fallback = float(fallback)
        self.expect_point_in_time = expect_point_in_time
        if series is None or series.empty:
            self.series = None
            # A live forecast is *meant* to use the configured constant, so only warn when
            # a point-in-time series was expected and is missing.
            self._warned = not expect_point_in_time
        else:
            clean = series.dropna().sort_index()
            clean.index = pd.DatetimeIndex(clean.index).tz_localize(None).normalize()
            self.series = clean
            self._warned = True  # nothing to warn about

    @property
    def is_point_in_time(self) -> bool:
        return self.series is not None

    def rate_at(self, as_of: pd.Timestamp) -> float:
        """Most recent rate at or before ``as_of``.

        Strictly backward-looking: an as-of date before the series starts falls back to
        the configured constant rather than reaching forward to the first observation.
        """
        if self.series is None:
            if not self._warned:
                LOGGER.warning(
                    "no risk-free series loaded; using the constant %.4f at every backtest "
                    "origin, so drift is NOT point-in-time",
                    self.fallback,
                )
                self._warned = True
            return self.fallback

        stamp = pd.Timestamp(as_of).tz_localize(None).normalize()
        position = self.series.index.searchsorted(stamp, side="right") - 1
        if position < 0:
            LOGGER.debug(
                "risk-free series starts %s, after requested %s; using fallback",
                self.series.index[0].date(),
                stamp.date(),
            )
            return self.fallback
        value = float(self.series.iloc[position])
        return value if np.isfinite(value) else self.fallback

    def truncated(self, as_of: pd.Timestamp) -> RiskFreeCurve:
        """Copy containing only observations at or before ``as_of`` (leakage guard)."""
        if self.series is None:
            return self
        stamp = pd.Timestamp(as_of).tz_localize(None).normalize()
        return RiskFreeCurve(self.series.loc[:stamp], self.fallback, self.expect_point_in_time)


def load_risk_free_series(path: str | Path) -> pd.Series | None:
    """Read a two-column CSV of ``date,rate``.

    Rates are accepted either as percent (4.2) or as decimals (0.042) and normalized to
    decimals; the two are told apart by the series median, since a 3-month bill has never
    yielded 100%+ and rarely sits below 0.1% in *percent* terms for a whole history.
    """
    csv_path = Path(path)
    if not csv_path.exists():
        return None

    frame = pd.read_csv(csv_path)
    if frame.shape[1] < 2:
        LOGGER.warning("risk-free CSV %s needs at least two columns", csv_path)
        return None

    date_col, rate_col = frame.columns[0], frame.columns[1]
    series = pd.Series(
        pd.to_numeric(frame[rate_col], errors="coerce").to_numpy(),
        index=pd.to_datetime(frame[date_col], errors="coerce"),
    ).dropna()
    series = series[~series.index.isna()]
    if series.empty:
        LOGGER.warning("risk-free CSV %s parsed to zero usable rows", csv_path)
        return None

    if series.abs().median() > 0.5:  # looks like percent
        series = series / 100.0
    return series.sort_index()


def fetch_risk_free_series(series_id: str = DEFAULT_SERIES_ID) -> pd.Series:
    """Download a daily rate series from FRED's public CSV endpoint.

    No API key is required. Values arrive in percent with ``.`` for missing days.
    """
    url = FRED_CSV_URL.format(series_id=series_id)
    frame = pd.read_csv(url)
    date_col = frame.columns[0]
    rate_col = frame.columns[1]
    series = pd.Series(
        pd.to_numeric(frame[rate_col], errors="coerce").to_numpy(),
        index=pd.to_datetime(frame[date_col], errors="coerce"),
    ).dropna()
    return (series / 100.0).sort_index()


def build_curve(config, for_backtest: bool = False) -> RiskFreeCurve:  # noqa: ANN001
    """Build the curve a run should use.

    Live forecasts use the configured constant (the brief's rule). Backtests load the
    historical series so each origin sees the rate that was actually in force.
    """
    if not for_backtest:
        return RiskFreeCurve(None, config.drift.risk_free_rate, expect_point_in_time=False)
    series = load_risk_free_series(config.path(config.backtest.risk_free_series))
    if series is None:
        LOGGER.warning(
            "backtest risk-free series not found at %s; run `semimc fetch --risk-free` to "
            "download it. Falling back to the constant %.4f.",
            config.backtest.risk_free_series,
            config.drift.risk_free_rate,
        )
    return RiskFreeCurve(series, config.drift.risk_free_rate, expect_point_in_time=True)

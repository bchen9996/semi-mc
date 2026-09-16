"""The point-in-time facts a model is given at a forecast origin.

`FitContext` lives in its own module, depended on by both `semimc.data` and
`semimc.models`, because putting it in either one creates an import cycle: the data layer
builds contexts, and the model layer consumes them. Keeping it here means neither package
has to import the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:  # imported for typing only, so no runtime dependency on semimc.data
    from .data.earnings import EarningsCalendar


@dataclass(frozen=True)
class DriftTilt:
    """A regression-implied adjustment to the configured drift (addendum Phase 4B).

    The drift policy decides a baseline expected return that no model may override. A
    tilt is the one sanctioned way to move it, and it is kept as a separate object rather
    than folded into the policy so that the baseline is always recoverable and the tilt is
    always auditable: every number below is a *deviation*, in annual arithmetic units.

    Attributes
    ----------
    tickers
        Column order the arrays below are indexed by.
    point
        ``(n_tickers,)`` annual arithmetic tilt, already shrunk towards zero and capped.
    draws
        ``(n_sets, n_tickers)`` coefficient-uncertainty sets from a block bootstrap over
        *dates*. Demeaned at construction so their column means equal ``point`` exactly,
        which is what lets the simulator add per-path deviations without moving the
        expected drift.
    horizon_days
        Number of simulated steps the tilt applies for. Beyond that the path reverts to
        the policy drift, because a tilt estimated on 21-day forward returns says nothing
        about day 200.
    label
        Which specification produced it, recorded in run manifests.
    """

    tickers: list[str]
    point: np.ndarray
    draws: np.ndarray
    horizon_days: int
    label: str = "tilt"

    @staticmethod
    def build(
        tickers: list[str],
        point: np.ndarray,
        draws: np.ndarray | None,
        horizon_days: int,
        label: str = "tilt",
    ) -> DriftTilt:
        """Construct a tilt, centring ``draws`` on ``point``.

        Centring matters: an uncentred bootstrap cloud would shift the mean drift by
        whatever asymmetry the resampling happened to produce, so coefficient uncertainty
        would silently become a second, unintended drift policy.
        """
        centre = np.asarray(point, dtype=float).reshape(-1)
        if centre.shape[0] != len(tickers):
            raise ValueError(f"point tilt has {centre.shape[0]} entries for {len(tickers)} tickers")
        if draws is None or len(draws) == 0:
            cloud = centre.reshape(1, -1)
        else:
            raw = np.asarray(draws, dtype=float)
            if raw.ndim != 2 or raw.shape[1] != len(tickers):
                raise ValueError(f"draws must be (n_sets, {len(tickers)}), got {raw.shape}")
            cloud = raw - raw.mean(axis=0, keepdims=True) + centre.reshape(1, -1)
        return DriftTilt(
            tickers=list(tickers),
            point=centre,
            draws=cloud,
            horizon_days=int(horizon_days),
            label=label,
        )

    def aligned_to(self, tickers: list[str]) -> DriftTilt | None:
        """Reorder to ``tickers``, or ``None`` when any of them has no tilt.

        Returning ``None`` rather than a zero is deliberate: a model fitted on a ticker the
        regression never saw should run untilted, not run with a silently invented tilt.
        """
        try:
            positions = [self.tickers.index(t) for t in tickers]
        except ValueError:
            return None
        return DriftTilt(
            tickers=list(tickers),
            point=self.point[positions],
            draws=self.draws[:, positions],
            horizon_days=self.horizon_days,
            label=self.label,
        )

    def describe(self) -> dict:
        """Summary for run manifests."""
        return {
            "label": self.label,
            "horizon_days": int(self.horizon_days),
            "n_coefficient_sets": int(self.draws.shape[0]),
            "annual_tilt": {t: float(v) for t, v in zip(self.tickers, self.point, strict=True)},
        }


@dataclass(frozen=True)
class FitContext:
    """Point-in-time facts a model needs but cannot read off its own return panel.

    This is the one addition to the brief's model interface. It exists so that CAPM drift
    (which needs benchmark returns), the earnings overlay (which needs a calendar) and
    point-in-time risk-free rates can be supplied *explicitly* rather than read from
    global state -- global state being precisely how look-ahead leaks in.

    Every field is truncated to ``as_of`` by :meth:`truncated` before a fit, and the
    backtester asserts it.
    """

    as_of: pd.Timestamp
    risk_free_rate: float
    benchmark_returns: pd.DataFrame = field(default_factory=pd.DataFrame)
    market_ticker: str = "SPY"
    sector_ticker: str = "SOXX"
    trading_days_per_year: int = 252
    drift_policy: str = "capm"
    equity_risk_premium: float = 0.05
    shrinkage_weight: float = 0.25
    manual_annual_return: dict[str, float] = field(default_factory=dict)
    earnings: EarningsCalendar | None = None
    drift_tilt: DriftTilt | None = None

    @property
    def market(self) -> pd.Series | None:
        """Market benchmark daily log returns, or ``None`` when unavailable."""
        if self.market_ticker in self.benchmark_returns.columns:
            return self.benchmark_returns[self.market_ticker].dropna()
        return None

    @property
    def sector(self) -> pd.Series | None:
        """Sector benchmark daily log returns, or ``None`` when unavailable."""
        if self.sector_ticker in self.benchmark_returns.columns:
            return self.benchmark_returns[self.sector_ticker].dropna()
        return None

    def truncated(self, as_of: pd.Timestamp) -> FitContext:
        """Copy holding only information available at ``as_of``.

        One deliberate exception: the earnings calendar is **not** truncated. A company
        confirms its next report date weeks ahead and the quarterly cadence is highly
        predictable, so the *schedule* is genuinely known at the origin -- while the
        *reaction* to each report is not, and that is what the model has to forecast.
        Truncating the calendar would instead delete every future date and silently turn
        the earnings overlay into its own base model.

        The asymmetry is kept auditable: models estimate the jump distribution from
        ``calendar.truncated(as_of)``, so past reactions stay strictly point-in-time.
        """
        stamp = pd.Timestamp(as_of).normalize()
        benchmarks = (
            self.benchmark_returns.loc[:stamp]
            if not self.benchmark_returns.empty
            else self.benchmark_returns
        )
        return FitContext(
            as_of=stamp,
            risk_free_rate=self.risk_free_rate,
            benchmark_returns=benchmarks,
            market_ticker=self.market_ticker,
            sector_ticker=self.sector_ticker,
            trading_days_per_year=self.trading_days_per_year,
            drift_policy=self.drift_policy,
            equity_risk_premium=self.equity_risk_premium,
            shrinkage_weight=self.shrinkage_weight,
            manual_annual_return=dict(self.manual_annual_return),
            earnings=self.earnings,  # schedule stays known; see the docstring above
            drift_tilt=self.drift_tilt,
        )

    def with_tilt(self, tilt: DriftTilt | None) -> FitContext:
        """Copy carrying ``tilt``. The only supported way to set one."""
        return FitContext(
            as_of=self.as_of,
            risk_free_rate=self.risk_free_rate,
            benchmark_returns=self.benchmark_returns,
            market_ticker=self.market_ticker,
            sector_ticker=self.sector_ticker,
            trading_days_per_year=self.trading_days_per_year,
            drift_policy=self.drift_policy,
            equity_risk_premium=self.equity_risk_premium,
            shrinkage_weight=self.shrinkage_weight,
            manual_annual_return=dict(self.manual_annual_return),
            earnings=self.earnings,
            drift_tilt=tilt,
        )

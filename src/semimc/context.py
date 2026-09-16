"""The point-in-time facts a model is given at a forecast origin.

`FitContext` lives in its own module, depended on by both `semimc.data` and
`semimc.models`, because putting it in either one creates an import cycle: the data layer
builds contexts, and the model layer consumes them. Keeping it here means neither package
has to import the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:  # imported for typing only, so no runtime dependency on semimc.data
    from .data.earnings import EarningsCalendar


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
        )

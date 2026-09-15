"""Model interface and the drift machinery every model inherits.

A model's job is to produce the *shape* of the return distribution -- volatility, tails,
clustering, co-movement. Its job is explicitly **not** to decide the expected return: that
is set by the configured drift policy and imposed here, identically for every model, so
that a backtest compares distributions rather than accidental differences in sample means.

Shape contract for simulated output, from the brief:
``(n_paths, horizon, n_tickers)`` daily **log** returns.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

from ..data.earnings import EarningsCalendar
from .drift import annual_arithmetic_drift

LOGGER = logging.getLogger(__name__)


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


@runtime_checkable
class ReturnModel(Protocol):
    """The interface every model implements, so models are interchangeable."""

    name: str

    def fit(self, log_returns: pd.DataFrame, context: FitContext) -> ReturnModel:
        """Estimate parameters from daily log returns (index: dates, columns: tickers).

        Must only ever receive data available at the forecast origin.
        """
        ...

    def simulate(self, n_paths: int, horizon: int, rng: np.random.Generator) -> np.ndarray:
        """Return simulated daily log returns with shape ``(n_paths, horizon, n_tickers)``."""
        ...


class NotFittedError(RuntimeError):
    """Raised when ``simulate`` is called before ``fit``."""


class BaseReturnModel(ABC):
    """Common fitting bookkeeping plus enforcement of the drift policy.

    Subclasses implement :meth:`_fit_shape` (estimate whatever describes the distribution's
    shape), :meth:`_simulate_zero_mean` (draw shocks whose per-step mean is zero by
    construction), and :meth:`annualized_variance` (the variance used for the Ito
    correction). This class supplies the mean.
    """

    name: str = "base"

    def __init__(self, dtype: str = "float64") -> None:
        self.dtype = np.dtype(dtype)
        self.tickers: list[str] = []
        self.context: FitContext | None = None
        self.training: pd.DataFrame | None = None
        self._annual_drift: pd.Series | None = None
        self._fitted = False

    # -- fitting --------------------------------------------------------------------

    def fit(self, log_returns: pd.DataFrame, context: FitContext) -> BaseReturnModel:
        """Fit the model to a training panel that ends at ``context.as_of``."""
        if log_returns.empty:
            raise ValueError(f"{self.name}: cannot fit on an empty return panel")

        latest = pd.Timestamp(log_returns.index.max()).normalize()
        as_of = pd.Timestamp(context.as_of).normalize()
        if latest > as_of:
            raise ValueError(
                f"{self.name}: training data ends {latest.date()}, after the forecast origin "
                f"{as_of.date()} -- this would be look-ahead"
            )

        self.tickers = list(log_returns.columns)
        self.context = context
        self.training = log_returns
        self._annual_drift = annual_arithmetic_drift(
            tickers=self.tickers,
            log_returns=log_returns,
            benchmark_returns=context.market,
            policy=context.drift_policy,
            risk_free=context.risk_free_rate,
            equity_risk_premium=context.equity_risk_premium,
            shrinkage_weight=context.shrinkage_weight,
            manual_annual_return=context.manual_annual_return,
            trading_days_per_year=context.trading_days_per_year,
        )
        self._fit_shape(log_returns, context)
        self._fitted = True
        return self

    @abstractmethod
    def _fit_shape(self, log_returns: pd.DataFrame, context: FitContext) -> None:
        """Estimate the distribution's shape parameters. Must not set any drift."""

    # -- drift ----------------------------------------------------------------------

    @abstractmethod
    def annualized_variance(self, horizon: int) -> pd.Series:
        """Annualized variance of log returns over ``horizon``, per ticker.

        Used only for the Ito correction. For a constant-volatility model this is
        ``sigma^2``; for GARCH it is the average of the conditional variance forecast
        across the horizon, which is what the realized average variance will be in
        expectation.
        """

    def daily_log_drift(self, horizon: int) -> pd.Series:
        """Per-day log drift each simulated path is centred on.

        ``(mu_A - sigma_A^2 / 2) / trading_days_per_year`` where ``mu_A`` is the annual
        arithmetic drift from the configured policy. Annualizing with 252 trading days.
        """
        self._require_fit()
        assert self.context is not None and self._annual_drift is not None
        variance = self.annualized_variance(horizon).reindex(self.tickers)
        drift = self._annual_drift.reindex(self.tickers)
        annual_log_drift = drift - 0.5 * variance
        return annual_log_drift / self.context.trading_days_per_year

    @property
    def annual_arithmetic_drift(self) -> pd.Series:
        """The configured target annual arithmetic drift per ticker."""
        self._require_fit()
        assert self._annual_drift is not None
        return self._annual_drift

    # -- simulation -----------------------------------------------------------------

    def simulate(self, n_paths: int, horizon: int, rng: np.random.Generator) -> np.ndarray:
        """Simulate daily log returns, shape ``(n_paths, horizon, n_tickers)``.

        The subclass draws shocks whose mean is zero *by construction* -- normal draws for
        GBM, a pre-demeaned sample for the bootstrap, zero-mean innovations for GARCH --
        and the configured drift is added here.

        The realized mean of a finite draw is deliberately **not** subtracted. Doing so
        would make the output depend on how many paths happened to be simulated together,
        so a run chunked at 1,000 paths would differ from the same seed chunked at 100;
        it would also hide genuine Monte Carlo error in the mean, which the convergence
        diagnostic exists to show. The drift is therefore exact in expectation rather than
        in each particular sample.
        """
        self._require_fit()
        if n_paths <= 0 or horizon <= 0:
            raise ValueError("n_paths and horizon must both be positive")

        shocks = self._simulate_zero_mean(n_paths, horizon, rng)
        expected = (n_paths, horizon, len(self.tickers))
        if shocks.shape != expected:
            raise ValueError(f"{self.name}: expected shape {expected}, got {shocks.shape}")

        drift = self.daily_log_drift(horizon).to_numpy(dtype=np.float64)
        out = shocks + drift.reshape(1, 1, -1)
        return np.asarray(out, dtype=self.dtype)

    @abstractmethod
    def _simulate_zero_mean(
        self, n_paths: int, horizon: int, rng: np.random.Generator
    ) -> np.ndarray:
        """Draw shocks with (approximately) zero mean per step, shape as in :meth:`simulate`."""

    # -- helpers --------------------------------------------------------------------

    def _require_fit(self) -> None:
        if not self._fitted:
            raise NotFittedError(f"{self.name}: call fit() before simulate()")

    def _column(self, ticker: str) -> int:
        return self.tickers.index(ticker)

    def describe(self) -> dict:
        """Small dict of fitted parameters, stored in run manifests for reproducibility."""
        self._require_fit()
        assert self.context is not None
        return {
            "model": self.name,
            "tickers": list(self.tickers),
            "as_of": str(pd.Timestamp(self.context.as_of).date()),
            "n_observations": int(len(self.training)) if self.training is not None else 0,
            "drift_policy": self.context.drift_policy,
            "risk_free_rate": float(self.context.risk_free_rate),
            "annual_arithmetic_drift": {
                k: float(v) for k, v in self.annual_arithmetic_drift.items()
            },
        }

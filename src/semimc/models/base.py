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
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

from ..context import DriftTilt, FitContext
from .drift import annual_arithmetic_drift

LOGGER = logging.getLogger(__name__)

__all__ = ["BaseReturnModel", "DriftTilt", "FitContext", "NotFittedError", "ReturnModel"]


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

        tilt = self._path_drift_tilt(n_paths, horizon, rng)
        if tilt is not None:
            out = out + tilt
        return np.asarray(out, dtype=self.dtype)

    def _path_drift_tilt(
        self, n_paths: int, horizon: int, rng: np.random.Generator
    ) -> np.ndarray | None:
        """Per-path daily log tilt, shape ``(n_paths, horizon, n_tickers)``, or ``None``.

        Three things happen here, and each is a requirement from the addendum.

        **Coefficient uncertainty becomes path dispersion.** Every path is assigned one
        bootstrap coefficient set, so the spread of plausible regressions widens the
        simulated distribution instead of being thrown away in favour of a point estimate.

        **The tilt expires.** A tilt estimated on 21-day forward returns is applied for
        21 steps and then stops; beyond that the path runs on the policy drift alone.

        **No Ito adjustment is needed.** The tilt shifts the *arithmetic* drift, and the
        variance is untouched, so the log-space shift is exactly ``tau / 252`` per day.

        ``daily_log_drift`` deliberately excludes all of this: it stays the drift the
        configured policy asks for, so the policy remains recoverable from a fitted model
        and the tilt stays separately auditable.
        """
        assert self.context is not None
        tilt = self.context.drift_tilt
        if tilt is None:
            return None
        aligned = tilt.aligned_to(self.tickers)
        if aligned is None:
            LOGGER.debug("%s: no drift tilt covers %s; running untilted", self.name, self.tickers)
            return None

        active = min(int(aligned.horizon_days), horizon)
        if active <= 0:
            return None

        sets = aligned.draws
        chosen = sets[rng.integers(0, sets.shape[0], size=n_paths)] if sets.shape[0] > 1 else None
        per_path = chosen if chosen is not None else np.broadcast_to(sets, (n_paths, sets.shape[1]))

        daily = per_path / float(self.context.trading_days_per_year)
        out = np.zeros((n_paths, horizon, len(self.tickers)), dtype=np.float64)
        out[:, :active, :] = daily[:, None, :]
        return out

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
            "drift_tilt": (
                self.context.drift_tilt.describe() if self.context.drift_tilt is not None else None
            ),
        }

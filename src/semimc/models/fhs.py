"""Filtered historical simulation.

The idea, in three steps:

1. **Filter.** Fit a GJR-GARCH to each ticker and divide its returns by the fitted
   conditional volatility. What is left, ``z_t = e_t / sigma_t``, is standardized: the
   volatility clustering has been removed, so the residuals are close to i.i.d. even
   though the returns were not.
2. **Resample by date.** Draw whole *rows* of the standardized residual matrix -- the
   entire cross-section of one historical day at once. This is the part that matters for
   a semiconductor portfolio: it keeps the empirical co-movement, including the fact that
   these names crash together, without assuming any parametric copula.
3. **Re-inflate.** Run each ticker's GARCH recursion forward driven by those resampled
   residuals, starting from today's conditional volatility.

The result keeps the empirical shape of the joint distribution -- fat tails, asymmetric
crashes, tail dependence -- while still being conditional on today's volatility, which a
plain historical bootstrap is not. Its limitation is the mirror image of the bootstrap's:
no simulated day can be more extreme than the worst standardized day in the training
window, though the volatility scaling means the *returns* can exceed anything in history.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .base import BaseReturnModel, FitContext
from .bootstrap import stationary_bootstrap_indices
from .garch_t import fit_gjr_garch_t

LOGGER = logging.getLogger(__name__)


class FilteredHistoricalSimulation(BaseReturnModel):
    """GARCH-filtered, cross-sectionally resampled historical simulation."""

    name = "fhs"

    def __init__(
        self,
        p: int = 1,
        o: int = 1,
        q: int = 1,
        rescale_factor: float = 100.0,
        block_length: int = 1,
        dtype: str = "float64",
    ) -> None:
        super().__init__(dtype=dtype)
        self.p = p
        self.o = o
        self.q = q
        self.rescale_factor = rescale_factor
        self.block_length = block_length
        self.params: dict[str, dict[str, float]] = {}
        self._h0: dict[str, float] = {}
        self._residuals: np.ndarray | None = None  # (n_days, n_tickers), aligned by date

    def _fit_shape(self, log_returns: pd.DataFrame, context: FitContext) -> None:
        standardized: dict[str, pd.Series] = {}

        for ticker in self.tickers:
            params, next_variance, residuals = fit_gjr_garch_t(
                log_returns[ticker],
                p=self.p,
                o=self.o,
                q=self.q,
                rescale_factor=self.rescale_factor,
                label=ticker,
            )
            self.params[ticker] = params
            self._h0[ticker] = next_variance
            standardized[ticker] = residuals

        # Align on dates every ticker has, so a resampled row is one real market day.
        frame = pd.DataFrame(standardized).dropna(how="any")
        if len(frame) < 250:
            raise ValueError(
                f"filtered historical simulation: only {len(frame)} dates where every ticker "
                "has a standardized residual"
            )
        matrix = frame.loc[:, self.tickers].to_numpy(dtype=float)

        # Re-standardize each column to unit variance after alignment: dropping dates can
        # leave the sample variance slightly off 1, and the GARCH recursion assumes 1.
        scale = matrix.std(axis=0, ddof=1)
        scale[scale <= 0] = 1.0
        self._residuals = (matrix - matrix.mean(axis=0)) / scale

    def annualized_variance(self, horizon: int) -> pd.Series:
        """Same geometric average of the conditional variance forecast as GJR-GARCH-t."""
        values: dict[str, float] = {}
        tdpy = self.context.trading_days_per_year if self.context else 252

        for ticker in self.tickers:
            par = self.params[ticker]
            h1 = self._h0[ticker]
            persistence = par["persistence"]
            if persistence >= 1.0 - 1e-8:
                average = h1
            else:
                long_run = par["omega"] / (1.0 - persistence)
                geometric = (1.0 - persistence**horizon) / (horizon * (1.0 - persistence))
                average = long_run + (h1 - long_run) * geometric
            values[ticker] = float(max(average, 1e-12) * tdpy)
        return pd.Series(values, dtype=float).reindex(self.tickers)

    def _simulate_zero_mean(
        self, n_paths: int, horizon: int, rng: np.random.Generator
    ) -> np.ndarray:
        assert self._residuals is not None
        n_days, n_tickers = self._residuals.shape

        # One index per (path, step): the same historical DATE for every ticker, which is
        # what preserves the cross-section.
        indices = stationary_bootstrap_indices(
            n_observations=n_days,
            n_paths=n_paths,
            horizon=horizon,
            block_length=float(self.block_length),
            rng=rng,
        )
        innovations = self._residuals[indices]  # (n_paths, horizon, n_tickers)

        out = np.empty((n_paths, horizon, n_tickers), dtype=float)
        for index, ticker in enumerate(self.tickers):
            par = self.params[ticker]
            omega, alpha, gamma, beta = par["omega"], par["alpha"], par["gamma"], par["beta"]
            variance = np.full(n_paths, self._h0[ticker], dtype=float)
            for step in range(horizon):
                shock = np.sqrt(variance) * innovations[:, step, index]
                out[:, step, index] = shock
                variance = omega + (alpha + gamma * (shock < 0.0)) * shock**2 + beta * variance
                np.maximum(variance, 1e-14, out=variance)
        return out

    def describe(self) -> dict:
        info = super().describe()
        info.update(
            {
                "params": {t: dict(p) for t, p in self.params.items()},
                "residual_rows": int(self._residuals.shape[0])
                if self._residuals is not None
                else 0,
                "block_length": self.block_length,
            }
        )
        return info

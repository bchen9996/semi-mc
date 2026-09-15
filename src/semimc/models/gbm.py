"""Geometric Brownian motion -- the baseline every other model has to beat.

Daily log returns are i.i.d. normal with a constant covariance:

``r_t ~ N(mu_log, Sigma)``,  ``mu_log = (mu_A - sigma^2 / 2) / 252``

Volatility comes from a trailing realized window (one year by default) or from an EWMA;
correlation comes from the full training window. Known failure modes -- no volatility
clustering, tails far too thin, and constant correlation -- are exactly what Phases 4 and
5 are built to fix, and the backtest scorecard is where that gets demonstrated rather than
asserted.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..data.returns import annualized_volatility, ewma_volatility
from .base import BaseReturnModel, FitContext

LOGGER = logging.getLogger(__name__)


def nearest_psd_cholesky(matrix: np.ndarray, jitter: float = 1e-12) -> np.ndarray:
    """Cholesky factor of the nearest positive semi-definite matrix.

    A sample covariance built from unequal histories, or from more tickers than
    observations, can come back very slightly indefinite. Rather than fail, clip the
    eigenvalues at zero, rebuild, and add a tiny ridge until the factorization succeeds.

    Parameters
    ----------
    matrix
        Symmetric covariance matrix, shape ``(n, n)``.
    jitter
        Initial ridge added to the diagonal when the clipped matrix is still not
        factorizable; grows by powers of ten, up to eight attempts.

    Returns
    -------
    np.ndarray
        Lower-triangular factor ``L`` with ``L @ L.T`` approximately equal to ``matrix``.
    """
    symmetric = (matrix + matrix.T) / 2.0
    try:
        return np.linalg.cholesky(symmetric)
    except np.linalg.LinAlgError:
        pass

    eigenvalues, eigenvectors = np.linalg.eigh(symmetric)
    clipped = eigenvectors @ np.diag(np.clip(eigenvalues, 0.0, None)) @ eigenvectors.T
    scale = float(np.mean(np.diag(clipped))) or 1.0
    for attempt in range(8):
        try:
            return np.linalg.cholesky(clipped + np.eye(len(clipped)) * jitter * scale * 10**attempt)
        except np.linalg.LinAlgError:
            continue
    raise np.linalg.LinAlgError("covariance matrix could not be factorized even with a ridge")


class GBMModel(BaseReturnModel):
    """Constant-volatility, constant-correlation lognormal baseline."""

    name = "gbm"

    def __init__(
        self,
        vol_estimator: str = "realized",
        vol_window_days: int = 252,
        ewma_lambda: float = 0.94,
        dtype: str = "float64",
    ) -> None:
        super().__init__(dtype=dtype)
        self.vol_estimator = vol_estimator
        self.vol_window_days = vol_window_days
        self.ewma_lambda = ewma_lambda
        self.daily_vol: pd.Series | None = None
        self.annual_vol: pd.Series | None = None
        self.correlation: pd.DataFrame | None = None
        self._cholesky: np.ndarray | None = None

    def _fit_shape(self, log_returns: pd.DataFrame, context: FitContext) -> None:
        tdpy = context.trading_days_per_year
        annual_vol: dict[str, float] = {}

        for ticker in self.tickers:
            series = log_returns[ticker].dropna()
            if self.vol_estimator == "ewma":
                annual_vol[ticker] = ewma_volatility(series, self.ewma_lambda, tdpy)
            else:
                window = series.iloc[-self.vol_window_days :]
                if len(window) < 20:
                    LOGGER.warning(
                        "%s has only %d observations in the volatility window; using its "
                        "whole history instead",
                        ticker,
                        len(window),
                    )
                    window = series
                annual_vol[ticker] = float(annualized_volatility(window, tdpy))

        self.annual_vol = pd.Series(annual_vol, dtype=float).reindex(self.tickers)
        if not np.isfinite(self.annual_vol).all():
            bad = self.annual_vol[~np.isfinite(self.annual_vol)].index.tolist()
            raise ValueError(f"{self.name}: volatility could not be estimated for {bad}")
        self.daily_vol = self.annual_vol / np.sqrt(tdpy)

        if len(self.tickers) == 1:
            self.correlation = pd.DataFrame([[1.0]], index=self.tickers, columns=self.tickers)
        else:
            self.correlation = log_returns.loc[:, self.tickers].corr().fillna(0.0)
            np.fill_diagonal(self.correlation.values, 1.0)

        sigma = self.daily_vol.to_numpy(dtype=float)
        covariance = self.correlation.to_numpy(dtype=float) * np.outer(sigma, sigma)
        self._cholesky = nearest_psd_cholesky(covariance)

    def annualized_variance(self, horizon: int) -> pd.Series:
        """Constant across the horizon, so simply ``sigma_A^2``."""
        assert self.annual_vol is not None
        return self.annual_vol**2

    def _simulate_zero_mean(
        self, n_paths: int, horizon: int, rng: np.random.Generator
    ) -> np.ndarray:
        assert self._cholesky is not None
        n_tickers = len(self.tickers)
        normals = rng.standard_normal(size=(n_paths, horizon, n_tickers))
        # (paths, steps, k) @ (k, k).T imposes the target covariance on the last axis.
        return normals @ self._cholesky.T

    def describe(self) -> dict:
        info = super().describe()
        assert self.annual_vol is not None and self.correlation is not None
        info.update(
            {
                "vol_estimator": self.vol_estimator,
                "vol_window_days": self.vol_window_days,
                "annual_vol": {k: float(v) for k, v in self.annual_vol.items()},
            }
        )
        return info

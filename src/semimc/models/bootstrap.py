"""Stationary block bootstrap of historical returns.

Politis and Romano's stationary bootstrap: resample *blocks* of consecutive historical
days whose lengths are geometric with mean ``L``. Blocks preserve short-range dependence
-- volatility clustering, momentum reversal -- that an i.i.d. bootstrap destroys, and the
geometric length keeps the resampled series stationary, which a fixed block length does
not.

Rows are drawn **by date**: when several tickers are simulated together, the whole
cross-section of a historical day is taken at once, so the contemporaneous correlation and
the tail co-movement of that day survive resampling. This is the same idea Phase 5's
filtered historical simulation applies to standardized residuals.

Two properties are inherited, for better and worse: the simulated distribution can never
produce a daily move larger than the worst day in the training window, and it assumes the
last five years are representative of the next year.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .base import BaseReturnModel, FitContext

LOGGER = logging.getLogger(__name__)


def optimal_block_length(returns: pd.DataFrame) -> float:
    """Mean block length from ``arch``'s optimal block length estimator.

    ``arch.bootstrap.optimal_block_length`` implements Politis and White (2004) with the
    Patton, Politis and White (2009) correction, returning a length per column. Because
    rows are resampled jointly, the per-ticker lengths are averaged into one; taking the
    maximum instead would let the single most persistent name dictate the block length for
    everyone.

    Returns
    -------
    float
        Mean block length, at least 1.0. Falls back to ``n ** (1/3)`` -- the textbook
        rate -- if the estimator fails.
    """
    from arch.bootstrap import optimal_block_length as _optimal

    clean = returns.dropna(how="any")
    if len(clean) < 20:
        return 1.0
    try:
        table = _optimal(clean)
        value = float(np.mean(np.asarray(table["stationary"], dtype=float)))
    except Exception as exc:  # noqa: BLE001 - estimator is a convenience, not a requirement
        value = float(len(clean) ** (1.0 / 3.0))
        LOGGER.warning("optimal_block_length failed (%s); falling back to n^(1/3)=%.1f", exc, value)
    if not np.isfinite(value) or value < 1.0:
        return 1.0
    return min(value, len(clean) / 2.0)


def stationary_bootstrap_indices(
    n_observations: int,
    n_paths: int,
    horizon: int,
    block_length: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """Row indices for a stationary bootstrap, shape ``(n_paths, horizon)``.

    Each step either continues the previous block (advancing one row, wrapping at the end
    of the sample) or, with probability ``1 / block_length``, jumps to a fresh uniform
    starting row.

    Vectorized by noting that the index at step ``t`` is determined by the most recent
    step at which a new block started: ``idx[t] = (start[s] + (t - s)) mod n`` where ``s``
    is that step. A running maximum finds every ``s`` at once, so no Python loop over
    steps is needed.
    """
    if n_observations < 1:
        raise ValueError("cannot bootstrap from an empty sample")
    probability = 1.0 / max(block_length, 1.0)

    new_block = rng.random((n_paths, horizon)) < probability
    new_block[:, 0] = True
    starts = rng.integers(0, n_observations, size=(n_paths, horizon))

    steps = np.arange(horizon)
    marker = np.where(new_block, steps[None, :], -1)
    last_start_step = np.maximum.accumulate(marker, axis=1)
    offset = steps[None, :] - last_start_step
    chosen_start = np.take_along_axis(starts, last_start_step, axis=1)
    return (chosen_start + offset) % n_observations


class BlockBootstrapModel(BaseReturnModel):
    """Stationary block bootstrap over demeaned historical returns."""

    name = "bootstrap"

    def __init__(self, block_length: int | str = "auto", dtype: str = "float64") -> None:
        super().__init__(dtype=dtype)
        self.block_length_setting = block_length
        self.block_length: float = 1.0
        self._sample: np.ndarray | None = None
        self._variance: pd.Series | None = None

    def _fit_shape(self, log_returns: pd.DataFrame, context: FitContext) -> None:
        panel = log_returns.loc[:, self.tickers].dropna(how="any")
        if len(panel) < 30:
            raise ValueError(
                f"{self.name}: only {len(panel)} complete rows after alignment; need at least 30"
            )

        self.block_length = (
            optimal_block_length(panel)
            if self.block_length_setting == "auto"
            else float(self.block_length_setting)
        )
        LOGGER.debug("%s: block length %.2f over %d rows", self.name, self.block_length, len(panel))

        # Demean before resampling so the sample mean cannot leak in as drift; the
        # configured drift is added back by BaseReturnModel.simulate.
        values = panel.to_numpy(dtype=float)
        self._sample = values - values.mean(axis=0, keepdims=True)
        self._variance = pd.Series(
            values.var(axis=0, ddof=1) * context.trading_days_per_year,
            index=self.tickers,
            dtype=float,
        )

    def annualized_variance(self, horizon: int) -> pd.Series:
        assert self._variance is not None
        return self._variance

    def _simulate_zero_mean(
        self, n_paths: int, horizon: int, rng: np.random.Generator
    ) -> np.ndarray:
        assert self._sample is not None
        indices = stationary_bootstrap_indices(
            n_observations=len(self._sample),
            n_paths=n_paths,
            horizon=horizon,
            block_length=self.block_length,
            rng=rng,
        )
        return self._sample[indices]

    def describe(self) -> dict:
        info = super().describe()
        info.update(
            {
                "block_length": float(self.block_length),
                "block_length_setting": self.block_length_setting,
                "sample_rows": int(len(self._sample)) if self._sample is not None else 0,
            }
        )
        return info

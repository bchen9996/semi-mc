"""Turning fitted regressions into a :class:`~semimc.context.DriftTilt`.

Three addendum requirements meet here.

**Shrinkage and a cap.** A raw regression coefficient, applied at full strength, would let
a noisy monthly panel fit swing a simulation's expected return by more than CAPM itself
does. ``drift = policy drift + lambda * tilt`` with ``lambda`` fixed before any holdout
evaluation, and the annualized result is clipped to ``+-tilt_cap_annual`` regardless of
what the regression says.

**Coefficient uncertainty as path dispersion, not a discarded number.** Rather than reduce
each regression to one point estimate, several hundred coefficient sets are produced by
resampling **dates** (not rows) with a block bootstrap, refitting on each resample, and
predicting from each refit. ``DriftTilt.build`` centres that cloud on the point estimate,
so every draw contributes spread without moving the mean the policy already set.

**A tilt expires.** It is estimated on an ``H``-day forward return and is only asked to
mean something for ``H`` days; :class:`DriftTilt` already carries that as ``horizon_days``,
enforced where it is applied (`models/base.py`), not here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..context import DriftTilt


def shrink_and_cap(raw_annual_tilt: np.ndarray, shrinkage_lambda: float, cap: float) -> np.ndarray:
    """``lambda * raw``, clipped to ``+-cap``. Both applied together since a large raw tilt
    should not be able to evade the cap just because ``lambda`` is small."""
    shrunk = shrinkage_lambda * np.asarray(raw_annual_tilt, dtype=float)
    return np.clip(shrunk, -abs(cap), abs(cap))


def block_bootstrap_date_indices(
    unique_dates: np.ndarray,
    n_bootstraps: int,
    block_length: int,
    rng: np.random.Generator,
) -> list[np.ndarray]:
    """``n_bootstraps`` resamples of ``unique_dates``, each the same length, drawn in
    contiguous blocks so the serial correlation between neighbouring monthly origins
    (overlapping return windows, a shared macro regime) survives the resample instead of
    being averaged away by resampling dates one at a time."""
    n = len(unique_dates)
    if n == 0:
        return []
    out: list[np.ndarray] = []
    for _ in range(n_bootstraps):
        pieces: list[np.ndarray] = []
        filled = 0
        while filled < n:
            start = int(rng.integers(0, n))
            length = min(block_length, n - filled)
            block = np.take(unique_dates, np.arange(start, start + length), mode="wrap")
            pieces.append(block)
            filled += length
        out.append(np.concatenate(pieces)[:n])
    return out


def _fit_point_estimate(design: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Plain OLS coefficients via least squares -- no HAC covariance, since a bootstrap
    draw only needs the point estimate; the uncertainty is what the bootstrap *is*."""
    coefficients, *_ = np.linalg.lstsq(design, target, rcond=None)
    return coefficients


def bootstrap_panel_tilt_draws(
    rows: pd.DataFrame,
    feature_columns: list[str],
    latest_features: pd.DataFrame,
    n_bootstraps: int,
    block_length: int,
    rng: np.random.Generator,
    date_column: str = "date",
    target_column: str = "target",
) -> np.ndarray:
    """Refit the pooled panel regression on ``n_bootstraps`` date-block resamples and
    predict on ``latest_features`` from each refit.

    Returns
    -------
    np.ndarray
        ``(n_bootstraps, n_tickers)`` predicted ``horizon``-day net-of-exposure returns,
        in ``latest_features``'s row order.
    """
    usable = rows.dropna(subset=[*feature_columns, target_column, date_column])
    unique_dates = usable[date_column].unique()
    if len(unique_dates) < 5:
        raise ValueError(f"only {len(unique_dates)} unique dates; too few to block-bootstrap")

    resamples = block_bootstrap_date_indices(unique_dates, n_bootstraps, block_length, rng)
    prediction_design = np.column_stack(
        [np.ones(len(latest_features)), latest_features.loc[:, feature_columns].to_numpy()]
    )

    draws = np.empty((len(resamples), len(latest_features)), dtype=float)
    for row_index, sampled_dates in enumerate(resamples):
        # A date can appear more than once in a block resample; repeat its rows to match,
        # so a date drawn twice counts twice in the refit exactly as a real resample would.
        counts = pd.Series(sampled_dates).value_counts()
        pieces = [usable[usable[date_column] == date] for date in counts.index if counts[date] > 0]
        repeats = [counts[date] for date in counts.index if counts[date] > 0]
        if not pieces:
            draws[row_index] = np.nan
            continue
        resampled = pd.concat(
            [piece for piece, n in zip(pieces, repeats, strict=True) for _ in range(int(n))],
            ignore_index=True,
        )
        design = np.column_stack(
            [np.ones(len(resampled)), resampled.loc[:, feature_columns].to_numpy()]
        )
        coefficients = _fit_point_estimate(design, resampled[target_column].to_numpy())
        draws[row_index] = prediction_design @ coefficients
    return draws


def bootstrap_sector_tilt_draws(
    rows: pd.DataFrame,
    latest_growth: float,
    n_bootstraps: int,
    block_length: int,
    rng: np.random.Generator,
    date_column: str = "date",
    feature_column: str = "industry_yoy",
    target_column: str = "target",
) -> np.ndarray:
    """Same idea as :func:`bootstrap_panel_tilt_draws`, for the single-series sector-tilt
    regression. Returns ``(n_bootstraps,)`` predicted forward sector-factor returns."""
    usable = rows.dropna(subset=[feature_column, target_column, date_column])
    unique_dates = usable[date_column].unique()
    if len(unique_dates) < 5 or not np.isfinite(latest_growth):
        return np.zeros(n_bootstraps)

    resamples = block_bootstrap_date_indices(unique_dates, n_bootstraps, block_length, rng)
    draws = np.empty(len(resamples), dtype=float)
    for index, sampled_dates in enumerate(resamples):
        counts = pd.Series(sampled_dates).value_counts()
        pieces = [usable[usable[date_column] == date] for date in counts.index if counts[date] > 0]
        repeats = [counts[date] for date in counts.index if counts[date] > 0]
        if not pieces:
            draws[index] = 0.0
            continue
        resampled = pd.concat(
            [piece for piece, n in zip(pieces, repeats, strict=True) for _ in range(int(n))],
            ignore_index=True,
        )
        design = np.column_stack([np.ones(len(resampled)), resampled[feature_column].to_numpy()])
        coefficients = _fit_point_estimate(design, resampled[target_column].to_numpy())
        draws[index] = coefficients[0] + coefficients[1] * latest_growth
    return draws


def build_drift_tilt(
    tickers: list[str],
    relative_point_h: pd.Series,
    relative_draws_h: np.ndarray,
    sector_point_h: float,
    sector_draws_h: np.ndarray,
    beta_sector: pd.Series,
    shrinkage_lambda: float,
    tilt_cap_annual: float,
    horizon_days: int,
    trading_days_per_year: int,
    label: str,
) -> DriftTilt:
    """Combine the relative and sector-broadcast tilts into one :class:`DriftTilt`.

    ``drift = CAPM drift + beta_sector * sector_tilt + relative_tilt`` (the addendum's own
    formula), each side converted from an ``H``-day predicted return to an annualized
    arithmetic tilt before shrinkage and the cap are applied -- shrinking and capping in
    daily units would make the cap's meaning depend on the horizon it happened to be fit at.
    """
    annualize = trading_days_per_year / horizon_days
    beta = beta_sector.reindex(tickers).fillna(0.0).to_numpy()

    point_annual = (
        relative_point_h.reindex(tickers).to_numpy() + beta * sector_point_h
    ) * annualize
    point = shrink_and_cap(point_annual, shrinkage_lambda, tilt_cap_annual)

    n_sets = min(relative_draws_h.shape[0], len(sector_draws_h))
    draws_annual = (
        relative_draws_h[:n_sets] + beta.reshape(1, -1) * sector_draws_h[:n_sets].reshape(-1, 1)
    ) * annualize
    draws = shrink_and_cap(draws_annual, shrinkage_lambda, tilt_cap_annual)

    return DriftTilt.build(
        tickers=tickers, point=point, draws=draws, horizon_days=horizon_days, label=label
    )

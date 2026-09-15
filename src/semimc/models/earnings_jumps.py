"""Earnings-jump overlay.

Semiconductor stocks do not move smoothly. Four days a year, a name can gap 10-25% on a
single print, and no volatility model fitted to ordinary days will ever produce that from
a smooth diffusion. This overlay wraps *any* base model and injects a draw from the
ticker's own empirical distribution of earnings-day abnormal returns on each scheduled
report inside the horizon.

Two details keep it honest:

**No double counting.** The abnormal earnings return is stripped out of the base model's
training data first -- each reaction day's return is replaced with ``beta * benchmark`` for
that day -- so the base model never sees the jumps it is about to have added back.

**No free drift.** The jump distribution is demeaned before use. If a ticker historically
popped on earnings, that is a *drift* contribution, and Section 8 of the brief forbids any
model from inheriting a historical mean. The jumps supply variance and skew; the
configured drift policy still supplies the expected return.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..data.earnings import abnormal_earnings_returns, strip_earnings_returns
from ..data.returns import estimate_beta
from .base import BaseReturnModel, FitContext

LOGGER = logging.getLogger(__name__)


class EarningsCalendarMissing(RuntimeError):
    """Raised when no usable earnings history exists for the requested tickers."""


class EarningsJumpModel(BaseReturnModel):
    """Wraps a base model with scheduled earnings jumps."""

    def __init__(
        self,
        base_model: BaseReturnModel,
        min_earnings_observations: int = 8,
        strip_from_training: bool = True,
        dtype: str = "float64",
    ) -> None:
        super().__init__(dtype=dtype)
        self.base = base_model
        self.min_earnings_observations = min_earnings_observations
        self.strip_from_training = strip_from_training
        self.name = f"{base_model.name}+earnings"
        self.jump_samples: dict[str, np.ndarray] = {}
        self.betas: dict[str, float] = {}
        self.n_stripped: dict[str, int] = {}
        self._offsets: dict[str, list[int]] = {}
        self._horizon_cached: int | None = None

    # -- fitting --------------------------------------------------------------------

    def _fit_shape(self, log_returns: pd.DataFrame, context: FitContext) -> None:
        calendar = context.earnings
        if calendar is None or calendar.is_empty():
            raise EarningsCalendarMissing(
                "the earnings overlay needs a populated earnings calendar; run "
                "`semimc fetch --earnings` to seed config/earnings_calendar.csv, then "
                "review and extend it. No dates are invented."
            )

        benchmark = context.market
        if benchmark is None:
            raise ValueError("the earnings overlay needs benchmark returns to compute abnormals")

        training = log_returns.copy()
        usable: list[str] = []

        # Estimation uses only reports that had already happened at the origin. The full
        # calendar is kept on the context for the forward schedule, but nothing about a
        # future report's *outcome* may touch the fitted distribution.
        past = calendar.truncated(context.as_of)

        for ticker in self.tickers:
            series = log_returns[ticker].dropna()
            reaction_days = past.reaction_days(ticker, pd.DatetimeIndex(series.index))
            if len(reaction_days) < self.min_earnings_observations:
                LOGGER.warning(
                    "%s has %d earnings reaction days in the training window (need %d); "
                    "it gets no jumps, only the base model",
                    ticker,
                    len(reaction_days),
                    self.min_earnings_observations,
                )
                self.jump_samples[ticker] = np.zeros(0)
                self.n_stripped[ticker] = 0
                continue

            # Beta is measured off non-earnings days so the jumps themselves do not
            # inflate it -- an earnings gap is idiosyncratic, not market exposure.
            ordinary = series.drop(index=series.index.intersection(reaction_days))
            beta = estimate_beta(ordinary, benchmark)
            self.betas[ticker] = float(beta) if np.isfinite(beta) else 1.0

            abnormal = abnormal_earnings_returns(
                series, benchmark, reaction_days, self.betas[ticker]
            )
            if len(abnormal) < self.min_earnings_observations:
                self.jump_samples[ticker] = np.zeros(0)
                self.n_stripped[ticker] = 0
                continue

            values = abnormal.to_numpy(dtype=float)
            self.jump_samples[ticker] = values - values.mean()  # demeaned: variance, not drift
            usable.append(ticker)

            if self.strip_from_training:
                stripped, n_replaced = strip_earnings_returns(
                    series, benchmark, reaction_days, self.betas[ticker]
                )
                training.loc[stripped.index, ticker] = stripped
                self.n_stripped[ticker] = n_replaced
            else:
                self.n_stripped[ticker] = 0

        if not usable:
            raise EarningsCalendarMissing(
                f"no ticker in {self.tickers} has at least "
                f"{self.min_earnings_observations} earnings days in the training window"
            )

        self.base.fit(training, context)

    # -- horizon bookkeeping ---------------------------------------------------------

    def _resolve_offsets(self, horizon: int) -> dict[str, list[int]]:
        """Horizon steps carrying a scheduled report, per ticker."""
        if self._horizon_cached == horizon:
            return self._offsets
        assert self.context is not None
        calendar = self.context.earnings
        offsets: dict[str, list[int]] = {}
        for ticker in self.tickers:
            if self.jump_samples.get(ticker, np.zeros(0)).size == 0 or calendar is None:
                offsets[ticker] = []
                continue
            offsets[ticker] = calendar.future_reaction_offsets(
                ticker,
                self.context.as_of,
                horizon,
                self.context.trading_days_per_year,
            )
            if not offsets[ticker]:
                LOGGER.warning(
                    "%s has an estimated jump distribution but no scheduled report between "
                    "%s and %d trading days later, so no jumps will be injected. The "
                    "calendar likely needs future dates added.",
                    ticker,
                    pd.Timestamp(self.context.as_of).date(),
                    horizon,
                )
        self._offsets = offsets
        self._horizon_cached = horizon
        return offsets

    def annualized_variance(self, horizon: int) -> pd.Series:
        """Base variance plus the variance the scheduled jumps add.

        ``n_jumps`` independent draws of variance ``v_jump`` over ``horizon`` days add
        ``n_jumps * v_jump / horizon`` to the average daily variance.
        """
        base_variance = self.base.annualized_variance(horizon)
        offsets = self._resolve_offsets(horizon)
        tdpy = self.context.trading_days_per_year if self.context else 252

        out: dict[str, float] = {}
        for ticker in self.tickers:
            extra = 0.0
            samples = self.jump_samples.get(ticker, np.zeros(0))
            n_jumps = len(offsets.get(ticker, []))
            if samples.size > 1 and n_jumps:
                extra = float(samples.var(ddof=1)) * n_jumps / horizon * tdpy
            out[ticker] = float(base_variance.get(ticker, np.nan)) + extra
        return pd.Series(out, dtype=float).reindex(self.tickers)

    # -- simulation -----------------------------------------------------------------

    def _simulate_zero_mean(
        self, n_paths: int, horizon: int, rng: np.random.Generator
    ) -> np.ndarray:
        shocks = self.base._simulate_zero_mean(n_paths, horizon, rng)
        offsets = self._resolve_offsets(horizon)

        for index, ticker in enumerate(self.tickers):
            samples = self.jump_samples.get(ticker, np.zeros(0))
            steps = offsets.get(ticker, [])
            if samples.size == 0 or not steps:
                continue
            for step in steps:
                draws = rng.choice(samples, size=n_paths, replace=True)
                shocks[:, step, index] += draws
        return shocks

    def describe(self) -> dict:
        info = super().describe()
        info.update(
            {
                "base_model": self.base.describe(),
                "earnings_days_used": {t: int(v.size) for t, v in self.jump_samples.items()},
                "earnings_days_stripped": dict(self.n_stripped),
                "betas_for_abnormal": {k: float(v) for k, v in self.betas.items()},
                "jump_std": {
                    t: float(v.std(ddof=1)) if v.size > 1 else 0.0
                    for t, v in self.jump_samples.items()
                },
            }
        )
        return info

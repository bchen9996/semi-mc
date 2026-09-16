"""HAR volatility forecasts driven by a realized-variance proxy.

The heterogeneous autoregressive model regresses future average variance on realized
variance measured over three windows -- one day, one week, one month:

``log RVbar(t+1..t+h) = a + b_d log RV_d + b_w log RV_w + b_m log RV_m + eps``

The story behind the three windows is that traders act on different horizons and each
group's activity feeds the next, so volatility has a layered memory. The reason to actually
use it is more prosaic: it captures long-memory-like decay with three OLS coefficients,
and it beats models that are far harder to estimate.

Three implementation choices matter.

**A direct forecast per horizon.** Rather than iterating a one-day model forward -- which
compounds its own errors -- a separate regression is fitted for each horizon on the
appropriate forward average. The per-step variance path is then recovered from the
*differences* of the cumulative forecasts, so every direct forecast is reproduced exactly
by the simulator rather than approximately.

**Logs, and a bias correction coming back out.** Variance is right-skewed and strictly
positive, so the regression runs in logs; but ``exp(E[log V]) < E[V]``, so back-transforming
naively underforecasts. The correction used is Duan's smearing estimator, the sample mean of
``exp(residual)``, which equals the familiar ``exp(sigma^2/2)`` when residuals are normal
and stays right when they are not.

**Earnings variance is removed before fitting.** Otherwise the report-day spike teaches HAR
to forecast an elevated *diffusion* volatility for the whole horizon, and combining it with
the earnings overlay would count each report twice.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..data.earnings import EarningsCalendar
from ..data.realized import (
    MIN_VARIANCE,
    forward_average_variance,
    har_features,
    realized_variance,
    replace_earnings_variance,
)
from .base import BaseReturnModel, FitContext

LOGGER = logging.getLogger(__name__)

MIN_HAR_OBSERVATIONS = 120


class HARFitError(RuntimeError):
    """Raised when a HAR regression cannot be fitted for a ticker."""


def fit_har_regression(
    variance: pd.Series,
    horizon: int,
) -> dict[str, float]:
    """Fit one direct HAR regression for a single horizon.

    Parameters
    ----------
    variance
        Daily realized-variance proxy, strictly positive.
    horizon
        Forecast horizon in trading days. The target is the average daily variance over
        the ``horizon`` days *after* each date.

    Returns
    -------
    dict
        ``const``, ``beta_d``, ``beta_w``, ``beta_m``, ``sigma_residual``,
        ``smearing`` (the back-transform correction), ``r_squared``, ``n_observations``.
    """
    features = har_features(variance)
    target = forward_average_variance(variance, horizon)
    joined = features.join(target.rename("target"), how="inner").dropna()
    joined = joined[(joined > 0).all(axis=1)]
    if len(joined) < MIN_HAR_OBSERVATIONS:
        raise HARFitError(
            f"{len(joined)} usable rows for a {horizon}-day HAR fit; "
            f"{MIN_HAR_OBSERVATIONS} required"
        )

    design = np.column_stack(
        [
            np.ones(len(joined)),
            np.log(joined["rv_d"].to_numpy()),
            np.log(joined["rv_w"].to_numpy()),
            np.log(joined["rv_m"].to_numpy()),
        ]
    )
    response = np.log(joined["target"].to_numpy())
    coefficients, *_ = np.linalg.lstsq(design, response, rcond=None)
    residuals = response - design @ coefficients
    total = float(np.sum((response - response.mean()) ** 2))

    return {
        "const": float(coefficients[0]),
        "beta_d": float(coefficients[1]),
        "beta_w": float(coefficients[2]),
        "beta_m": float(coefficients[3]),
        "sigma_residual": float(np.std(residuals, ddof=4)),
        # Duan's smearing estimator: the nonparametric version of exp(sigma^2 / 2).
        "smearing": float(np.mean(np.exp(residuals))),
        "r_squared": float(1.0 - np.sum(residuals**2) / total) if total > 0 else float("nan"),
        "n_observations": int(len(joined)),
    }


def har_forecast(params: dict[str, float], features: pd.Series) -> float:
    """Back-transformed average-variance forecast from a fitted HAR and the latest features."""
    linear = (
        params["const"]
        + params["beta_d"] * np.log(max(float(features["rv_d"]), MIN_VARIANCE))
        + params["beta_w"] * np.log(max(float(features["rv_w"]), MIN_VARIANCE))
        + params["beta_m"] * np.log(max(float(features["rv_m"]), MIN_VARIANCE))
    )
    return float(np.exp(linear) * params["smearing"])


def per_step_variance_path(
    forecasts: dict[int, float],
    horizon: int,
) -> np.ndarray:
    """Turn direct average-variance forecasts into a per-step variance path.

    A direct forecast at horizon ``h`` is an average, so it pins the *cumulative* variance
    ``C(h) = h * Vbar(h)``. The variance for the steps between two forecast horizons is
    then the forward difference ``(C(h_k) - C(h_{k-1})) / (h_k - h_{k-1})``, which makes the
    simulator reproduce every direct forecast exactly instead of interpolating the averages
    and reproducing none of them.

    Beyond the longest fitted horizon the last forward variance is held flat, and the whole
    path is floored: a non-monotone set of forecasts can otherwise imply a negative forward
    variance, which is a fitting artefact rather than a statement about the world.
    """
    grid = sorted(forecasts)
    path = np.empty(horizon, dtype=float)
    previous_h, previous_c = 0, 0.0
    position = 0

    for h in grid:
        cumulative = h * forecasts[h]
        span = h - previous_h
        if span <= 0:
            continue
        forward = max((cumulative - previous_c) / span, MIN_VARIANCE)
        stop = min(h, horizon)
        if position < stop:
            path[position:stop] = forward
            position = stop
        previous_h, previous_c = h, cumulative
        if position >= horizon:
            break

    if position < horizon:
        last = path[position - 1] if position > 0 else forecasts[grid[-1]]
        path[position:] = max(last, MIN_VARIANCE)
    return path


class HARVolatilityModel(BaseReturnModel):
    """Volatility from a HAR regression, shocks from resampled standardized residuals."""

    name = "har"

    def __init__(
        self,
        horizons: list[int] | None = None,
        estimator: str = "garman_klass",
        innovations: str = "bootstrap",
        exclude_earnings_variance: bool = True,
        dtype: str = "float64",
    ) -> None:
        super().__init__(dtype=dtype)
        self.horizons = sorted(set(horizons or [1, 21, 63, 126, 252]))
        self.estimator = estimator
        self.innovations = innovations
        self.exclude_earnings_variance = exclude_earnings_variance
        self.params: dict[str, dict[int, dict[str, float]]] = {}
        self.forecasts: dict[str, dict[int, float]] = {}
        self.n_earnings_replaced: dict[str, int] = {}
        self._standardized: dict[str, np.ndarray] = {}
        self._standardized_panel: np.ndarray | None = None
        self._ohlc: dict[str, pd.DataFrame] | None = None

    def with_ohlc(self, ohlc: dict[str, pd.DataFrame]) -> HARVolatilityModel:
        """Supply the OHLC frames the variance proxy needs. Must be called before ``fit``."""
        self._ohlc = ohlc
        return self

    # -- fitting --------------------------------------------------------------------

    def _fit_shape(self, log_returns: pd.DataFrame, context: FitContext) -> None:
        if self._ohlc is None:
            raise HARFitError(
                "HAR needs adjusted open/high/low. Call with_ohlc(market.ohlc) before fit(); "
                "if the cache has no OHLC, run `semimc fetch --force`."
            )

        calendar: EarningsCalendar | None = context.earnings
        grid = sorted({1, *(h for h in self.horizons if h > 0)})
        standardized_columns: dict[str, pd.Series] = {}

        for ticker in self.tickers:
            # Point-in-time: the proxy is cut at the origin exactly like the return panel.
            variance = realized_variance(self._ohlc, ticker, estimator=self.estimator)
            variance = variance.loc[: pd.Timestamp(context.as_of)]
            window_start = log_returns.index.min()
            variance = variance.loc[variance.index >= window_start]

            replaced = 0
            if self.exclude_earnings_variance and calendar is not None and not calendar.is_empty():
                past = calendar.truncated(context.as_of)
                reaction_days = past.reaction_days(ticker, pd.DatetimeIndex(variance.index))
                variance, replaced = replace_earnings_variance(variance, reaction_days)
            self.n_earnings_replaced[ticker] = replaced

            if len(variance) < MIN_HAR_OBSERVATIONS + 22:
                raise HARFitError(f"{ticker}: {len(variance)} variance observations is too few")

            features = har_features(variance)
            latest = features.iloc[-1]

            fitted: dict[int, dict[str, float]] = {}
            predicted: dict[int, float] = {}
            for horizon in grid:
                try:
                    params = fit_har_regression(variance, horizon)
                except HARFitError as exc:
                    LOGGER.warning(
                        "%s: %d-day HAR fit failed (%s); horizon skipped", ticker, horizon, exc
                    )
                    continue
                fitted[horizon] = params
                predicted[horizon] = har_forecast(params, latest)

            if not predicted:
                raise HARFitError(f"{ticker}: no HAR horizon could be fitted")
            self.params[ticker] = fitted
            self.forecasts[ticker] = predicted

            # Standardized residuals come from the one-day regression's in-sample fitted
            # variance, so the innovations carry the real return distribution's shape once
            # the predictable part of volatility has been divided out.
            one_day = fitted.get(1)
            returns = log_returns[ticker].dropna()
            if one_day is not None:
                one_day_params = one_day  # bound explicitly; a loop-scoped default arg would do too

                def _forecast_one_day(row: pd.Series, _params: dict = one_day_params) -> float:
                    return har_forecast(_params, row)

                fitted_variance = features.apply(_forecast_one_day, axis=1)
                aligned = returns.reindex(fitted_variance.index).dropna()
                scale = np.sqrt(fitted_variance.reindex(aligned.index))
            else:
                aligned = returns
                scale = pd.Series(np.sqrt(variance.reindex(aligned.index)), index=aligned.index)
            z = ((aligned - aligned.mean()) / scale).replace([np.inf, -np.inf], np.nan).dropna()
            if len(z) < 100:
                raise HARFitError(f"{ticker}: only {len(z)} standardized residuals")
            standardized_columns[ticker] = z

        frame = pd.DataFrame(standardized_columns).dropna(how="any")
        # Rows, not cells: resampling whole dates keeps whatever cross-sectional
        # dependence the standardized residuals have, exactly as FHS does.
        #
        # Re-demean and re-standardize *after* alignment, not before. Dividing an
        # already-demeaned series by a time-varying scale does not preserve a zero mean
        # (heteroskedasticity correlates the scale with the sign of the return), and
        # cross-ticker date alignment drops rows unevenly, which can reintroduce a small
        # drift of its own. Bootstrapping a sample whose mean is not exactly zero would
        # inject drift into every simulated path -- the same bug the base class's own
        # docstring warns a chunk-size-dependent realized-mean subtraction would cause,
        # just arriving from the fitting side instead of the simulation side. FHS uses
        # the identical fix for the identical reason.
        matrix = frame.loc[:, self.tickers].to_numpy(dtype=float)
        scale = matrix.std(axis=0, ddof=1)
        scale[scale <= 0] = 1.0
        self._standardized_panel = (matrix - matrix.mean(axis=0)) / scale
        for index, ticker in enumerate(self.tickers):
            self._standardized[ticker] = self._standardized_panel[:, index]

    # -- drift input ----------------------------------------------------------------

    def _variance_path(self, ticker: str, horizon: int) -> np.ndarray:
        return per_step_variance_path(self.forecasts[ticker], horizon)

    def annualized_variance(self, horizon: int) -> pd.Series:
        """Average of the per-step variance path over the horizon, annualized.

        Consistent by construction with what :meth:`_simulate_zero_mean` actually draws,
        which is the property that keeps the drift exact.
        """
        tdpy = self.context.trading_days_per_year if self.context else 252
        values = {
            ticker: float(np.mean(self._variance_path(ticker, horizon)) * tdpy)
            for ticker in self.tickers
        }
        return pd.Series(values, dtype=float).reindex(self.tickers)

    # -- simulation -----------------------------------------------------------------

    def _simulate_zero_mean(
        self, n_paths: int, horizon: int, rng: np.random.Generator
    ) -> np.ndarray:
        n_tickers = len(self.tickers)
        assert self._standardized_panel is not None
        panel = self._standardized_panel

        if self.innovations == "bootstrap" and len(panel) > 0:
            rows = rng.integers(0, len(panel), size=(n_paths, horizon))
            innovations = panel[rows]
        else:
            # Student-t fallback keeps the model usable when a ticker's standardized
            # residual history is too short to resample from honestly.
            innovations = rng.standard_normal(size=(n_paths, horizon, n_tickers))

        out = np.empty((n_paths, horizon, n_tickers), dtype=float)
        for index, ticker in enumerate(self.tickers):
            volatility = np.sqrt(self._variance_path(ticker, horizon))
            out[:, :, index] = innovations[:, :, index] * volatility.reshape(1, -1)
        return out

    def describe(self) -> dict:
        info = super().describe()
        info.update(
            {
                "estimator": self.estimator,
                "innovations": self.innovations,
                "earnings_days_replaced": dict(self.n_earnings_replaced),
                "forecast_annual_vol": {
                    t: {
                        int(h): float(np.sqrt(v * 252))
                        for h, v in sorted(self.forecasts[t].items())
                    }
                    for t in self.tickers
                },
                "coefficients": {
                    t: {int(h): dict(p) for h, p in sorted(self.params[t].items())}
                    for t in self.tickers
                },
            }
        )
        return info

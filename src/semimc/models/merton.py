"""Merton jump-diffusion.

Daily log return, before the configured drift is added:

``e_t = sigma_d * Z_t + (sum of N_t jumps Y ~ N(mu_J, sigma_J^2)) - lambda_d * mu_J``

with ``N_t ~ Poisson(lambda_d)``. The final term centres the jump component so the shock is
zero-mean in log space, which is what :class:`~semimc.models.base.BaseReturnModel` requires.

**What it is for.** The earnings overlay handles gaps whose *date* is known in advance. This
model handles the ones whose date is not: an export-control announcement, a customer cutting
guidance, a fab losing a quarter of output to a power failure. GJR-GARCH-t widens its tails
by raising conditional volatility *after* a shock; a jump process can produce the shock out
of a calm state, which is closer to how those headlines actually arrive.

**Why it may not earn its place.** On daily data the diffusion/jump split is weakly
identified -- a larger jump component offset by a smaller diffusion volatility gives almost
the same likelihood. :func:`refit_stability` measures exactly that, and the backtest, not
the likelihood, decides whether the model stays.

The compensator
---------------

The brief asks for the ``-lambda * k`` compensator so the configured drift survives. It
turns out that falls straight out of the existing base class with no change to it. The base
class simulates ``mu_A - V/2 + shocks`` annually, where ``V`` is whatever
:meth:`annualized_variance` returns, so ``E[S_T/S_0] = exp(mu_A * T)`` requires ``V = 2K``
with ``K = log E[exp(shocks)]``. For this process

``K = sigma_d^2/2 + lambda*(k - mu_J)``,  ``k = exp(mu_J + sigma_J^2/2) - 1``

so ``V = sigma_d^2 + 2*lambda*(k - mu_J)``, and the net log drift becomes
``mu_A - sigma_d^2/2 - lambda*k`` -- the textbook Merton drift. The consequence worth
stating: ``annualized_variance`` here is the *Ito-correction* variance, deliberately not the
log-return variance ``sigma_d^2 + lambda*(mu_J^2 + sigma_J^2)``. The two differ by a term of
order ``lambda * mu_J^2``, which is small but not zero.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from scipy import optimize, stats

from ..data.earnings import EarningsCalendar
from .base import BaseReturnModel, FitContext
from .gbm import nearest_psd_cholesky

LOGGER = logging.getLogger(__name__)

MAX_JUMPS_PER_DAY = 3  # Poisson mass beyond this is negligible at the intensities fitted here


class MertonFitError(RuntimeError):
    """Raised when the jump-diffusion likelihood cannot be optimized for a ticker."""


def _negative_log_likelihood(
    theta: np.ndarray,
    returns: np.ndarray,
    trading_days_per_year: int,
    max_jumps: int,
) -> float:
    """Negative log-likelihood of demeaned daily returns under a truncated Poisson mixture.

    ``theta`` is ``(sigma_annual, lambda_annual, mu_J, sigma_J)``. The density is a mixture
    over the number of jumps in a day, each component normal:

    ``f(e) = sum_n Poisson(n; lambda_d) * N(e; n*mu_J - lambda_d*mu_J, sigma_d^2 + n*sigma_J^2)``
    """
    sigma_annual, lambda_annual, mu_jump, sigma_jump = theta
    if sigma_annual <= 0 or lambda_annual < 0 or sigma_jump <= 0:
        return 1e12

    lambda_daily = lambda_annual / trading_days_per_year
    diffusion_variance = sigma_annual**2 / trading_days_per_year
    centring = lambda_daily * mu_jump

    counts = np.arange(max_jumps + 1)
    weights = stats.poisson.pmf(counts, mu=lambda_daily)
    means = counts * mu_jump - centring
    variances = diffusion_variance + counts * sigma_jump**2
    if np.any(variances <= 0):
        return 1e12

    # (n_obs, n_components); logsumexp keeps the small-probability components from
    # underflowing to zero before they are summed.
    residuals = returns[:, None] - means[None, :]
    log_components = (
        np.log(np.maximum(weights, 1e-300))[None, :]
        - 0.5 * np.log(2.0 * np.pi * variances)[None, :]
        - 0.5 * residuals**2 / variances[None, :]
    )
    total = np.logaddexp.reduce(log_components, axis=1)
    if not np.all(np.isfinite(total)):
        return 1e12
    return float(-np.sum(total))


def fit_merton(
    log_returns: pd.Series,
    trading_days_per_year: int = 252,
    max_jumps: int = MAX_JUMPS_PER_DAY,
    label: str = "series",
) -> dict[str, float]:
    """Maximum-likelihood fit of a Merton jump-diffusion to one ticker's daily returns.

    Returns are demeaned first: the mean is the drift policy's job, never the model's.

    Starting values matter more than usual here, because the likelihood has a ridge along
    which jump intensity trades off against diffusion volatility. Several starts are tried
    and the best log-likelihood wins; each start fixes ``lambda``, ``mu_J`` and ``sigma_J``
    and then *solves* for the diffusion volatility that makes total model variance match
    the sample variance, so every start begins on the correct variance level and differs
    only in how the variance is split.

    Returns
    -------
    dict
        ``sigma_annual``, ``lambda_annual``, ``mu_jump``, ``sigma_jump``, ``k``,
        ``loglikelihood``, ``n_observations``, ``jump_variance_share``.
    """
    series = log_returns.dropna()
    if len(series) < 250:
        raise MertonFitError(f"{label}: {len(series)} observations is too few for a stable fit")

    values = (series - series.mean()).to_numpy(dtype=float)
    sample_variance_annual = float(np.var(values, ddof=1)) * trading_days_per_year

    starts: list[np.ndarray] = []
    for lambda_annual in (2.0, 6.0, 12.0, 26.0):
        for sigma_jump in (0.03, 0.06, 0.12):
            for mu_jump in (-0.01, 0.0):
                jump_variance = lambda_annual * (mu_jump**2 + sigma_jump**2)
                diffusion = np.sqrt(max(sample_variance_annual - jump_variance, 1e-4))
                starts.append(np.array([diffusion, lambda_annual, mu_jump, sigma_jump]))

    bounds = [(0.01, 3.0), (0.1, 60.0), (-0.30, 0.30), (0.005, 0.50)]
    best: optimize.OptimizeResult | None = None
    for start in starts:
        clipped = np.clip(start, [b[0] for b in bounds], [b[1] for b in bounds])
        try:
            result = optimize.minimize(
                _negative_log_likelihood,
                clipped,
                args=(values, trading_days_per_year, max_jumps),
                method="L-BFGS-B",
                bounds=bounds,
            )
        except (ValueError, FloatingPointError):
            continue
        if result.success and np.isfinite(result.fun) and (best is None or result.fun < best.fun):
            best = result

    if best is None:
        raise MertonFitError(f"{label}: every Merton starting value failed to converge")

    sigma_annual, lambda_annual, mu_jump, sigma_jump = (float(v) for v in best.x)
    k = float(np.expm1(mu_jump + sigma_jump**2 / 2.0))
    jump_variance = lambda_annual * (mu_jump**2 + sigma_jump**2)
    return {
        "sigma_annual": sigma_annual,
        "lambda_annual": lambda_annual,
        "mu_jump": mu_jump,
        "sigma_jump": sigma_jump,
        "k": k,
        "loglikelihood": float(-best.fun),
        "n_observations": int(len(values)),
        "jump_variance_share": float(jump_variance / max(jump_variance + sigma_annual**2, 1e-12)),
    }


def threshold_jump_estimate(
    log_returns: pd.Series,
    conditional_volatility: pd.Series,
    multiplier: float = 4.0,
    trading_days_per_year: int = 252,
) -> dict[str, float]:
    """Non-likelihood cross-check: count days exceeding ``multiplier`` conditional sigmas.

    The MLE and this estimator disagree in a specific, informative way. The threshold rule
    can only ever find jumps that are large relative to the GARCH volatility *of that day*,
    so it misses jumps that arrive during an already-volatile stretch and finds none at all
    in a model where volatility has adapted. When the MLE reports far more jumps than this
    does, the MLE is describing fat tails rather than discrete events.

    Parameters
    ----------
    log_returns
        Demeaned daily log returns.
    conditional_volatility
        Daily conditional volatility from a GARCH filter, same index.
    multiplier
        How many conditional sigmas count as a jump.

    Returns
    -------
    dict
        ``lambda_annual``, ``mu_jump``, ``sigma_jump``, ``n_jumps``, ``threshold_multiplier``.
    """
    joined = pd.concat([log_returns, conditional_volatility], axis=1, join="inner").dropna()
    if joined.empty:
        return {"lambda_annual": float("nan"), "n_jumps": 0, "threshold_multiplier": multiplier}
    residual = joined.iloc[:, 0] - joined.iloc[:, 0].mean()
    exceeds = residual.abs() > multiplier * joined.iloc[:, 1]
    jumps = residual[exceeds]
    return {
        "lambda_annual": float(len(jumps) / len(joined) * trading_days_per_year),
        "mu_jump": float(jumps.mean()) if len(jumps) else 0.0,
        "sigma_jump": float(jumps.std(ddof=1)) if len(jumps) > 1 else float("nan"),
        "n_jumps": int(len(jumps)),
        "threshold_multiplier": float(multiplier),
    }


def refit_stability(
    log_returns: pd.Series,
    n_windows: int = 6,
    window_years: float = 5.0,
    trading_days_per_year: int = 252,
) -> pd.DataFrame:
    """Refit on rolling sub-windows and report how much each parameter moves.

    The addendum asks for this explicitly, and it is the honest answer to "should I believe
    these parameters". A coefficient of variation near or above 1 across refits means the
    parameter is not identified by the data, whatever its standard error says.

    Returns
    -------
    pd.DataFrame
        One row per window plus a final ``cv`` row holding each parameter's coefficient of
        variation across windows.
    """
    series = log_returns.dropna()
    window = int(round(window_years * trading_days_per_year))
    if len(series) < window + n_windows:
        n_windows = max(2, (len(series) - window) // 21)
    step = max(1, (len(series) - window) // max(1, n_windows - 1))

    rows: list[dict] = []
    for index in range(n_windows):
        stop = len(series) - index * step
        start = stop - window
        if start < 0:
            break
        chunk = series.iloc[start:stop]
        try:
            params = fit_merton(chunk, trading_days_per_year=trading_days_per_year)
        except MertonFitError:
            continue
        params["window_end"] = chunk.index[-1]
        rows.append(params)

    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows).set_index("window_end").sort_index()
    numeric = frame.select_dtypes("number")
    cv = (numeric.std(ddof=1) / numeric.mean().abs()).to_frame().T
    cv.index = pd.Index(["cv"], name="window_end")
    return pd.concat([frame, cv])


class MertonJumpDiffusion(BaseReturnModel):
    """Merton jump-diffusion fitted per ticker, with correlated diffusions."""

    name = "merton"

    def __init__(
        self,
        max_jumps_per_day: int = MAX_JUMPS_PER_DAY,
        exclude_earnings_days: bool = True,
        dtype: str = "float64",
    ) -> None:
        super().__init__(dtype=dtype)
        self.max_jumps_per_day = max_jumps_per_day
        self.exclude_earnings_days = exclude_earnings_days
        self.params: dict[str, dict[str, float]] = {}
        self.n_earnings_excluded: dict[str, int] = {}
        self._diffusion_correlation: np.ndarray | None = None

    # -- fitting --------------------------------------------------------------------

    def _fit_shape(self, log_returns: pd.DataFrame, context: FitContext) -> None:
        calendar: EarningsCalendar | None = context.earnings
        residuals: dict[str, pd.Series] = {}

        for ticker in self.tickers:
            series = log_returns[ticker].dropna()
            excluded = 0

            # Scheduled earnings gaps belong to the overlay. Leaving them in here would
            # have the jump process absorb them, and stacking the two models would then
            # count every report twice.
            if self.exclude_earnings_days and calendar is not None and not calendar.is_empty():
                past = calendar.truncated(context.as_of)
                reaction_days = past.reaction_days(ticker, pd.DatetimeIndex(series.index))
                overlap = series.index.intersection(reaction_days)
                if len(overlap):
                    series = series.drop(index=overlap)
                    excluded = int(len(overlap))

            self.n_earnings_excluded[ticker] = excluded
            self.params[ticker] = fit_merton(
                series,
                trading_days_per_year=context.trading_days_per_year,
                max_jumps=self.max_jumps_per_day,
                label=ticker,
            )
            residuals[ticker] = series - series.mean()

        if len(self.tickers) == 1:
            self._diffusion_correlation = np.eye(1)
        else:
            frame = pd.DataFrame(residuals).dropna(how="any")
            correlation = frame.loc[:, self.tickers].corr().fillna(0.0).to_numpy(dtype=float)
            np.fill_diagonal(correlation, 1.0)
            self._diffusion_correlation = correlation

    # -- drift input ----------------------------------------------------------------

    def annualized_variance(self, horizon: int) -> pd.Series:
        """Ito-correction variance ``sigma_d^2 + 2*lambda*(k - mu_J)``.

        Not the log-return variance -- see this module's docstring. Returning this is what
        makes the net log drift equal ``mu_A - sigma_d^2/2 - lambda*k``, the Merton
        compensator, without the base class needing to know jumps exist.

        Constant in ``horizon``: the process is i.i.d. across days.
        """
        values = {}
        for ticker in self.tickers:
            par = self.params[ticker]
            values[ticker] = float(
                par["sigma_annual"] ** 2 + 2.0 * par["lambda_annual"] * (par["k"] - par["mu_jump"])
            )
        return pd.Series(values, dtype=float).reindex(self.tickers)

    # -- simulation -----------------------------------------------------------------

    def _simulate_zero_mean(
        self, n_paths: int, horizon: int, rng: np.random.Generator
    ) -> np.ndarray:
        """Correlated diffusions plus independent compensated jumps.

        The sum of ``N`` i.i.d. normal jumps is itself normal, ``N(N*mu_J, N*sigma_J^2)``,
        so a day's jump component is drawn in closed form from the Poisson count rather
        than by looping over individual jumps.

        Jumps are independent across tickers. That is a real limitation -- an export-control
        headline hits the whole sector at once -- and it makes this model understate
        portfolio tail risk relative to single-name tail risk. The factor model is the one
        that handles shared shocks; this is noted in MODEL_NOTES rather than papered over.
        """
        assert self.context is not None
        n_tickers = len(self.tickers)
        tdpy = self.context.trading_days_per_year

        assert self._diffusion_correlation is not None
        cholesky = nearest_psd_cholesky(self._diffusion_correlation)
        normals = rng.standard_normal(size=(n_paths, horizon, n_tickers)) @ cholesky.T

        out = np.empty((n_paths, horizon, n_tickers), dtype=float)
        for index, ticker in enumerate(self.tickers):
            par = self.params[ticker]
            daily_sigma = par["sigma_annual"] / np.sqrt(tdpy)
            lambda_daily = par["lambda_annual"] / tdpy
            mu_jump = par["mu_jump"]
            sigma_jump = par["sigma_jump"]

            counts = rng.poisson(lam=lambda_daily, size=(n_paths, horizon))
            jump_noise = rng.standard_normal(size=(n_paths, horizon))
            jumps = counts * mu_jump + np.sqrt(counts) * sigma_jump * jump_noise

            out[:, :, index] = daily_sigma * normals[:, :, index] + jumps - lambda_daily * mu_jump
        return out

    def describe(self) -> dict:
        info = super().describe()
        info.update(
            {
                "params": {t: dict(p) for t, p in self.params.items()},
                "earnings_days_excluded": dict(self.n_earnings_excluded),
                "expected_jumps_per_year": {
                    t: float(self.params[t]["lambda_annual"]) for t in self.tickers
                },
            }
        )
        return info

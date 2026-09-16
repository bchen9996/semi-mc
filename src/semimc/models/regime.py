"""Two-regime Markov-switching volatility.

Semiconductor volatility does not smoothly mean-revert; it sits calm for long stretches and
then jumps to a materially higher level for months at a time, tracking the industry's own
boom-bust capex cycle more than any single day's news. A GARCH recursion mean-reverts
towards *one* long-run variance; a two-state Markov-switching model lets that long-run
level itself change, which is a better description of a cycle than a bigger persistence
parameter would be.

Fitted with ``statsmodels.tsa.regime_switching.markov_regression.MarkovRegression`` --
**never** ``import statsmodels.api``, which drags in the Kalman-filter state-space code and
is blocked by Windows Application Control on this machine (`tests/test_imports.py` guards
this). The submodule import used here is unaffected.

Three rules the addendum is explicit about, and that a naive implementation gets wrong:

**Filtered, not smoothed, start state.** A smoothed probability at day ``t`` was computed
using observations after ``t`` -- it is a description of the past made with hindsight. A
forecast made *at* ``t`` can only condition on what a filter has seen by ``t``. Starting a
simulation from the smoothed probability would leak the future into the one place leakage
is easiest to miss: the starting point.

**Drift is a deviation, re-centred on the stationary average.** Regime means enter the
model only as offsets from the configured drift, and the offsets are shifted so their
*stationary-distribution-weighted* average is exactly zero -- not their simple average,
because the calm regime is visited far more often than the turbulent one. Getting this
wrong would make the model's long-run drift depend on how volatile the sector happened to
be, which the whole drift-policy machinery exists to prevent.

**Daily-to-weekly mapping is explicit.** If a daily fit produces a regime whose expected
duration is a handful of days, the model has found a volatility cluster, not an industry
cycle, and the addendum asks to try weekly returns instead. The mapping back divides the
weekly variance by 5 and holds the regime fixed across each 5-trading-day block, which is
recorded in ``describe()`` so results can be told apart.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .base import BaseReturnModel, FitContext
from .gbm import nearest_psd_cholesky

LOGGER = logging.getLogger(__name__)

MIN_EXPECTED_DURATION_DAYS = 15.0  # below this, a "regime" is really just a vol cluster


class RegimeFitError(RuntimeError):
    """Raised when the Markov-switching optimizer fails or does not converge."""


def _order_by_variance(result) -> tuple[np.ndarray, np.ndarray, np.ndarray]:  # noqa: ANN001
    """Return (means, variances, transition) with regime 0 forced to be the calmer one.

    ``MarkovRegression`` has no ordering constraint, so which fitted regime index is "calm"
    is arbitrary and can flip between refits on nearly identical data. Sorting once, here,
    is what keeps "regime 0" meaning the same thing in every manifest and chart.

    ``result.params`` is a plain ``ndarray`` in this statsmodels version, not a labelled
    ``Series`` -- names live on ``result.model.param_names`` and have to be looked up by
    position instead.
    """
    k = result.model.k_regimes
    names = list(result.model.param_names)
    values = np.asarray(result.params, dtype=float)
    lookup = {name: values[position] for position, name in enumerate(names)}

    variances = np.array([lookup[f"sigma2[{i}]"] for i in range(k)])
    if "const" in lookup:  # switching_trend=False: one shared mean, not regime-indexed
        means = np.full(k, float(lookup["const"]))
    else:
        means = np.array([lookup.get(f"const[{i}]", 0.0) for i in range(k)])

    order = np.argsort(variances)
    transition = result.regime_transition[:, :, 0]  # time-invariant: constant across t
    reordered_transition = transition[np.ix_(order, order)]
    return means[order], variances[order], reordered_transition


def fit_regime_switching(
    log_returns: pd.Series,
    search_reps: int = 20,
    switching_mean: bool = False,
    trading_days_per_year: int = 252,
    rescale_factor: float = 100.0,
    label: str = "series",
):
    """Fit a two-regime Markov-switching model, ordered so regime 0 is the calmer one.

    Parameters
    ----------
    log_returns
        Daily (or weekly, by the caller's choice) log returns.
    search_reps
        Random restarts; the best log-likelihood wins. Regime-switching likelihoods have
        multiple local optima, so one start is not reliable.
    switching_mean
        Whether the regime mean is allowed to switch, or only the variance.
    rescale_factor
        Returns are multiplied by this before fitting. Same reason as GARCH: at daily
        log-return scale (~1e-2) the likelihood surface is flat enough that the optimizer
        stalls with non-finite filtered probabilities even when it reports convergence;
        every parameter is converted back to unscaled units afterwards.
    label
        Used in error messages.

    Returns
    -------
    dict
        ``means`` (unscaled, order-sorted), ``variances``, ``transition`` (2x2,
        ``transition[i, j] = P(next=i | current=j)``), ``stationary`` (2,),
        ``filtered_last`` (2,), ``expected_duration`` (2,), ``loglikelihood``, ``converged``.
    """
    from statsmodels.tsa.regime_switching.markov_regression import MarkovRegression

    series = log_returns.dropna()
    if len(series) < 250:
        raise RegimeFitError(f"{label}: {len(series)} observations is too few for a stable fit")

    scaled = series.to_numpy(dtype=float) * rescale_factor
    model = MarkovRegression(
        scaled,
        k_regimes=2,
        trend="c",
        switching_variance=True,
        switching_trend=switching_mean,
    )
    try:
        result = model.fit(search_reps=search_reps, disp=False)
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed error
        raise RegimeFitError(f"{label}: Markov-switching optimizer failed: {exc}") from exc

    if not np.all(np.isfinite(result.params)):
        raise RegimeFitError(f"{label}: Markov-switching returned non-finite parameters")

    filtered_check = np.asarray(result.filtered_marginal_probabilities)
    if not np.all(np.isfinite(filtered_check)):
        raise RegimeFitError(
            f"{label}: Markov-switching produced non-finite filtered probabilities"
        )

    means, variances, transition = _order_by_variance(result)
    means = means / rescale_factor
    variances = variances / rescale_factor**2

    # Stationary distribution: the left eigenvector of the transition matrix for
    # eigenvalue 1, i.e. pi such that pi = T @ pi (columns sum to 1 under this convention).
    eigenvalues, eigenvectors = np.linalg.eig(transition)
    stationary_index = int(np.argmin(np.abs(eigenvalues - 1.0)))
    stationary = np.real(eigenvectors[:, stationary_index])
    stationary = np.clip(stationary, 0.0, None)
    stationary = stationary / stationary.sum() if stationary.sum() > 0 else np.array([0.5, 0.5])

    persistence = np.diag(transition)
    with np.errstate(divide="ignore"):
        safe_persistence = np.maximum(1.0 - persistence, 1e-12)
        expected_duration = np.where(persistence < 1.0, 1.0 / safe_persistence, np.inf)

    # The fit's own regime order may differ from the variance-sorted order used above, so
    # filtered probabilities are re-mapped through the same sort before being returned.
    param_names = list(result.model.param_names)
    param_values = np.asarray(result.params, dtype=float)
    raw_variances = np.array([param_values[param_names.index(f"sigma2[{i}]")] for i in range(2)])
    variance_order = np.argsort(raw_variances)
    filtered_raw = result.filtered_marginal_probabilities[-1]
    filtered_last = np.asarray(filtered_raw)[variance_order]

    retvals = getattr(result, "mle_retvals", None)
    converged = bool(retvals.get("converged", True)) if isinstance(retvals, dict) else True

    return {
        "means": means,
        "variances": variances,
        "transition": transition,
        "stationary": stationary,
        "filtered_last": filtered_last,
        "expected_duration": expected_duration,
        "loglikelihood": float(result.llf),
        "converged": converged,
        "trading_days_per_year": trading_days_per_year,
    }


def recentred_means(means: np.ndarray, stationary: np.ndarray) -> np.ndarray:
    """Shift regime means so the *stationary-weighted* average is exactly zero.

    Not the simple average of the two means -- the calm regime is visited far more often,
    so weighting by how much time is actually spent in each regime is what keeps the
    long-run drift equal to the configured policy rather than a coin flip's worth of it.
    """
    centre = float(np.dot(means, stationary))
    return means - centre


class RegimeSwitchingModel(BaseReturnModel):
    """Two-regime Markov-switching volatility, GBM diffusion within each regime."""

    name = "regime"

    def __init__(
        self,
        search_reps: int = 20,
        switching_mean: bool = False,
        min_expected_duration_days: float = MIN_EXPECTED_DURATION_DAYS,
        force_independent: bool = False,
        dtype: str = "float64",
    ) -> None:
        super().__init__(dtype=dtype)
        self.search_reps = search_reps
        self.switching_mean = switching_mean
        self.min_expected_duration_days = min_expected_duration_days
        self.force_independent = force_independent
        self.fits: dict[str, dict] = {}
        self.frequency_used: dict[str, str] = {}
        self._correlation: np.ndarray | None = None

    # -- fitting --------------------------------------------------------------------

    def _fit_shape(self, log_returns: pd.DataFrame, context: FitContext) -> None:
        residuals: dict[str, pd.Series] = {}

        for ticker in self.tickers:
            daily = log_returns[ticker].dropna()
            fitted = fit_regime_switching(
                daily,
                search_reps=self.search_reps,
                switching_mean=self.switching_mean,
                trading_days_per_year=context.trading_days_per_year,
                label=ticker,
            )
            used_frequency = "daily"

            if np.min(fitted["expected_duration"]) < self.min_expected_duration_days:
                weekly = daily.resample("W-FRI").sum().dropna()
                if len(weekly) >= 100:
                    try:
                        weekly_fit = fit_regime_switching(
                            weekly,
                            search_reps=self.search_reps,
                            switching_mean=self.switching_mean,
                            trading_days_per_year=context.trading_days_per_year,
                            label=f"{ticker} (weekly)",
                        )
                    except RegimeFitError:
                        weekly_fit = None
                    if weekly_fit is not None and np.min(weekly_fit["expected_duration"]) >= 3.0:
                        # Map weekly variance/mean back to a daily scale by dividing by 5;
                        # the regime itself is then held fixed across each 5-day block at
                        # simulation time (see _simulate_zero_mean).
                        weekly_fit["variances"] = weekly_fit["variances"] / 5.0
                        weekly_fit["means"] = weekly_fit["means"] / 5.0
                        weekly_fit["expected_duration"] = weekly_fit["expected_duration"] * 5.0
                        fitted = weekly_fit
                        used_frequency = "weekly (mapped to daily)"
                    else:
                        LOGGER.warning(
                            "%s: daily regimes are short-lived (expected duration %s days) "
                            "and the weekly refit did not help; keeping the daily fit",
                            ticker,
                            np.round(fitted["expected_duration"], 1),
                        )

            fitted["means"] = recentred_means(fitted["means"], fitted["stationary"])
            self.fits[ticker] = fitted
            self.frequency_used[ticker] = used_frequency

            standardized = (daily - daily.mean()) / daily.std(ddof=1)
            residuals[ticker] = standardized

        if len(self.tickers) == 1 or self.force_independent:
            self._correlation = np.eye(len(self.tickers))
        else:
            frame = pd.DataFrame(residuals).dropna(how="any")
            correlation = frame.loc[:, self.tickers].corr().fillna(0.0).to_numpy(dtype=float)
            np.fill_diagonal(correlation, 1.0)
            self._correlation = correlation

    # -- drift input ----------------------------------------------------------------

    def annualized_variance(self, horizon: int) -> pd.Series:
        """Expected variance over the horizon under the regime-switching forecast.

        Computed by propagating the filtered starting distribution forward through the
        transition matrix for each step and averaging the resulting mixture variance --
        the same "start from what is known now, average what is expected" logic GARCH
        uses, specialised to two discrete states instead of a continuous recursion.
        """
        tdpy = self.context.trading_days_per_year if self.context else 252
        values = {}
        for ticker in self.tickers:
            fit = self.fits[ticker]
            path_variance = self._mixture_variance_path(fit, horizon)
            values[ticker] = float(np.mean(path_variance) * tdpy)
        return pd.Series(values, dtype=float).reindex(self.tickers)

    @staticmethod
    def _mixture_variance_path(fit: dict, horizon: int) -> np.ndarray:
        probability = fit["filtered_last"].copy()
        transition = fit["transition"]
        variances = fit["variances"]
        means = fit["means"]
        out = np.empty(horizon, dtype=float)
        for step in range(horizon):
            # Mixture second moment about the (regime-weighted) mean, not just the
            # weighted variance -- differing regime means also contribute dispersion.
            mixture_mean = float(np.dot(probability, means))
            out[step] = float(np.dot(probability, variances + (means - mixture_mean) ** 2))
            probability = transition @ probability
        return out

    # -- simulation -----------------------------------------------------------------

    def _simulate_zero_mean(
        self, n_paths: int, horizon: int, rng: np.random.Generator
    ) -> np.ndarray:
        """Simulate the regime chain per path, then a correlated Gaussian within each state.

        The chain is genuinely simulated (not just its expected variance), so paths that
        happen to land in the turbulent regime look like turbulent-regime paths -- wide,
        possibly with a shifted mean if ``switching_mean`` is on -- rather than every path
        carrying the horizon-averaged blend.
        """
        n_tickers = len(self.tickers)
        assert self._correlation is not None
        cholesky = nearest_psd_cholesky(self._correlation)
        normals = rng.standard_normal(size=(n_paths, horizon, n_tickers)) @ cholesky.T

        out = np.empty((n_paths, horizon, n_tickers), dtype=float)
        for index, ticker in enumerate(self.tickers):
            fit = self.fits[ticker]
            transition = fit["transition"]  # [:, j] = P(next | current=j); columns sum to 1
            p_stay = transition[0, 0]
            p_switch_from_1 = transition[0, 1]

            weekly_mapped = self.frequency_used[ticker].startswith("weekly")
            block = 5 if weekly_mapped else 1

            state = rng.choice(
                [0, 1], size=n_paths, p=fit["filtered_last"] / fit["filtered_last"].sum()
            )
            for step in range(horizon):
                if step % block == 0 and step > 0:
                    draw = rng.random(size=n_paths)
                    from_calm = state == 0
                    stay_calm = draw < p_stay
                    switch_to_calm = draw < p_switch_from_1
                    state = np.where(
                        from_calm,
                        np.where(stay_calm, 0, 1),
                        np.where(switch_to_calm, 0, 1),
                    )
                sigma = np.sqrt(fit["variances"][state])
                mean = fit["means"][state]
                out[:, step, index] = mean + sigma * normals[:, step, index]
        return out

    def describe(self) -> dict:
        info = super().describe()
        info.update(
            {
                "frequency_used": dict(self.frequency_used),
                "regimes": {
                    t: {
                        "annual_vol": [
                            float(np.sqrt(v * self.context.trading_days_per_year))
                            for v in self.fits[t]["variances"]
                        ],
                        "expected_duration_days": [
                            float(d) for d in self.fits[t]["expected_duration"]
                        ],
                        "stationary_probability": [float(p) for p in self.fits[t]["stationary"]],
                        "filtered_probability_at_origin": [
                            float(p) for p in self.fits[t]["filtered_last"]
                        ],
                        "transition_matrix": self.fits[t]["transition"].tolist(),
                        "converged": bool(self.fits[t]["converged"]),
                    }
                    for t in self.tickers
                },
            }
        )
        return info

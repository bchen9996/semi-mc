"""GJR-GARCH(1,1) with Student-t innovations.

The conditional variance recursion, on demeaned log returns ``e_t``:

``h_t = omega + (alpha + gamma * 1[e_{t-1} < 0]) * e_{t-1}^2 + beta * h_{t-1}``

Two features matter for semiconductors. **Clustering**: a violent day raises tomorrow's
expected variance, so a simulation started in October 2018 stays volatile for weeks
instead of reverting to a five-year average by the next session. **Asymmetry**: the
``gamma`` term lets a fall of a given size raise future volatility more than a rise of the
same size, which is what the data shows. Student-t innovations then supply the fat tails
that a normal GARCH still misses.

Simulation starts from *today's* conditional variance -- the point of the model -- and
mean-reverts towards ``omega / (1 - alpha - gamma/2 - beta)`` at the estimated rate.

Returns are rescaled by 100 before fitting: the optimizer works with variances near 1
rather than near 1e-4, where the log-likelihood surface is flat enough to stall. Every
parameter is converted back afterwards.
"""

from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd
from scipy import stats

from .base import BaseReturnModel, FitContext
from .gbm import nearest_psd_cholesky

LOGGER = logging.getLogger(__name__)

MIN_NU = 2.05  # below this a Student-t has no finite variance, so it cannot be standardized


class GarchFitError(RuntimeError):
    """Raised when the GARCH optimizer fails for a ticker."""


def standardized_t(nu: float, size: tuple[int, ...], rng: np.random.Generator) -> np.ndarray:
    """Draw Student-t variates rescaled to unit variance.

    A raw ``t_nu`` has variance ``nu / (nu - 2)``; dividing by its square root makes the
    innovation's scale the conditional volatility itself, which is what the GARCH
    recursion assumes.
    """
    safe_nu = max(float(nu), MIN_NU)
    raw = rng.standard_t(df=safe_nu, size=size)
    return raw / np.sqrt(safe_nu / (safe_nu - 2.0))


def fit_gjr_garch_t(
    log_returns: pd.Series,
    p: int = 1,
    o: int = 1,
    q: int = 1,
    rescale_factor: float = 100.0,
    label: str = "series",
) -> tuple[dict[str, float], float, pd.Series]:
    """Fit one GJR-GARCH(1,1)-t and return its parameters in unscaled units.

    Shared by :class:`GJRGarchTModel` and by filtered historical simulation, which needs
    exactly the same filter but then resamples the standardized residuals instead of
    drawing fresh Student-t innovations.

    Parameters
    ----------
    log_returns
        Daily log returns for one ticker. Demeaned internally.
    p, o, q
        GARCH orders; ``o = 1`` supplies the GJR asymmetry term.
    rescale_factor
        Returns are multiplied by this before fitting for optimizer stability, and every
        variance parameter is divided back out by its square afterwards.
    label
        Name used in error messages.

    Returns
    -------
    params, next_variance, standardized_residuals
        ``params`` holds omega, alpha, gamma, beta, nu and persistence in daily
        log-return units; ``next_variance`` is the one-step-ahead conditional variance
        implied by the last observation; ``standardized_residuals`` are the in-sample
        ``e_t / sigma_t``, which have unit variance by construction.
    """
    result, scaled = _fit_arch_gjr_t(log_returns, p, o, q, rescale_factor, label)
    params, next_variance, standardized, _ = _unpack_fit(result, scaled, rescale_factor, label)
    return params, next_variance, standardized


def _fit_arch_gjr_t(
    log_returns: pd.Series, p: int, o: int, q: int, rescale_factor: float, label: str
):  # noqa: ANN202 -- returns (arch ARCHModelResult, scaled demeaned returns)
    """The optimizer call itself, shared by :func:`fit_gjr_garch_t` and
    :class:`GJRGarchTParamUncertaintyModel` (which also needs the fit's covariance)."""
    from arch import arch_model

    series = log_returns.dropna()
    if len(series) < 250:
        raise GarchFitError(f"{label}: {len(series)} observations is too few for a stable fit")

    demeaned = series - series.mean()
    scaled = demeaned * rescale_factor

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = arch_model(scaled, mean="Zero", vol="GARCH", p=p, o=o, q=q, dist="t", rescale=False)
        try:
            result = model.fit(disp="off", show_warning=False)
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed error
            raise GarchFitError(f"{label}: GARCH optimizer failed: {exc}") from exc
    return result, scaled


def _unpack_fit(
    result, scaled: pd.Series, rescale_factor: float, label: str
) -> tuple[dict[str, float], float, pd.Series, dict[str, float]]:  # noqa: ANN001
    """Unscaled parameters, next-step variance and standardized residuals from an arch
    result, plus the scaled-unit filter state (last residual, last conditional
    variance) the parameter-uncertainty model needs to re-derive the next-step variance
    under each drawn parameter set."""
    raw = {k: float(v) for k, v in result.params.items()}
    omega = raw.get("omega", np.nan)
    alpha = raw.get("alpha[1]", 0.0)
    gamma = raw.get("gamma[1]", 0.0)
    beta = raw.get("beta[1]", 0.0)
    nu = raw.get("nu", 8.0)
    if not np.isfinite([omega, alpha, gamma, beta, nu]).all():
        raise GarchFitError(f"{label}: GARCH returned non-finite parameters {raw}")

    scale_sq = rescale_factor**2
    last_variance = float(result.conditional_volatility.iloc[-1] ** 2)
    last_resid = float(scaled.iloc[-1])
    next_variance_scaled = (
        omega + (alpha + (gamma if last_resid < 0 else 0.0)) * last_resid**2 + beta * last_variance
    )

    params = {
        "omega": omega / scale_sq,
        "alpha": alpha,
        "gamma": gamma,
        "beta": beta,
        "nu": nu,
        "persistence": alpha + gamma / 2.0 + beta,
        "loglikelihood": float(result.loglikelihood),
    }
    standardized = (scaled / result.conditional_volatility).dropna()
    state = {
        "last_resid_scaled": last_resid,
        "last_variance_scaled": last_variance,
        "max_variance_scaled": float(result.conditional_volatility.max() ** 2),
    }
    return params, next_variance_scaled / scale_sq, standardized, state


class GJRGarchTModel(BaseReturnModel):
    """GJR-GARCH(1,1)-t fitted per ticker, with a Gaussian copula across tickers.

    ``variance_cap_multiple`` caps every *simulated* conditional variance at that multiple
    of the highest conditional variance the fit filtered in-sample (MODEL_NOTES section
    3). Found 2026-09-27: fits with ``nu`` below 4 (infinite innovation kurtosis) or
    persistence at 1 occasionally simulated explosive paths -- a 7,800x single day in one
    reproduction -- which barely moved quantiles but dominated the backtest's CRPS. The
    default 2.0 (about 1.41x the worst in-sample volatility) was fixed before looking at
    any backtest result, not tuned on one. ``None`` or ``0`` disables the cap. The
    closed-form :meth:`annualized_variance` (the Ito correction's input) ignores the cap.
    Measured on the real universe over 252 days: it touches a median 1-5% of paths (up
    to 23% for the most extreme fits) but only ~0.02% of simulated days (at most 0.36%),
    so the resulting drift error is negligible.
    """

    name = "garch_t"

    def __init__(
        self,
        p: int = 1,
        o: int = 1,
        q: int = 1,
        rescale_factor: float = 100.0,
        force_independent: bool = False,
        variance_cap_multiple: float | None = 2.0,
        dtype: str = "float64",
    ) -> None:
        super().__init__(dtype=dtype)
        self.variance_cap_multiple = variance_cap_multiple or None
        self._variance_cap: dict[str, float] = {}  # unscaled daily variance ceiling
        self.p = p
        self.o = o
        self.q = q
        self.rescale_factor = rescale_factor
        self.force_independent = force_independent
        self.params: dict[str, dict[str, float]] = {}
        self._h0: dict[str, float] = {}  # one-step-ahead conditional variance, unscaled
        self._resid_correlation: np.ndarray | None = None

    # -- fitting --------------------------------------------------------------------

    def _fit_shape(self, log_returns: pd.DataFrame, context: FitContext) -> None:
        standardized: dict[str, pd.Series] = {}

        for ticker in self.tickers:
            result, scaled = _fit_arch_gjr_t(
                log_returns[ticker], self.p, self.o, self.q, self.rescale_factor, ticker
            )
            params, next_variance, std_resid, state = _unpack_fit(
                result, scaled, self.rescale_factor, ticker
            )
            self.params[ticker] = params
            self._h0[ticker] = next_variance
            standardized[ticker] = std_resid
            if self.variance_cap_multiple is not None:
                self._variance_cap[ticker] = (
                    self.variance_cap_multiple
                    * state["max_variance_scaled"]
                    / self.rescale_factor**2
                )
            self._after_ticker_fit(ticker, result, params, state)

        if len(self.tickers) == 1 or self.force_independent:
            self._resid_correlation = np.eye(len(self.tickers))
        else:
            frame = pd.DataFrame(standardized).dropna(how="any")
            correlation = frame.loc[:, self.tickers].corr().fillna(0.0).to_numpy(dtype=float)
            np.fill_diagonal(correlation, 1.0)
            self._resid_correlation = correlation

    def _after_ticker_fit(self, ticker: str, result, params: dict, state: dict) -> None:  # noqa: ANN001
        """Hook for subclasses that need more of a ticker's arch result than the
        parameters (``GJRGarchTParamUncertaintyModel``: the covariance)."""

    def _cap_variance(self, ticker: str, variance: np.ndarray) -> None:
        """In place: floor at 1e-14, and cap at ``_variance_cap`` when one is set."""
        np.maximum(variance, 1e-14, out=variance)
        cap = self._variance_cap.get(ticker)
        if cap is not None:
            np.minimum(variance, cap, out=variance)

    # -- drift input ----------------------------------------------------------------

    def annualized_variance(self, horizon: int) -> pd.Series:
        """Average conditional variance across the horizon, annualized.

        With symmetric innovations the expected indicator ``1[e < 0]`` is one half, so
        the effective persistence is ``alpha + gamma / 2 + beta`` and

        ``E[h_{t+k}] = h_inf + persistence^(k-1) * (h_1 - h_inf)``.

        Averaging that geometric series over ``k = 1..H`` gives the mean variance the
        Ito correction needs. When persistence is at or above 1 the long-run variance does
        not exist, and the one-step variance is used for every step instead.
        """
        values: dict[str, float] = {}
        tdpy = self.context.trading_days_per_year if self.context else 252

        for ticker in self.tickers:
            par = self.params[ticker]
            h1 = self._h0[ticker]
            persistence = par["persistence"]
            if persistence >= 1.0 - 1e-8:
                LOGGER.warning(
                    "%s: GARCH persistence %.4f is at or above 1; variance does not "
                    "mean-revert, using the one-step forecast for the whole horizon",
                    ticker,
                    persistence,
                )
                average = h1
            else:
                long_run = par["omega"] / (1.0 - persistence)
                geometric = (
                    (1.0 - persistence**horizon) / (horizon * (1.0 - persistence))
                    if horizon > 0
                    else 1.0
                )
                average = long_run + (h1 - long_run) * geometric
            values[ticker] = float(max(average, 1e-12) * tdpy)

        return pd.Series(values, dtype=float).reindex(self.tickers)

    # -- simulation -----------------------------------------------------------------

    def _draw_innovations(self, n_paths: int, horizon: int, rng: np.random.Generator) -> np.ndarray:
        """Unit-variance innovations, shape ``(n_paths, horizon, n_tickers)``.

        One ticker: Student-t draws directly. Several: a Gaussian copula -- correlate
        normals with the standardized-residual correlation, map them through the normal
        CDF, then through the t quantile function. Applying a Cholesky factor straight to
        t draws would break the marginal distribution; the copula keeps each ticker's
        tails exactly Student-t while still coupling their moves.
        """
        n_tickers = len(self.tickers)
        if n_tickers == 1:
            nu = self.params[self.tickers[0]]["nu"]
            return standardized_t(nu, (n_paths, horizon, 1), rng)

        assert self._resid_correlation is not None
        cholesky = nearest_psd_cholesky(self._resid_correlation)
        normals = rng.standard_normal(size=(n_paths, horizon, n_tickers)) @ cholesky.T
        uniforms = stats.norm.cdf(normals)
        np.clip(uniforms, 1e-12, 1 - 1e-12, out=uniforms)

        out = np.empty_like(uniforms)
        for index, ticker in enumerate(self.tickers):
            nu = max(self.params[ticker]["nu"], MIN_NU)
            out[:, :, index] = stats.t.ppf(uniforms[:, :, index], df=nu) / np.sqrt(nu / (nu - 2.0))
        return out

    def _simulate_zero_mean(
        self, n_paths: int, horizon: int, rng: np.random.Generator
    ) -> np.ndarray:
        innovations = self._draw_innovations(n_paths, horizon, rng)
        out = np.empty((n_paths, horizon, len(self.tickers)), dtype=float)

        for index, ticker in enumerate(self.tickers):
            par = self.params[ticker]
            omega, alpha, gamma, beta = par["omega"], par["alpha"], par["gamma"], par["beta"]
            variance = np.full(n_paths, self._h0[ticker], dtype=float)

            # The GARCH recursion is inherently sequential in time; paths stay vectorized.
            for step in range(horizon):
                volatility = np.sqrt(variance)
                shock = volatility * innovations[:, step, index]
                out[:, step, index] = shock
                variance = omega + (alpha + gamma * (shock < 0.0)) * shock**2 + beta * variance
                self._cap_variance(ticker, variance)

        return out

    def describe(self) -> dict:
        info = super().describe()
        info.update(
            {
                "params": {t: dict(p) for t, p in self.params.items()},
                "variance_cap_multiple": self.variance_cap_multiple,
                "start_conditional_vol_annual": {
                    t: float(
                        np.sqrt(
                            self._h0[t]
                            * (self.context.trading_days_per_year if self.context else 252)
                        )
                    )
                    for t in self.tickers
                },
            }
        )
        return info


_PARAM_ORDER = ("omega", "alpha[1]", "gamma[1]", "beta[1]", "nu")


class GJRGarchTParamUncertaintyModel(GJRGarchTModel):
    """GJR-GARCH(1,1)-t with parameter uncertainty propagated into the paths
    (MODEL_NOTES section 18). Everything about the fit is :class:`GJRGarchTModel`'s; the
    difference is simulation: each path draws its own ``(omega, alpha, gamma, beta, nu)``
    from the maximum-likelihood estimate's asymptotic normal approximation (``arch``'s
    ``param_cov``), then runs the ordinary recursion under that set.

    - **Invalid draws are redrawn**, up to ``max_redraws`` rounds (negative omega/alpha/
      beta, ``alpha + gamma < 0``, persistence >= 1, ``nu <= MIN_NU``); anything still
      invalid falls back to the point estimate. The share of paths that got a genuine
      draw is reported in :meth:`describe` -- the normal approximation is poor near the
      stationarity boundary, and this makes its failures visible. A ticker whose point
      estimate is itself non-stationary gets no uncertainty (the parent model's
      fallback applies unchanged).
    - **The next-step variance is re-derived per draw** from the point fit's last
      residual and filtered variance. Re-filtering the whole training series under each
      draw would be exact but costs ``n_paths x T`` per ticker; the approximation keeps
      the filter state and changes only the one-step update.
    - **The drift contract holds exactly per path.** The base class adds
      ``mu_A - V/2`` with ``V`` the *point* parameters' average variance; each path
      here adds ``(V - V_i) / 2`` per day so its own drift is ``mu_A - V_i/2`` -- the
      arithmetic drift is exact for every parameter set, not just on average.
    - Draws are independent across tickers (the cross-ticker covariance of parameter
      estimates is not estimated). ``covariance_scale`` multiplies the covariance: ``0``
      reproduces ``garch_t`` exactly (a wiring check), ``1`` is the asymptotic estimate.
    """

    name = "garch_t_pu"

    def __init__(
        self,
        covariance_scale: float = 1.0,
        max_redraws: int = 10,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        if (self.p, self.o, self.q) != (1, 1, 1):
            raise ValueError("garch_t_pu supports GJR-GARCH(1,1) only (p = o = q = 1)")
        self.covariance_scale = float(covariance_scale)
        self.max_redraws = int(max_redraws)
        self._theta: dict[str, np.ndarray] = {}  # scaled-unit point estimate, _PARAM_ORDER
        self._cov_cholesky: dict[str, np.ndarray | None] = {}
        self._state: dict[str, dict[str, float]] = {}
        self.last_valid_fraction: dict[str, float] = {}

    def _after_ticker_fit(self, ticker: str, result, params: dict, state: dict) -> None:  # noqa: ANN001
        self._state[ticker] = state
        self._theta[ticker] = np.array([float(result.params[k]) for k in _PARAM_ORDER])
        cov = result.param_cov.loc[list(_PARAM_ORDER), list(_PARAM_ORDER)].to_numpy()
        usable = (
            self.covariance_scale > 0
            and params["persistence"] < 1.0 - 1e-8
            and np.isfinite(cov).all()
        )
        self._cov_cholesky[ticker] = (
            nearest_psd_cholesky(cov * self.covariance_scale) if usable else None
        )

    # -- parameter draws ------------------------------------------------------------

    @staticmethod
    def _valid(draws: np.ndarray) -> np.ndarray:
        omega, alpha, gamma, beta, nu = draws.T
        return (
            (omega > 0)
            & (alpha >= 0)
            & (alpha + gamma >= 0)
            & (beta >= 0)
            & (alpha + gamma / 2.0 + beta < 1.0 - 1e-6)
            & (nu > MIN_NU)
        )

    def _draw_parameter_sets(
        self, ticker: str, n_paths: int, rng: np.random.Generator
    ) -> dict[str, np.ndarray]:
        """Per-path parameters in unscaled daily units, each shape ``(n_paths,)``, plus
        ``h1`` (next-step variance) and ``valid`` (bool: a genuine draw, not fallback)."""
        theta = self._theta[ticker]
        chol = self._cov_cholesky[ticker]
        draws = np.broadcast_to(theta, (n_paths, theta.size)).copy()
        valid = np.zeros(n_paths, dtype=bool)
        if chol is not None:
            pending = np.arange(n_paths)
            for _ in range(self.max_redraws + 1):
                if pending.size == 0:
                    break
                candidate = theta + rng.standard_normal((pending.size, theta.size)) @ chol.T
                ok = self._valid(candidate)
                draws[pending[ok]] = candidate[ok]
                valid[pending[ok]] = True
                pending = pending[~ok]
            # Anything still pending keeps the point estimate already in `draws`.

        scale_sq = self.rescale_factor**2
        omega_s, alpha, gamma, beta, nu = draws.T
        state = self._state[ticker]
        e_last = state["last_resid_scaled"]
        h_last = state["last_variance_scaled"]
        h1_scaled = omega_s + (alpha + gamma * (e_last < 0)) * e_last**2 + beta * h_last
        return {
            "omega": omega_s / scale_sq,
            "alpha": alpha,
            "gamma": gamma,
            "beta": beta,
            "nu": nu,
            "h1": np.maximum(h1_scaled / scale_sq, 1e-14),
            "valid": valid,
        }

    @staticmethod
    def _average_variance(sets: dict[str, np.ndarray], horizon: int) -> np.ndarray:
        """Per-path mean conditional variance over ``horizon`` (daily units), the same
        closed form :meth:`GJRGarchTModel.annualized_variance` uses, vectorized."""
        persistence = sets["alpha"] + sets["gamma"] / 2.0 + sets["beta"]
        long_run = sets["omega"] / (1.0 - persistence)
        geometric = (1.0 - persistence**horizon) / (horizon * (1.0 - persistence))
        return np.maximum(long_run + (sets["h1"] - long_run) * geometric, 1e-14)

    # -- simulation -----------------------------------------------------------------

    def _simulate_zero_mean(
        self, n_paths: int, horizon: int, rng: np.random.Generator
    ) -> np.ndarray:
        if all(self._cov_cholesky[t] is None for t in self.tickers):
            return super()._simulate_zero_mean(n_paths, horizon, rng)

        sets = {t: self._draw_parameter_sets(t, n_paths, rng) for t in self.tickers}
        self.last_valid_fraction = {t: float(sets[t]["valid"].mean()) for t in self.tickers}
        innovations = self._draw_innovations_per_path(sets, n_paths, horizon, rng)
        tdpy = self.context.trading_days_per_year if self.context else 252
        point_variance = self.annualized_variance(horizon) / tdpy

        out = np.empty((n_paths, horizon, len(self.tickers)), dtype=float)
        for index, ticker in enumerate(self.tickers):
            par = sets[ticker]
            variance = par["h1"].copy()
            # The GARCH recursion is inherently sequential in time; paths stay vectorized.
            for step in range(horizon):
                shock = np.sqrt(variance) * innovations[:, step, index]
                out[:, step, index] = shock
                variance = (
                    par["omega"]
                    + (par["alpha"] + par["gamma"] * (shock < 0.0)) * shock**2
                    + par["beta"] * variance
                )
                self._cap_variance(ticker, variance)
            if self._cov_cholesky[ticker] is not None:
                ito = 0.5 * (point_variance[ticker] - self._average_variance(par, horizon))
                out[:, :, index] += ito[:, None]
        return out

    def _draw_innovations_per_path(
        self,
        sets: dict[str, dict[str, np.ndarray]],
        n_paths: int,
        horizon: int,
        rng: np.random.Generator,
    ) -> np.ndarray:
        """As :meth:`GJRGarchTModel._draw_innovations`, with each path's own ``nu``."""
        n_tickers = len(self.tickers)
        if n_tickers == 1:
            nu = np.maximum(sets[self.tickers[0]]["nu"], MIN_NU)[:, None]
            raw = rng.standard_t(df=nu, size=(n_paths, horizon))
            return (raw / np.sqrt(nu / (nu - 2.0)))[:, :, None]

        assert self._resid_correlation is not None
        cholesky = nearest_psd_cholesky(self._resid_correlation)
        normals = rng.standard_normal(size=(n_paths, horizon, n_tickers)) @ cholesky.T
        uniforms = stats.norm.cdf(normals)
        np.clip(uniforms, 1e-12, 1 - 1e-12, out=uniforms)
        out = np.empty_like(uniforms)
        for index, ticker in enumerate(self.tickers):
            nu = np.maximum(sets[ticker]["nu"], MIN_NU)[:, None]
            out[:, :, index] = stats.t.ppf(uniforms[:, :, index], df=nu) / np.sqrt(nu / (nu - 2.0))
        return out

    def describe(self) -> dict:
        info = super().describe()
        info.update(
            {
                "covariance_scale": self.covariance_scale,
                "uncertainty_applied": {t: self._cov_cholesky[t] is not None for t in self.tickers},
                "last_valid_fraction": dict(self.last_valid_fraction),
            }
        )
        return info

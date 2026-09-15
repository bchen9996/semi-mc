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
    return params, next_variance_scaled / scale_sq, standardized


class GJRGarchTModel(BaseReturnModel):
    """GJR-GARCH(1,1)-t fitted per ticker, with a Gaussian copula across tickers."""

    name = "garch_t"

    def __init__(
        self,
        p: int = 1,
        o: int = 1,
        q: int = 1,
        rescale_factor: float = 100.0,
        force_independent: bool = False,
        dtype: str = "float64",
    ) -> None:
        super().__init__(dtype=dtype)
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
            params, next_variance, std_resid = fit_gjr_garch_t(
                log_returns[ticker],
                p=self.p,
                o=self.o,
                q=self.q,
                rescale_factor=self.rescale_factor,
                label=ticker,
            )
            self.params[ticker] = params
            self._h0[ticker] = next_variance
            standardized[ticker] = std_resid

        if len(self.tickers) == 1 or self.force_independent:
            self._resid_correlation = np.eye(len(self.tickers))
        else:
            frame = pd.DataFrame(standardized).dropna(how="any")
            correlation = frame.loc[:, self.tickers].corr().fillna(0.0).to_numpy(dtype=float)
            np.fill_diagonal(correlation, 1.0)
            self._resid_correlation = correlation

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
                np.maximum(variance, 1e-14, out=variance)

        return out

    def describe(self) -> dict:
        info = super().describe()
        info.update(
            {
                "params": {t: dict(p) for t, p in self.params.items()},
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

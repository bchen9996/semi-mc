"""Factor model with regime-switching sector volatility (addendum Phase 5: "shared regimes").

The addendum, in full: "put regime switching on the sector factor, so all stocks share one
regime path and turn turbulent together in portfolio simulations. If each stock switched
regimes independently, the simulation would miss selloffs that hit the whole sector at
once."

Applying :class:`~semimc.models.regime.RegimeSwitchingModel` directly to a multi-ticker
panel does not do this -- its ``_simulate_zero_mean`` draws each ticker's regime chain with
its own independent ``rng.choice``/``rng.random`` call, so which names are turbulent on a
given simulated day is uncorrelated across tickers except through the (regime-conditional)
Gaussian correlation. A true sector-wide selloff, where every name enters the turbulent
regime on the same simulated day, would only ever appear by chance.

The fix reuses :class:`~semimc.models.factor.FactorModel`'s own architecture instead of
changing ``RegimeSwitchingModel``: every ticker's simulated shock already includes the
*same* simulated sector-factor draw, via ``loadings @ factor_shocks`` -- that sharing is
what a factor decomposition is. Fitting the sector factor itself with regime-switching
volatility (one path, shared) therefore makes every ticker turn turbulent together for
free; nothing about how tickers are combined has to change.

Decomposition, per ticker, identical to ``FactorModel``:

``r_i = b_m * f_market + b_s * f_sector + e_i``

The market factor and idiosyncratic residuals keep GJR-GARCH-t, as in ``FactorModel``; only
the sector factor's volatility source changes. Splitting the market and sector factors into
two independently-fit models (rather than one joint 2-column GARCH fit, as ``FactorModel``
uses) drops whatever small residual correlation GARCH would have found between them --
expected to be minor, since the sector factor is already SOXX residualized on SPY by
construction, but a real, disclosed simplification rather than a hidden one.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..data.returns import residualize
from .base import BaseReturnModel, FitContext
from .garch_t import GJRGarchTModel
from .regime import RegimeSwitchingModel

LOGGER = logging.getLogger(__name__)

MARKET_FACTOR = "_market"
SECTOR_FACTOR = "_sector"


class FactorRegimeModel(BaseReturnModel):
    """Market factor (GARCH) + sector factor (regime-switching) + idiosyncratic (GARCH).

    Unlike ``FactorModel``, a sector benchmark is required, not optional -- without one
    there is no sector factor to put regime switching on, and this model exists
    specifically for that; use ``"factor"`` when there is no sector benchmark.
    """

    name = "factor_regime"

    def __init__(
        self,
        search_reps: int = 20,
        switching_mean: bool = False,
        min_expected_duration_days: float = 15.0,
        dtype: str = "float64",
    ) -> None:
        super().__init__(dtype=dtype)
        self.search_reps = search_reps
        self.switching_mean = switching_mean
        self.min_expected_duration_days = min_expected_duration_days
        self.loadings: pd.DataFrame | None = None
        self.market_model: GJRGarchTModel | None = None
        self.sector_model: RegimeSwitchingModel | None = None
        self.idio_model: GJRGarchTModel | None = None
        self._factor_names: list[str] = [MARKET_FACTOR, SECTOR_FACTOR]

    def _fit_shape(self, log_returns: pd.DataFrame, context: FitContext) -> None:
        market = context.market
        sector = context.sector
        if market is None:
            raise ValueError(
                "the factor_regime model needs market benchmark returns in the context"
            )
        if sector is None:
            raise ValueError(
                "the factor_regime model needs a sector benchmark in the context -- without "
                "one there is no sector factor to put regime switching on; use 'factor' instead"
            )

        sector_residual = residualize(sector, market)
        factors = pd.DataFrame({MARKET_FACTOR: market, SECTOR_FACTOR: sector_residual})
        factors = factors.dropna(how="any")

        panel = log_returns.loc[:, self.tickers]
        aligned = panel.join(factors, how="inner").dropna(how="any")
        if len(aligned) < 250:
            raise ValueError(
                f"factor_regime model: only {len(aligned)} overlapping observations "
                "with the factors"
            )

        design = aligned.loc[:, self._factor_names].to_numpy(dtype=float)
        design = np.column_stack([np.ones(len(design)), design])
        targets = aligned.loc[:, self.tickers].to_numpy(dtype=float)

        coefficients, *_ = np.linalg.lstsq(design, targets, rcond=None)
        self.loadings = pd.DataFrame(
            coefficients[1:].T, index=self.tickers, columns=self._factor_names
        )
        residuals = pd.DataFrame(
            targets - design @ coefficients, index=aligned.index, columns=self.tickers
        )

        factor_context = context  # factors are already point-in-time
        self.market_model = GJRGarchTModel(force_independent=True).fit(
            aligned[[MARKET_FACTOR]], factor_context
        )
        self.sector_model = RegimeSwitchingModel(
            search_reps=self.search_reps,
            switching_mean=self.switching_mean,
            min_expected_duration_days=self.min_expected_duration_days,
            force_independent=True,
        ).fit(aligned[[SECTOR_FACTOR]], factor_context)
        self.idio_model = GJRGarchTModel(force_independent=True).fit(residuals, factor_context)

    def annualized_variance(self, horizon: int) -> pd.Series:
        """``b' Sigma_f b + var(e)``, the sector block from the regime forecast."""
        assert self.loadings is not None
        assert self.market_model is not None
        assert self.sector_model is not None
        assert self.idio_model is not None

        market_variance = self.market_model.annualized_variance(horizon)
        sector_variance = self.sector_model.annualized_variance(horizon)
        factor_variance = pd.concat([market_variance, sector_variance]).reindex(self._factor_names)
        idio_variance = self.idio_model.annualized_variance(horizon).reindex(self.tickers)

        loadings = self.loadings.loc[self.tickers, self._factor_names].to_numpy(dtype=float)
        systematic = (loadings**2) @ factor_variance.to_numpy(dtype=float)
        return pd.Series(systematic + idio_variance.to_numpy(dtype=float), index=self.tickers)

    def _simulate_zero_mean(
        self, n_paths: int, horizon: int, rng: np.random.Generator
    ) -> np.ndarray:
        """Every ticker's shock is built from the *same* simulated market- and sector-
        factor paths -- the mechanism that makes a shared sector regime switch show up in
        every ticker's return on the same simulated day."""
        assert self.loadings is not None
        assert self.market_model is not None
        assert self.sector_model is not None
        assert self.idio_model is not None

        market_shocks = self.market_model._simulate_zero_mean(n_paths, horizon, rng)
        sector_shocks = self.sector_model._simulate_zero_mean(n_paths, horizon, rng)
        idio_shocks = self.idio_model._simulate_zero_mean(n_paths, horizon, rng)

        factor_shocks = np.concatenate([market_shocks, sector_shocks], axis=-1)
        loadings = self.loadings.loc[self.tickers, self._factor_names].to_numpy(dtype=float)
        return factor_shocks @ loadings.T + idio_shocks

    def describe(self) -> dict:
        info = super().describe()
        assert self.loadings is not None
        assert self.sector_model is not None
        sector_regime = self.sector_model.describe().get("regimes", {}).get(SECTOR_FACTOR, {})
        info.update(
            {
                "factors": list(self._factor_names),
                "loadings": self.loadings.round(4).to_dict(orient="index"),
                "sector_regime": sector_regime,
            }
        )
        return info

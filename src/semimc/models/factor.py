"""Market + sector factor model.

Semiconductor names do not move independently, and a model that treats them as if they do
will understate portfolio risk badly -- diversification across fifteen semis is far less
than diversification across fifteen unrelated stocks.

Decomposition, per ticker:

``r_i = b_m * f_market + b_s * f_sector + e_i``

where ``f_market`` is SPY's log return and ``f_sector`` is SOXX **residualized on SPY**, so
the two factors are close to orthogonal and ``b_s`` reads as sector exposure over and above
market exposure rather than a mixture of the two.

Each piece gets its own dynamics: the factors are simulated with GJR-GARCH-t (so a
volatile market stays volatile, and its tails are fat), and the idiosyncratic residuals
likewise but forced independent across tickers -- that independence *is* the factor model's
assumption, and Phase 5's filtered historical simulation is the alternative that drops it.

Correlations therefore rise automatically when factor volatility rises: with idiosyncratic
variance roughly fixed, a market shock raises the shared component's share of total
variance, which is the "correlations go to one in a selloff" effect appearing as a
consequence rather than as a hand-set parameter.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..data.returns import residualize
from .base import BaseReturnModel, FitContext
from .garch_t import GJRGarchTModel

LOGGER = logging.getLogger(__name__)

MARKET_FACTOR = "_market"
SECTOR_FACTOR = "_sector"


class FactorModel(BaseReturnModel):
    """Two-factor model with GARCH dynamics on the factors and on the residuals."""

    name = "factor"

    def __init__(self, dtype: str = "float64") -> None:
        super().__init__(dtype=dtype)
        self.loadings: pd.DataFrame | None = None  # index: ticker, columns: market, sector
        self.factor_model: GJRGarchTModel | None = None
        self.idio_model: GJRGarchTModel | None = None
        self._factor_names: list[str] = []

    def _fit_shape(self, log_returns: pd.DataFrame, context: FitContext) -> None:
        market = context.market
        sector = context.sector
        if market is None:
            raise ValueError("the factor model needs market benchmark returns in the context")

        sector_residual = (
            residualize(sector, market) if sector is not None else pd.Series(dtype=float)
        )
        if sector_residual.empty:
            LOGGER.warning(
                "no sector benchmark available; the factor model degenerates to a "
                "single-factor market model"
            )

        factors = pd.DataFrame({MARKET_FACTOR: market})
        if not sector_residual.empty:
            factors[SECTOR_FACTOR] = sector_residual
        factors = factors.dropna(how="any")
        self._factor_names = list(factors.columns)

        panel = log_returns.loc[:, self.tickers]
        aligned = panel.join(factors, how="inner").dropna(how="any")
        if len(aligned) < 250:
            raise ValueError(
                f"factor model: only {len(aligned)} overlapping observations with the factors"
            )

        design = aligned.loc[:, self._factor_names].to_numpy(dtype=float)
        design = np.column_stack([np.ones(len(design)), design])
        targets = aligned.loc[:, self.tickers].to_numpy(dtype=float)

        # One least-squares solve for every ticker at once.
        coefficients, *_ = np.linalg.lstsq(design, targets, rcond=None)
        self.loadings = pd.DataFrame(
            coefficients[1:].T, index=self.tickers, columns=self._factor_names
        )
        residuals = pd.DataFrame(
            targets - design @ coefficients, index=aligned.index, columns=self.tickers
        )

        factor_context = context  # factors are already point-in-time
        self.factor_model = GJRGarchTModel(force_independent=False).fit(
            aligned.loc[:, self._factor_names], factor_context
        )
        self.idio_model = GJRGarchTModel(force_independent=True).fit(residuals, factor_context)

    def annualized_variance(self, horizon: int) -> pd.Series:
        """``b' Sigma_f b + var(e)``, with each piece from its own GARCH forecast."""
        assert self.loadings is not None
        assert self.factor_model is not None and self.idio_model is not None

        factor_variance = self.factor_model.annualized_variance(horizon).reindex(self._factor_names)
        idio_variance = self.idio_model.annualized_variance(horizon).reindex(self.tickers)

        loadings = self.loadings.loc[self.tickers, self._factor_names].to_numpy(dtype=float)
        systematic = (loadings**2) @ factor_variance.to_numpy(dtype=float)
        return pd.Series(systematic + idio_variance.to_numpy(dtype=float), index=self.tickers)

    def _simulate_zero_mean(
        self, n_paths: int, horizon: int, rng: np.random.Generator
    ) -> np.ndarray:
        assert self.loadings is not None
        assert self.factor_model is not None and self.idio_model is not None

        factor_shocks = self.factor_model._simulate_zero_mean(n_paths, horizon, rng)
        idio_shocks = self.idio_model._simulate_zero_mean(n_paths, horizon, rng)
        loadings = self.loadings.loc[self.tickers, self._factor_names].to_numpy(dtype=float)
        return factor_shocks @ loadings.T + idio_shocks

    def describe(self) -> dict:
        info = super().describe()
        assert self.loadings is not None
        info.update(
            {
                "factors": list(self._factor_names),
                "loadings": self.loadings.round(4).to_dict(orient="index"),
            }
        )
        return info

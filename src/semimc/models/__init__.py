"""Model registry.

Models are addressed by name everywhere -- the CLI, the config, the backtest scorecard --
so that adding one means adding a builder here and nothing else. Every model satisfies the
same :class:`~semimc.models.base.ReturnModel` interface, which is what lets the backtester
score them against each other without knowing what any of them do.
"""

from __future__ import annotations

from collections.abc import Callable

from .base import BaseReturnModel, DriftTilt, FitContext, NotFittedError, ReturnModel
from .blend import BlendModel, optimize_crps_weights
from .bootstrap import BlockBootstrapModel
from .earnings_jumps import EarningsCalendarMissing, EarningsJumpModel
from .factor import FactorModel
from .factor_regime import FactorRegimeModel
from .fhs import FilteredHistoricalSimulation
from .garch_t import GarchFitError, GJRGarchTModel, GJRGarchTParamUncertaintyModel
from .gbm import GBMModel
from .har import HARFitError, HARVolatilityModel
from .merton import MertonFitError, MertonJumpDiffusion
from .regime import RegimeFitError, RegimeSwitchingModel

__all__ = [
    "MODEL_NAMES",
    "BaseReturnModel",
    "BlendModel",
    "BlockBootstrapModel",
    "DriftTilt",
    "EarningsCalendarMissing",
    "EarningsJumpModel",
    "FactorModel",
    "FactorRegimeModel",
    "FilteredHistoricalSimulation",
    "FitContext",
    "GBMModel",
    "GJRGarchTModel",
    "GJRGarchTParamUncertaintyModel",
    "GarchFitError",
    "HARFitError",
    "HARVolatilityModel",
    "MertonFitError",
    "MertonJumpDiffusion",
    "NotFittedError",
    "RegimeFitError",
    "RegimeSwitchingModel",
    "ReturnModel",
    "build_model",
    "optimize_crps_weights",
]

MODEL_NAMES = [
    "gbm",
    "bootstrap",
    "garch_t",
    "garch_t_pu",
    "earnings_jumps",
    "factor",
    "factor_regime",
    "fhs",
    "merton",
    "har",
    "regime",
    "blend",
]

# Models whose builder needs more than the config -- currently only HAR, which needs the
# OHLC frames a MarketData instance carries. Kept as an explicit set so build_model can
# give a precise error rather than an AttributeError deep inside fitting.
_NEEDS_MARKET = {"har"}


def build_model(
    name: str,
    config,
    dtype: str = "float64",
    market=None,  # noqa: ANN001
) -> BaseReturnModel:
    """Instantiate a model by name using the settings in ``config``.

    Parameters
    ----------
    name
        One of :data:`MODEL_NAMES`.
    config
        A validated :class:`~semimc.config.Config`.
    dtype
        Storage dtype for simulated arrays.
    market
        Required for ``"har"`` (needs adjusted OHLC) and for a ``"blend"`` or
        ``"earnings_jumps"`` whose components include it. Every other model ignores it.

    Returns
    -------
    BaseReturnModel
        An unfitted model.
    """
    if name in _NEEDS_MARKET and market is None:
        raise ValueError(f"model {name!r} needs market data; call build_model(..., market=market)")

    builders: dict[str, Callable[[], BaseReturnModel]] = {
        "gbm": lambda: GBMModel(
            vol_estimator=config.fit.vol_estimator,
            vol_window_days=config.fit.vol_window_days,
            ewma_lambda=config.fit.ewma_lambda,
            dtype=dtype,
        ),
        "bootstrap": lambda: BlockBootstrapModel(
            block_length=config.models.bootstrap.block_length, dtype=dtype
        ),
        "garch_t": lambda: GJRGarchTModel(
            p=config.models.garch_t.p,
            o=config.models.garch_t.o,
            q=config.models.garch_t.q,
            rescale_factor=config.models.garch_t.rescale_factor,
            variance_cap_multiple=config.models.garch_t.variance_cap_multiple,
            dtype=dtype,
        ),
        "garch_t_pu": lambda: GJRGarchTParamUncertaintyModel(
            covariance_scale=config.models.garch_t_pu.covariance_scale,
            max_redraws=config.models.garch_t_pu.max_redraws,
            p=config.models.garch_t.p,
            o=config.models.garch_t.o,
            q=config.models.garch_t.q,
            rescale_factor=config.models.garch_t.rescale_factor,
            variance_cap_multiple=config.models.garch_t.variance_cap_multiple,
            dtype=dtype,
        ),
        "factor": lambda: FactorModel(dtype=dtype),
        "factor_regime": lambda: FactorRegimeModel(
            search_reps=config.models.regime.search_reps,
            switching_mean=config.models.regime.switching_mean,
            min_expected_duration_days=config.models.regime.min_expected_duration_days,
            dtype=dtype,
        ),
        "fhs": lambda: FilteredHistoricalSimulation(
            p=config.models.garch_t.p,
            o=config.models.garch_t.o,
            q=config.models.garch_t.q,
            rescale_factor=config.models.garch_t.rescale_factor,
            block_length=config.models.fhs.block_length,
            dtype=dtype,
        ),
        "merton": lambda: MertonJumpDiffusion(
            max_jumps_per_day=config.models.merton.max_jumps_per_day,
            exclude_earnings_days=config.models.merton.exclude_earnings_days,
            dtype=dtype,
        ),
        "har": lambda: HARVolatilityModel(
            horizons=config.models.har.horizons,
            estimator=config.models.har.estimator,
            innovations=config.models.har.innovations,
            exclude_earnings_variance=config.models.har.exclude_earnings_variance,
            dtype=dtype,
        ).with_ohlc(market.ohlc if market is not None else {}),
        "regime": lambda: RegimeSwitchingModel(
            search_reps=config.models.regime.search_reps,
            switching_mean=config.models.regime.switching_mean,
            min_expected_duration_days=config.models.regime.min_expected_duration_days,
            dtype=dtype,
        ),
    }

    if name == "earnings_jumps":
        base_name = config.models.earnings_jumps.base_model
        if base_name == "earnings_jumps":
            raise ValueError("models.earnings_jumps.base_model cannot be earnings_jumps itself")
        return EarningsJumpModel(
            base_model=build_model(base_name, config, dtype=dtype, market=market),
            min_earnings_observations=config.models.earnings_jumps.min_earnings_observations,
            strip_from_training=config.models.earnings_jumps.strip_from_training,
            dtype=dtype,
        )

    if name == "blend":
        component_names = config.models.blend.components
        if len(component_names) < 2:
            raise ValueError("models.blend.components needs at least two model names")
        if "blend" in component_names:
            raise ValueError("models.blend.components cannot include 'blend' itself")
        components = {
            component_name: build_model(component_name, config, dtype=dtype, market=market)
            for component_name in component_names
        }
        weights = config.models.blend.weights or None
        return BlendModel(components=components, weights=weights, dtype=dtype)

    if name not in builders:
        raise ValueError(f"unknown model {name!r}; available: {', '.join(MODEL_NAMES)}")
    return builders[name]()

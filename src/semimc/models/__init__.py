"""Model registry.

Models are addressed by name everywhere -- the CLI, the config, the backtest scorecard --
so that adding one means adding a builder here and nothing else. Every model satisfies the
same :class:`~semimc.models.base.ReturnModel` interface, which is what lets the backtester
score them against each other without knowing what any of them do.
"""

from __future__ import annotations

from collections.abc import Callable

from .base import BaseReturnModel, FitContext, NotFittedError, ReturnModel
from .bootstrap import BlockBootstrapModel
from .earnings_jumps import EarningsCalendarMissing, EarningsJumpModel
from .factor import FactorModel
from .fhs import FilteredHistoricalSimulation
from .garch_t import GarchFitError, GJRGarchTModel
from .gbm import GBMModel

__all__ = [
    "MODEL_NAMES",
    "BaseReturnModel",
    "BlockBootstrapModel",
    "EarningsCalendarMissing",
    "EarningsJumpModel",
    "FactorModel",
    "FilteredHistoricalSimulation",
    "FitContext",
    "GBMModel",
    "GJRGarchTModel",
    "GarchFitError",
    "NotFittedError",
    "ReturnModel",
    "build_model",
]

MODEL_NAMES = ["gbm", "bootstrap", "garch_t", "earnings_jumps", "factor", "fhs"]


def build_model(name: str, config, dtype: str = "float64") -> BaseReturnModel:  # noqa: ANN001
    """Instantiate a model by name using the settings in ``config``.

    Parameters
    ----------
    name
        One of :data:`MODEL_NAMES`.
    config
        A validated :class:`~semimc.config.Config`.
    dtype
        Storage dtype for simulated arrays.

    Returns
    -------
    BaseReturnModel
        An unfitted model.
    """
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
            dtype=dtype,
        ),
        "factor": lambda: FactorModel(dtype=dtype),
        "fhs": lambda: FilteredHistoricalSimulation(
            p=config.models.garch_t.p,
            o=config.models.garch_t.o,
            q=config.models.garch_t.q,
            rescale_factor=config.models.garch_t.rescale_factor,
            block_length=config.models.fhs.block_length,
            dtype=dtype,
        ),
    }

    if name == "earnings_jumps":
        base_name = config.models.earnings_jumps.base_model
        if base_name == "earnings_jumps":
            raise ValueError("models.earnings_jumps.base_model cannot be earnings_jumps itself")
        return EarningsJumpModel(
            base_model=build_model(base_name, config, dtype=dtype),
            min_earnings_observations=config.models.earnings_jumps.min_earnings_observations,
            strip_from_training=config.models.earnings_jumps.strip_from_training,
            dtype=dtype,
        )

    if name not in builders:
        raise ValueError(f"unknown model {name!r}; available: {', '.join(MODEL_NAMES)}")
    return builders[name]()

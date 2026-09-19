"""Addendum Phase 4B: regressions, shrinkage, and the coefficient bootstrap that turn
point-in-time signals into a drift tilt.

``panel.py`` fits the pooled cross-sectional regression (relative tilt) and its
Fama-MacBeth robustness check; ``sector.py`` fits the single-series sector-tilt regression;
``cv.py`` splits panel data on dates, with a gap, for anything that needs
cross-validation; ``tilt.py`` turns fitted regressions into a
:class:`~semimc.context.DriftTilt`; ``evaluate.py`` scores all of it.
"""

from __future__ import annotations

from .combine import (
    CandidateSpec,
    QuantileRegressionModel,
    ShrunkLinearModel,
    SimpleAverageModel,
    cross_validated_pinball_losses,
    evaluate_candidate,
    fit_quantile_regressions,
    fit_ridge_or_lasso,
    fit_simple_average,
    predict_quantiles,
    select_alpha_by_cv,
    shrinkage_spec,
    simple_average_spec,
    single_or_combined_spec,
)
from .cv import (
    approx_trading_days_per_period,
    date_grouped_gapped_splits,
    min_gap_periods,
    rows_for_dates,
)
from .evaluate import (
    clark_west_statistic,
    oos_r_squared,
    pinball_losses,
    rank_information_coefficient,
    sign_flip_rate,
    subset_by_date_range,
)
from .panel import (
    PanelRegressionResult,
    build_regression_row,
    fama_macbeth,
    fit_driscoll_kraay,
    newey_west_mean,
    standardize_cross_sectionally,
    trailing_betas,
    training_rows,
)
from .sector import build_sector_regression_row, fit_sector_tilt, industry_yoy_growth, sector_factor
from .stability import (
    fama_macbeth_coefficient_path,
    market_environment_split,
    rolling_coefficient_stability,
)
from .tilt import (
    block_bootstrap_date_indices,
    bootstrap_panel_tilt_draws,
    bootstrap_sector_tilt_draws,
    build_drift_tilt,
    shrink_and_cap,
)

__all__ = [
    "CandidateSpec",
    "PanelRegressionResult",
    "QuantileRegressionModel",
    "ShrunkLinearModel",
    "SimpleAverageModel",
    "approx_trading_days_per_period",
    "block_bootstrap_date_indices",
    "bootstrap_panel_tilt_draws",
    "bootstrap_sector_tilt_draws",
    "build_drift_tilt",
    "build_regression_row",
    "build_sector_regression_row",
    "clark_west_statistic",
    "cross_validated_pinball_losses",
    "date_grouped_gapped_splits",
    "evaluate_candidate",
    "fama_macbeth",
    "fama_macbeth_coefficient_path",
    "fit_driscoll_kraay",
    "fit_quantile_regressions",
    "fit_ridge_or_lasso",
    "fit_sector_tilt",
    "fit_simple_average",
    "industry_yoy_growth",
    "market_environment_split",
    "min_gap_periods",
    "newey_west_mean",
    "oos_r_squared",
    "pinball_losses",
    "predict_quantiles",
    "rank_information_coefficient",
    "rolling_coefficient_stability",
    "rows_for_dates",
    "sector_factor",
    "select_alpha_by_cv",
    "shrink_and_cap",
    "shrinkage_spec",
    "sign_flip_rate",
    "simple_average_spec",
    "single_or_combined_spec",
    "standardize_cross_sectionally",
    "subset_by_date_range",
    "trailing_betas",
    "training_rows",
]

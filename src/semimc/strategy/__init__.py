"""Sleeve target weights (semis_paper_trading_brief.md).

The one module the backtest, the shadow book, and (a later phase) the live jobs all call
to turn "an as-of date and a universe" into target portfolio weights -- the brief's own
requirement. Every function here reuses the existing choke points
(:func:`semimc.engine.runner.fit_at_origin`, :func:`semimc.engine.runner.run_simulation`,
:class:`semimc.models.base.BaseReturnModel`) rather than re-deriving a forecast.

Scoped for now to what S0 (equal-weight control) and S3 (vol-targeted control) need. S1
(signal tilt) and S2 (long/short) have no code path here -- the addendum's Phase 4B
holdout came back null (``research_log.md``, 2026-09-16) and the brief's own rule is that
paper trading can't rescue a signal that failed it.
"""

from .decide import Decision, decide, decide_s0, decide_s3, decide_vol_twin_shadow
from .forecasts import portfolio_vol_forecast, trailing_portfolio_vol
from .weights import equal_weight, scale_to_target_vol

__all__ = [
    "Decision",
    "decide",
    "decide_s0",
    "decide_s3",
    "decide_vol_twin_shadow",
    "equal_weight",
    "portfolio_vol_forecast",
    "scale_to_target_vol",
    "trailing_portfolio_vol",
]

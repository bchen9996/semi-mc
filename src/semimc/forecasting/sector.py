"""The sector-tilt regression: industry-cycle signals predicting the sector factor.

The sector factor is SOXX residualized on SPY (:func:`semimc.data.returns.residualize`) --
the part of sector performance that is not just the market doing well. This module
regresses its **forward** return on the industry-cycle signal's year-over-year growth,
sampled monthly.

There is exactly one sector series, so this regression has far less statistical power than
the stock-level panel regression, which pools 15 names. The addendum says to expect little
predictability here, and this module's evaluation output is built to say so honestly rather
than dress up a handful of effective observations as a discovery.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from statsmodels.regression.linear_model import OLS
from statsmodels.tools.tools import add_constant

from ..data.returns import residualize
from ..signals.external import as_of as external_as_of
from .panel import PanelRegressionResult, forward_cumulative_return


def sector_factor(market_returns: pd.Series, sector_returns: pd.Series) -> pd.Series:
    """SOXX residualized on SPY -- the sector move net of the market's own move."""
    return residualize(sector_returns, market_returns)


def industry_yoy_growth(external_series: pd.DataFrame, as_of: pd.Timestamp) -> float:
    """Year-over-year growth of an external monthly series, as known at ``as_of``.

    Both the current and the year-ago value must have been *released* by ``as_of`` --
    using a not-yet-published year-ago revision would be exactly the leak the point-in-time
    store exists to prevent, even though a year-ago value usually settles quickly.
    """
    known = external_as_of(external_series, as_of)
    if known.empty:
        return float("nan")
    latest_period = known.index.max()
    year_ago_period = latest_period - pd.DateOffset(years=1)
    position = known.index.searchsorted(year_ago_period)
    if position >= len(known) or abs((known.index[position] - year_ago_period).days) > 45:
        return float("nan")
    current = known.iloc[-1]
    prior = known.iloc[position]
    if not np.isfinite(prior) or prior == 0:
        return float("nan")
    return float(current / prior - 1.0)


def build_sector_regression_row(
    sector_factor_returns: pd.Series,
    external_series: pd.DataFrame,
    as_of: pd.Timestamp,
    horizon_days: int,
) -> dict:
    """One origin's row for the sector-tilt regression: industry YoY growth predicting the
    forward sector-factor return."""
    growth = industry_yoy_growth(external_series, as_of)
    outcome = forward_cumulative_return(sector_factor_returns, as_of, horizon_days)
    target, end = (outcome[0], outcome[1]) if outcome is not None else (np.nan, pd.NaT)
    return {
        "date": pd.Timestamp(as_of).normalize(),
        "industry_yoy": growth,
        "target": target,
        "target_end_date": end,
    }


def fit_sector_tilt(rows: pd.DataFrame, maxlags: int = 3) -> PanelRegressionResult:
    """Time-series OLS of the forward sector factor on industry YoY growth, Newey-West SEs.

    A single time series has no cross-section to apply Driscoll-Kraay's group dimension
    to, so this uses plain HAC (Newey-West) instead -- the serial-correlation half of
    Driscoll-Kraay, which is the half that still applies to overlapping forward returns
    sampled from one series.
    """
    usable = rows.dropna(subset=["industry_yoy", "target"])
    if len(usable) < 12:
        raise ValueError(
            f"only {len(usable)} usable sector-tilt observations; need >= 12. Expected -- "
            "one sector series with monthly sampling gives few origins fast."
        )
    design = add_constant(usable[["industry_yoy"]], has_constant="add")
    fitted = OLS(usable["target"].to_numpy(), design.to_numpy()).fit(
        cov_type="HAC", cov_kwds={"maxlags": maxlags}
    )
    columns = list(design.columns)
    return PanelRegressionResult(
        coefficients=pd.Series(fitted.params, index=columns),
        std_errors=pd.Series(fitted.bse, index=columns),
        t_stats=pd.Series(fitted.tvalues, index=columns),
        r_squared=float(fitted.rsquared),
        n_observations=int(len(usable)),
        n_dates=int(len(usable)),
        feature_columns=["industry_yoy"],
    )

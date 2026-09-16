"""Stability diagnostics the addendum's evaluation section asks for: rolling coefficient
charts, sign flips, and a split by market environment (its own example: "the 2022 downturn
vs. the rally that followed").

Not a new estimator -- a report built from what a Fama-MacBeth fit already computes
internally. :func:`~semimc.forecasting.panel.fama_macbeth` fits one cross-sectional OLS per
date and averages the resulting coefficients over time; the per-date coefficients *are* the
rolling-coefficient series this module charts, so :func:`fama_macbeth_coefficient_path`
exposes exactly that intermediate rather than recomputing it differently.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .evaluate import sign_flip_rate, subset_by_date_range
from .panel import PanelRegressionResult, fit_driscoll_kraay

# The addendum's own example split: semiconductor names (SOXX) fell through 2022 and began
# recovering around mid-October; the boundary is a documented, adjustable default, not a
# fitted breakpoint.
DEFAULT_DOWNTURN_START = pd.Timestamp("2022-01-01")
DEFAULT_DOWNTURN_END = pd.Timestamp("2022-10-14")
DEFAULT_RALLY_START = pd.Timestamp("2022-10-15")


def fama_macbeth_coefficient_path(
    rows: pd.DataFrame,
    feature_columns: list[str],
    target_column: str = "target",
    date_column: str = "date",
    min_cross_section: int = 5,
) -> pd.DataFrame:
    """One cross-sectional OLS fit per date, coefficients only.

    This is the rolling-coefficient series the addendum asks to chart -- date-indexed, one
    column per feature plus ``const``. A date without ``min_cross_section`` observations is
    skipped, the same rule :func:`~semimc.forecasting.panel.fama_macbeth` uses when
    averaging these into a single robustness-check coefficient.
    """
    from statsmodels.regression.linear_model import OLS
    from statsmodels.tools.tools import add_constant

    usable = rows.dropna(subset=[*feature_columns, target_column, date_column])
    per_date: dict[pd.Timestamp, pd.Series] = {}
    for date, group in usable.groupby(date_column):
        if len(group) < min_cross_section:
            continue
        design = add_constant(group.loc[:, feature_columns], has_constant="add")
        try:
            fitted = OLS(group[target_column].to_numpy(), design.to_numpy()).fit()
        except (ValueError, np.linalg.LinAlgError):
            continue
        per_date[date] = pd.Series(fitted.params, index=design.columns)
    if not per_date:
        return pd.DataFrame(columns=["const", *feature_columns])
    return pd.DataFrame(per_date).T.sort_index()


def rolling_coefficient_stability(
    rows: pd.DataFrame, feature_columns: list[str], min_cross_section: int = 5
) -> dict[str, dict[str, float]]:
    """Sign-flip rate per feature, computed from the rolling (per-date) coefficient path."""
    path = fama_macbeth_coefficient_path(rows, feature_columns, min_cross_section=min_cross_section)
    out: dict[str, dict[str, float]] = {}
    for column in feature_columns:
        series = path[column] if column in path.columns else pd.Series(dtype=float)
        out[column] = {
            "n_dates": int(series.notna().sum()),
            "sign_flip_rate": sign_flip_rate(series) if series.notna().sum() >= 2 else float("nan"),
        }
    return out


def _fit_subset(
    rows: pd.DataFrame,
    feature_columns: list[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
    maxlags: int,
    min_observations: int,
) -> dict:
    subset = subset_by_date_range(rows, start, end).dropna(subset=[*feature_columns, "target"])
    if len(subset) < min_observations:
        return {"n_observations": len(subset), "result": None}
    try:
        result: PanelRegressionResult = fit_driscoll_kraay(subset, feature_columns, maxlags=maxlags)
    except ValueError:
        return {"n_observations": len(subset), "result": None}
    return {
        "n_observations": len(subset),
        "result": {
            "coefficients": result.coefficients.to_dict(),
            "t_stats": result.t_stats.to_dict(),
            "r_squared": result.r_squared,
        },
    }


def market_environment_split(
    rows: pd.DataFrame,
    feature_columns: list[str],
    downturn_start: pd.Timestamp = DEFAULT_DOWNTURN_START,
    downturn_end: pd.Timestamp = DEFAULT_DOWNTURN_END,
    rally_start: pd.Timestamp = DEFAULT_RALLY_START,
    rally_end: pd.Timestamp | None = None,
    maxlags: int = 1,
    min_observations: int = 30,
) -> dict[str, dict]:
    """Fit the same regression separately on the 2022 downturn and the rally that
    followed, the addendum's own example of a market-environment split.

    Returns ``{"downturn": {...}, "rally": {...}}``, each with ``n_observations`` and a
    ``result`` that is ``None`` when there were too few rows to fit -- reported as "not
    enough data" by the caller, never silently skipped or backfilled.
    """
    end = rally_end or pd.Timestamp(rows["date"].max())
    return {
        "downturn": _fit_subset(
            rows, feature_columns, downturn_start, downturn_end, maxlags, min_observations
        ),
        "rally": _fit_subset(rows, feature_columns, rally_start, end, maxlags, min_observations),
    }

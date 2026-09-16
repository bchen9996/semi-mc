"""Scoring a signal or a fitted regression: OOS R^2, Clark-West, rank IC, pinball, stability.

Every function here answers one question from the addendum's evaluation list, and none of
them are wired to a specific model -- they take plain arrays or a rows frame, so they work
identically on the panel regression, the sector regression, or a hand-built candidate.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from .panel import newey_west_mean


def oos_r_squared(
    actual: np.ndarray, predicted: np.ndarray, benchmark: np.ndarray | float = 0.0
) -> float:
    """Campbell-Thompson out-of-sample R^2: ``1 - SSE(model) / SSE(benchmark)``.

    ``benchmark`` defaults to zero, which is the natural null here: the panel regression's
    target is already net of CAPM and sector exposure, so "no signal" predicts exactly no
    residual return, not the historical mean (using the historical mean as the benchmark
    would silently reopen the door the drift policy exists to close).

    Positive means the signal beat that null out of sample; negative means it did worse
    than predicting nothing, which happens often and should be reported plainly.
    """
    a = np.asarray(actual, dtype=float)
    p = np.asarray(predicted, dtype=float)
    b = np.broadcast_to(np.asarray(benchmark, dtype=float), a.shape)
    mask = np.isfinite(a) & np.isfinite(p) & np.isfinite(b)
    if mask.sum() < 5:
        return float("nan")
    sse_model = float(np.sum((a[mask] - p[mask]) ** 2))
    sse_benchmark = float(np.sum((a[mask] - b[mask]) ** 2))
    if sse_benchmark <= 0:
        return float("nan")
    return 1.0 - sse_model / sse_benchmark


def clark_west_statistic(
    actual: np.ndarray, restricted: np.ndarray, unrestricted: np.ndarray, maxlags: int = 3
) -> tuple[float, float]:
    """Clark-West (2007) test for equal predictive accuracy of nested models.

    ``restricted`` is the null (predict zero / CAPM alone); ``unrestricted`` adds the
    signal. A plain Diebold-Mariano test is biased toward the smaller model here because
    the larger model's extra parameter adds estimation noise even when the true
    coefficient is exactly zero; Clark-West's adjustment term removes that bias so the test
    has the right size under the null the addendum wants to reject.

    Returns
    -------
    (t_stat, p_value)
        One-sided p-value (``unrestricted`` forecasts better), Clark-West's own normal
        approximation rather than a t-distribution.
    """
    a = np.asarray(actual, dtype=float)
    r = np.asarray(restricted, dtype=float)
    u = np.asarray(unrestricted, dtype=float)
    mask = np.isfinite(a) & np.isfinite(r) & np.isfinite(u)
    a, r, u = a[mask], r[mask], u[mask]
    if len(a) < 10:
        return float("nan"), float("nan")

    f = (a - r) ** 2 - ((a - u) ** 2 - (r - u) ** 2)
    mean, se = newey_west_mean(f, maxlags)
    if not se or not np.isfinite(se) or se <= 0:
        return float("nan"), float("nan")
    t_stat = mean / se
    p_value = float(1.0 - stats.norm.cdf(t_stat))
    return float(t_stat), p_value


def rank_information_coefficient(
    predicted: pd.Series,
    realized: pd.Series,
    dates: pd.Series,
    newey_west_lags: int = 3,
    min_cross_section: int = 5,
) -> dict[str, float | int]:
    """Monthly rank IC (Spearman correlation of predicted vs. realized, per date), averaged
    over time with a Newey-West t-stat.

    With 15 names, any single month's IC is nearly meaningless -- the addendum says so
    explicitly -- so this always reports ``n_dates`` alongside the average, and callers
    should read the t-stat, not the raw mean, as the number that matters.
    """
    frame = pd.DataFrame({"predicted": predicted, "realized": realized, "date": dates}).dropna()
    ics: list[float] = []
    for _, group in frame.groupby("date"):
        if len(group) < min_cross_section:
            continue
        ic = group["predicted"].corr(group["realized"], method="spearman")
        if np.isfinite(ic):
            ics.append(float(ic))

    if not ics:
        return {
            "mean_ic": float("nan"),
            "newey_west_se": float("nan"),
            "t_stat": float("nan"),
            "n_dates": 0,
        }
    mean, se = newey_west_mean(np.array(ics), newey_west_lags)
    return {
        "mean_ic": mean,
        "newey_west_se": se,
        "t_stat": mean / se if se and np.isfinite(se) and se > 0 else float("nan"),
        "n_dates": len(ics),
    }


def pinball_losses(
    quantile_predictions: dict[float, np.ndarray], actual: np.ndarray
) -> dict[float, float]:
    """Mean pinball loss at each quantile level -- how quantile-regression tilts are scored.

    A thin wrapper around ``sklearn.metrics.mean_pinball_loss`` so callers depend on this
    module rather than sklearn directly, and so the NaN-handling convention (drop rather
    than error) stays consistent with the rest of this package.
    """
    from sklearn.metrics import mean_pinball_loss

    a = np.asarray(actual, dtype=float)
    out: dict[float, float] = {}
    for level, prediction in quantile_predictions.items():
        p = np.asarray(prediction, dtype=float)
        mask = np.isfinite(a) & np.isfinite(p)
        if mask.sum() >= 5:
            out[level] = float(mean_pinball_loss(a[mask], p[mask], alpha=level))
        else:
            out[level] = float("nan")
    return out


def sign_flip_rate(coefficient_series: pd.Series) -> float:
    """Fraction of consecutive non-zero coefficient estimates that changed sign.

    A crude but legible stability check the addendum asks for: a coefficient that keeps
    flipping sign across refits is not a stable effect, whatever any single period's
    t-statistic says.
    """
    values = coefficient_series.dropna().to_numpy()
    if len(values) < 2:
        return float("nan")
    signs = np.sign(values)
    nonzero = signs != 0
    pairs = nonzero[:-1] & nonzero[1:]
    if pairs.sum() == 0:
        return float("nan")
    flips = (signs[:-1] != signs[1:]) & pairs
    return float(flips.sum() / pairs.sum())


def subset_by_date_range(
    rows: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, date_column: str = "date"
) -> pd.DataFrame:
    """Rows within one market environment -- used to split, e.g., the 2022 downturn from
    the rally that followed, per the addendum's stability check."""
    stamp_start, stamp_end = pd.Timestamp(start), pd.Timestamp(end)
    return rows[(rows[date_column] >= stamp_start) & (rows[date_column] <= stamp_end)]

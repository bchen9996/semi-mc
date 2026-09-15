"""Monte Carlo convergence diagnostics.

Every number this tool produces carries two errors: model error, which backtesting
measures, and *Monte Carlo* error, which is purely the cost of using a finite number of
paths. Reporting a 5th percentile to four decimals from 500 paths would be false
precision, so the diagnostic shows how the tail statistics settle as the path count grows,
with standard errors attached.

Standard errors come from a bootstrap over the simulated sample rather than from an
asymptotic formula: the quantile standard error ``sqrt(p(1-p)/n) / f(q)`` needs the
density at the quantile, and estimating that for a fat-tailed simulated distribution is
less reliable than resampling.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .risk import conditional_value_at_risk, value_at_risk


def bootstrap_standard_error(
    sample: np.ndarray,
    statistic,
    n_bootstrap: int = 200,
    rng: np.random.Generator | None = None,
) -> float:
    """Standard error of a statistic, by resampling the simulated sample with replacement.

    Parameters
    ----------
    sample
        The simulated draws.
    statistic
        Callable mapping an array to a float.
    n_bootstrap
        Number of resamples. 200 is enough for a standard error reported to two
        significant figures.
    rng
        Generator, passed explicitly.

    Returns
    -------
    float
        Standard deviation of the statistic across resamples.
    """
    values = np.asarray(sample, dtype=float)
    generator = rng if rng is not None else np.random.default_rng(0)
    n = values.size
    if n < 2:
        return float("nan")
    draws = np.empty(n_bootstrap, dtype=float)
    for i in range(n_bootstrap):
        draws[i] = statistic(values[generator.integers(0, n, size=n)])
    return float(draws.std(ddof=1))


def convergence_table(
    terminal_log_returns: np.ndarray,
    path_counts: list[int] | None = None,
    var_level: float = 0.95,
    n_bootstrap: int = 200,
    rng: np.random.Generator | None = None,
) -> pd.DataFrame:
    """How the 5th percentile and CVaR settle as paths accumulate.

    Path counts are nested prefixes of one simulation rather than independent reruns:
    the paths are i.i.d. by construction, so a prefix is a valid smaller sample, and this
    shows the reader the *same* run stabilizing rather than a different one each row.

    Parameters
    ----------
    terminal_log_returns
        Cumulative log returns at the horizon, one per path.
    path_counts
        Sample sizes to report. Defaults to powers of two up to the full sample.
    var_level
        Confidence level for the CVaR column.
    n_bootstrap
        Bootstrap resamples per row.
    rng
        Generator for the bootstrap.

    Returns
    -------
    pd.DataFrame
        Columns: ``n_paths``, ``p5_return``, ``p5_se``, ``cvar``, ``cvar_se``,
        ``mean_return``, ``mean_se``.
    """
    simple = np.expm1(np.asarray(terminal_log_returns, dtype=float))
    total = simple.size
    generator = rng if rng is not None else np.random.default_rng(0)

    if path_counts is None:
        counts: list[int] = []
        size = 500
        while size < total:
            counts.append(size)
            size *= 2
        counts.append(total)
    else:
        counts = [c for c in path_counts if c <= total] or [total]

    rows = []
    for count in counts:
        subset = simple[:count]
        rows.append(
            {
                "n_paths": int(count),
                "p5_return": float(np.percentile(subset, 5)),
                "p5_se": bootstrap_standard_error(
                    subset, lambda a: float(np.percentile(a, 5)), n_bootstrap, generator
                ),
                "cvar": conditional_value_at_risk(subset, var_level),
                "cvar_se": bootstrap_standard_error(
                    subset,
                    lambda a: conditional_value_at_risk(a, var_level),
                    n_bootstrap,
                    generator,
                ),
                "var": value_at_risk(subset, var_level),
                "mean_return": float(subset.mean()),
                "mean_se": float(subset.std(ddof=1) / np.sqrt(count)),
            }
        )
    frame = pd.DataFrame(rows)
    # Carried so the summary and chart label the CVaR column with the level actually used
    # rather than assuming 95%.
    frame.attrs["var_level"] = float(var_level)
    return frame


def summarize_convergence(table: pd.DataFrame) -> str:
    """One-paragraph reading of a convergence table, for the run summary and HTML report."""
    if table.empty:
        return "No convergence diagnostic was produced."
    level = int(round(100 * table.attrs.get("var_level", 0.95)))
    final = table.iloc[-1]
    p5_relative = (
        abs(final["p5_se"] / final["p5_return"])
        if final["p5_return"] not in (0, np.nan)
        else np.nan
    )
    return (
        f"At {int(final['n_paths']):,} paths the 5th-percentile return is "
        f"{final['p5_return']:+.2%} with a Monte Carlo standard error of "
        f"{final['p5_se']:.2%}"
        + (f" ({p5_relative:.1%} of the estimate)" if np.isfinite(p5_relative) else "")
        + f", and {level}% CVaR is {final['cvar']:.2%} "
        f"(standard error {final['cvar_se']:.2%}). Monte Carlo error shrinks with the "
        "square root of the path count, so quadrupling paths halves it; model error does "
        "not shrink at all, which is what the backtest measures."
    )

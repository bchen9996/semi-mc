"""Risk and distribution analytics computed from simulated samples.

Sign convention: **VaR and CVaR are reported as positive losses.** A 95% VaR of 0.18
means "a 5% chance of losing 18% or more over the horizon". Returns themselves stay
signed, so a 5th-percentile return of -0.18 is the same fact stated the other way round.
CVaR (expected shortfall) is the average loss *given* the loss exceeds VaR, so it is
always at least as large as VaR.

Everything here works on the simulated terminal distribution, never on a fitted formula,
so the same functions serve every model.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def value_at_risk(returns: np.ndarray, level: float = 0.95) -> float:
    """Value at Risk as a positive loss.

    Parameters
    ----------
    returns
        Simple returns over the horizon, one per path.
    level
        Confidence level, e.g. 0.95. The loss quantile taken is ``1 - level``.

    Returns
    -------
    float
        Loss at the quantile. Negative when even the tail outcome is a gain, which is
        possible at short horizons for a high-drift asset.
    """
    if not 0 < level < 1:
        raise ValueError("level must be strictly between 0 and 1")
    return float(-np.quantile(np.asarray(returns, dtype=float), 1.0 - level))


def conditional_value_at_risk(returns: np.ndarray, level: float = 0.95) -> float:
    """Expected shortfall: mean loss conditional on breaching the VaR quantile."""
    values = np.asarray(returns, dtype=float)
    threshold = np.quantile(values, 1.0 - level)
    tail = values[values <= threshold]
    if tail.size == 0:  # every value identical; fall back to the threshold itself
        return float(-threshold)
    return float(-tail.mean())


def normal_var(mu: float, sigma: float, level: float = 0.95) -> float:
    """Closed-form VaR of a normal distribution. Used to test :func:`value_at_risk`."""
    from scipy import stats

    return float(-(mu + sigma * stats.norm.ppf(1.0 - level)))


def normal_cvar(mu: float, sigma: float, level: float = 0.95) -> float:
    """Closed-form expected shortfall of a normal distribution.

    ``CVaR = -(mu - sigma * phi(z_alpha) / alpha)`` with ``alpha = 1 - level`` and
    ``z_alpha`` the corresponding standard-normal quantile.
    """
    from scipy import stats

    alpha = 1.0 - level
    z = stats.norm.ppf(alpha)
    return float(-(mu - sigma * stats.norm.pdf(z) / alpha))


def probability_above(values: np.ndarray, threshold: float) -> float:
    """Share of paths finishing strictly above a threshold."""
    array = np.asarray(values, dtype=float)
    return float(np.mean(array > threshold))


def probability_loss_worse_than(returns: np.ndarray, loss: float) -> float:
    """Share of paths whose simple return is a loss of at least ``loss`` (a positive number)."""
    return float(np.mean(np.asarray(returns, dtype=float) <= -abs(loss)))


def percentile_table(values: np.ndarray, percentiles: list[int]) -> pd.Series:
    """Requested percentiles of a sample, indexed as ``p5``, ``p50``, and so on."""
    array = np.asarray(values, dtype=float)
    return pd.Series({f"p{p}": float(np.percentile(array, p)) for p in percentiles}, dtype=float)


def fan_percentiles(paths: np.ndarray, percentiles: list[int]) -> pd.DataFrame:
    """Per-step percentiles of price paths, for a fan chart.

    Parameters
    ----------
    paths
        Price paths, shape ``(n_paths, n_steps)``.
    percentiles
        Percentiles to compute, e.g. ``[5, 25, 50, 75, 95]``.

    Returns
    -------
    pd.DataFrame
        Index: step 0..n_steps-1. Columns: ``p5``, ``p25``, ...
    """
    array = np.asarray(paths, dtype=float)
    computed = np.percentile(array, percentiles, axis=0)
    return pd.DataFrame(
        computed.T, columns=[f"p{p}" for p in percentiles], index=pd.RangeIndex(array.shape[1])
    )


def risk_table(
    terminal_log_returns: np.ndarray,
    spot: float,
    max_drawdown: np.ndarray | None = None,
    var_levels: list[float] | None = None,
    loss_threshold: float = 0.20,
    price_threshold: float | None = None,
    horizon: int | None = None,
) -> pd.Series:
    """The Phase 1 risk table for one series.

    Parameters
    ----------
    terminal_log_returns
        Cumulative log return at the horizon, one per path.
    spot
        Price at the forecast origin.
    max_drawdown
        Per-path maximum drawdown (negative simple returns), if available.
    var_levels
        Confidence levels for VaR and CVaR. Defaults to 95% and 99%.
    loss_threshold
        The "probability of losing more than X" threshold, as a positive fraction.
    price_threshold
        Optional user-supplied price level; the probability of finishing above it is
        reported.
    horizon
        Recorded in the output for context.

    Returns
    -------
    pd.Series
        Named risk statistics. Prices are in USD, probabilities in [0, 1], VaR/CVaR are
        positive losses as fractions of the starting value.
    """
    levels = var_levels or [0.95, 0.99]
    log_returns = np.asarray(terminal_log_returns, dtype=float)
    simple = np.expm1(log_returns)
    prices = spot * np.exp(log_returns)

    out: dict[str, float] = {}
    if horizon is not None:
        out["horizon_days"] = float(horizon)
    out["spot"] = float(spot)
    out["median_price"] = float(np.median(prices))
    out["mean_price"] = float(np.mean(prices))
    out["median_return"] = float(np.median(simple))
    out["prob_gain"] = float(np.mean(simple > 0.0))
    out[f"prob_loss_over_{int(loss_threshold * 100)}pct"] = probability_loss_worse_than(
        simple, loss_threshold
    )

    for level in levels:
        tag = f"{int(round(level * 100))}"
        out[f"var_{tag}"] = value_at_risk(simple, level)
        out[f"cvar_{tag}"] = conditional_value_at_risk(simple, level)

    if price_threshold is not None:
        out["price_threshold"] = float(price_threshold)
        out["prob_above_threshold"] = probability_above(prices, price_threshold)

    if max_drawdown is not None and len(max_drawdown):
        drawdowns = np.asarray(max_drawdown, dtype=float)
        out["median_max_drawdown"] = float(np.median(drawdowns))
        out["p95_max_drawdown"] = float(np.percentile(drawdowns, 5))  # the worst 5% of paths

    return pd.Series(out, dtype=float)


def contribution_to_cvar(
    terminal_log_returns: np.ndarray,
    weights: np.ndarray,
    level: float = 0.95,
) -> pd.Series:
    """Each holding's share of portfolio expected shortfall.

    Computed as the component expected shortfall: average each holding's weighted simple
    return over exactly the paths where the *portfolio* breaches its VaR threshold. The
    components sum to the portfolio CVaR by construction, which is the property that makes
    this decomposition meaningful -- a marginal-VaR style approximation would not.

    Parameters
    ----------
    terminal_log_returns
        Shape ``(n_paths, n_tickers)``.
    weights
        Shape ``(n_tickers,)``, summing to 1.
    level
        Confidence level of the portfolio VaR used to select the tail paths.

    Returns
    -------
    pd.Series
        Contributions as positive losses, one per holding.
    """
    simple = np.expm1(np.asarray(terminal_log_returns, dtype=float))
    contributions = simple * weights.reshape(1, -1)
    portfolio = contributions.sum(axis=1)

    threshold = np.quantile(portfolio, 1.0 - level)
    tail = portfolio <= threshold
    if not tail.any():
        return pd.Series(np.zeros(simple.shape[1]), dtype=float)
    return pd.Series(-contributions[tail].mean(axis=0), dtype=float)

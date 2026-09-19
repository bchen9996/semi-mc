"""Portfolio volatility forecasts: the sizing input S3 and its shadow twin scale against.

Two ways to estimate the same thing, on purpose -- T3 exists to compare them. Neither
function decides a position; they only ever return a number.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..data import MarketData
from ..engine.runner import fit_at_origin, run_simulation
from ..models.base import BaseReturnModel


def portfolio_vol_forecast(
    market: MarketData,
    config,  # noqa: ANN001
    tickers: list[str],
    weights: pd.Series,
    as_of: pd.Timestamp,
    horizon: int,
    n_paths: int,
    rng: np.random.Generator,
    model_name: str = "blend",
) -> tuple[float, BaseReturnModel]:
    """Annualized volatility ``model_name`` forecasts for ``weights`` at ``horizon``.

    Fits ``model_name`` on ``tickers`` at ``as_of`` through :func:`fit_at_origin` -- the
    same path the backtester and ``simulate --portfolio`` use -- and simulates
    ``n_paths`` portfolio paths at ``horizon`` through :func:`run_simulation`, which
    aggregates per-ticker paths into portfolio value using **simple** returns
    (``engine/runner.py::portfolio_value_paths``), never a weighted average of log
    returns. The forecast is the empirical annualized standard deviation of the
    portfolio's own cumulative log return at that horizon -- not a closed-form
    combination of per-ticker variances, so it reflects whatever cross-ticker dependence
    ``model_name`` itself encodes, the same as a live ``simulate --portfolio`` run would.

    Returns
    -------
    tuple[float, BaseReturnModel]
        The forecast and the fitted model, so a caller that wants to record the fit
        (e.g. ``fitted.describe()`` in a decision's diagnostics) doesn't fit it twice.
    """
    context = market.context(as_of)
    fitted, _training = fit_at_origin(market, config, model_name, tickers, as_of, context)
    spot = market.spot(tickers, as_of)
    result = run_simulation(
        model=fitted,
        spot=spot,
        n_paths=n_paths,
        horizon=horizon,
        rng=rng,
        chunk_size=config.simulation.chunk_size,
        portfolio_weights=weights,
    )
    # `result.portfolio_value` is already the terminal value per path (normalized to a
    # starting value of 1.0) -- `run_simulation` only ever retains the terminal value and
    # the max drawdown per chunk, not full portfolio paths (see its own docstring).
    portfolio_log_return = np.log(result.portfolio_value)
    daily_variance = float(np.var(portfolio_log_return)) / horizon
    annual_vol = float(np.sqrt(daily_variance * config.simulation.trading_days_per_year))
    return annual_vol, fitted


def trailing_portfolio_vol(
    training_log_returns: pd.DataFrame,
    weights: pd.Series,
    window_days: int = 252,
    trading_days_per_year: int = 252,
) -> float:
    """Trailing realized annualized volatility of ``weights`` -- no model fit at all.

    ``training_log_returns`` (as returned by ``MarketData.training_window``) is converted
    to **simple** returns before aggregating across tickers -- log returns are for
    simulation, never portfolio aggregation (CLAUDE.md's conventions; a portfolio's log
    return is not the weighted average of its constituents' log returns), the same rule
    :func:`portfolio_vol_forecast` observes by going through ``run_simulation`` instead of
    combining log returns directly.
    """
    aligned = training_log_returns.reindex(columns=weights.index).iloc[-window_days:]
    simple_returns = np.expm1(aligned)
    portfolio_returns = simple_returns.to_numpy() @ weights.to_numpy()
    return float(np.std(portfolio_returns, ddof=1) * np.sqrt(trading_days_per_year))

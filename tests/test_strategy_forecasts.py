"""`strategy/forecasts.py`: the two ways S3 and its shadow twin size a position -- a model
forecast and trailing realized volatility. T3 exists to compare them, so both need their
own correctness check, not just "runs without crashing."
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from semimc.engine.runner import fit_at_origin
from semimc.strategy.forecasts import portfolio_vol_forecast, trailing_portfolio_vol
from semimc.strategy.weights import equal_weight


def test_portfolio_vol_forecast_matches_gbms_own_fitted_vol_for_a_single_ticker(market, config):
    """GBM's `annualized_variance` is constant and known the moment it's fitted -- a
    closed form the simulated forecast should converge to, the same anchor this project's
    other Monte Carlo tests use (CLAUDE.md: tolerances justified by MC error, not tuned
    until green)."""
    tickers = ["NVDA"]
    as_of = market.log_returns.index[-50]
    context = market.context(as_of)
    fitted, _ = fit_at_origin(market, config, "gbm", tickers, as_of, context)
    expected_vol = float(np.sqrt(fitted.annualized_variance(21).iloc[0]))

    rng = np.random.default_rng(123)
    forecast_vol, returned_model = portfolio_vol_forecast(
        market,
        config,
        tickers,
        equal_weight(tickers),
        as_of,
        horizon=21,
        n_paths=5000,
        rng=rng,
        model_name="gbm",
    )
    assert forecast_vol == pytest.approx(expected_vol, rel=0.15)
    assert returned_model.name == "gbm"


def test_portfolio_vol_forecast_is_positive_and_finite_for_a_real_portfolio(market, config):
    tickers = ["NVDA", "AMD"]
    as_of = market.log_returns.index[-50]
    rng = np.random.default_rng(7)
    forecast_vol, _ = portfolio_vol_forecast(
        market,
        config,
        tickers,
        equal_weight(tickers),
        as_of,
        horizon=21,
        n_paths=1000,
        rng=rng,
        model_name="blend",
    )
    assert np.isfinite(forecast_vol)
    assert 0.0 < forecast_vol < 5.0  # a sane annualized-vol range, not a tight bound


def test_trailing_portfolio_vol_matches_a_hand_built_series():
    """Known-by-construction case: two perfectly correlated tickers with a known daily
    simple-return standard deviation, so the annualized answer is exact, not simulated."""
    dates = pd.date_range("2024-01-01", periods=260, freq="B")
    daily_simple = 0.01  # constant-magnitude alternating return -> known population std
    simple_returns = pd.Series(
        [daily_simple if i % 2 == 0 else -daily_simple for i in range(len(dates))], index=dates
    )
    log_returns = pd.DataFrame(
        {"A": np.log1p(simple_returns), "B": np.log1p(simple_returns)}, index=dates
    )
    weights = pd.Series({"A": 0.5, "B": 0.5})

    vol = trailing_portfolio_vol(log_returns, weights, window_days=252, trading_days_per_year=252)
    expected = float(np.std(simple_returns.iloc[-252:].to_numpy(), ddof=1) * np.sqrt(252))
    assert vol == pytest.approx(expected, rel=1e-9)


def test_trailing_portfolio_vol_uses_simple_not_log_return_aggregation():
    """A regression guard for the project's own rule: portfolio aggregation must use
    simple returns, never a weighted average of log returns."""
    dates = pd.date_range("2024-01-01", periods=10, freq="B")
    # A large up move then an equal-magnitude simple-return down move is NOT symmetric in
    # log-return space -- exploiting that asymmetry is what would catch a caller that
    # mistakenly averaged log returns directly instead of going through simple returns.
    up = pd.Series([0.20] * 5 + [-0.20] * 5, index=dates)
    flat = pd.Series([0.0] * 10, index=dates)
    log_returns = pd.DataFrame({"A": np.log1p(up), "B": np.log1p(flat)})
    weights = pd.Series({"A": 0.5, "B": 0.5})

    vol = trailing_portfolio_vol(log_returns, weights, window_days=10, trading_days_per_year=252)

    # Correct (simple-return) aggregation: portfolio simple return = 0.5 * up + 0.5 * 0.
    portfolio_simple = 0.5 * up.to_numpy()
    expected = float(np.std(portfolio_simple, ddof=1) * np.sqrt(252))
    assert vol == pytest.approx(expected, rel=1e-9)

    # A caller that wrongly weighted log returns directly would get a different number.
    wrong_log = 0.5 * np.log1p(up).to_numpy()
    wrong = float(np.std(wrong_log, ddof=1) * np.sqrt(252))
    assert vol != pytest.approx(wrong, rel=1e-6)

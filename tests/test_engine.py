"""Simulation engine: chunking, drawdowns, portfolio aggregation, scenarios."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from semimc.analytics.risk import conditional_value_at_risk, contribution_to_cvar, risk_table
from semimc.data.returns import log_returns
from semimc.engine.runner import (
    _max_drawdown_from_prices,
    portfolio_value_paths,
    run_simulation,
    simulate_terminal_log_returns,
)
from semimc.models.base import FitContext
from semimc.models.gbm import GBMModel
from semimc.scenarios.stress import run_scenario


@pytest.fixture
def fitted(prices):
    panel = log_returns(prices)
    context = FitContext(
        as_of=panel.index[-1],
        risk_free_rate=0.04,
        benchmark_returns=panel.loc[:, ["SPY", "SOXX"]],
        drift_policy="capm",
    )
    return GBMModel(vol_window_days=500).fit(panel.loc[:, ["NVDA", "AMD"]], context), panel


# ---------------------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------------------


def test_chunking_does_not_change_the_result(fitted, prices):
    """Chunk size is a memory knob, not a modelling one: same seed, same output."""
    model, _ = fitted
    spot = prices.iloc[-1][["NVDA", "AMD"]]

    big = run_simulation(
        model, spot, 1000, 30, np.random.default_rng(3), chunk_size=1000, dtype="float64"
    )
    small = run_simulation(
        model, spot, 1000, 30, np.random.default_rng(3), chunk_size=100, dtype="float64"
    )
    assert np.allclose(big.terminal_log_returns, small.terminal_log_returns)


def test_float32_storage_stays_within_monte_carlo_error(fitted, prices):
    """float32 must not shift the tail statistics meaningfully."""
    model, _ = fitted
    spot = prices.iloc[-1][["NVDA", "AMD"]]
    wide = run_simulation(model, spot, 4000, 63, np.random.default_rng(5), dtype="float64")
    narrow = run_simulation(model, spot, 4000, 63, np.random.default_rng(5), dtype="float32")

    for index in range(2):
        a = np.percentile(wide.terminal_log_returns[:, index], 5)
        b = np.percentile(narrow.terminal_log_returns[:, index], 5)
        assert abs(a - b) < 1e-4


def test_simulate_terminal_matches_the_full_runner(fitted, prices):
    """The backtester's fast path must agree with the full engine."""
    model, _ = fitted
    spot = prices.iloc[-1][["NVDA", "AMD"]]
    full = run_simulation(model, spot, 800, 21, np.random.default_rng(11), dtype="float64")
    fast = simulate_terminal_log_returns(model, 800, 21, np.random.default_rng(11))
    assert np.allclose(full.terminal_log_returns, fast)


def test_tracked_paths_include_the_starting_price(fitted, prices):
    model, _ = fitted
    spot = prices.iloc[-1][["NVDA", "AMD"]]
    result = run_simulation(
        model, spot, 200, 40, np.random.default_rng(1), track=["NVDA"], dtype="float64"
    )
    paths = result.tracked_paths["NVDA"]
    assert paths.shape == (200, 41)
    assert np.allclose(paths[:, 0], float(spot["NVDA"]))


# ---------------------------------------------------------------------------------------
# Drawdowns
# ---------------------------------------------------------------------------------------


def test_max_drawdown_on_a_known_path():
    """100 -> 120 -> 60 -> 90 is a 50% drawdown from the 120 peak."""
    path = np.array([[100.0, 120.0, 60.0, 90.0]])
    assert _max_drawdown_from_prices(path)[0] == pytest.approx(-0.5)


def test_max_drawdown_of_a_monotonic_rise_is_zero():
    path = np.array([[100.0, 101.0, 102.0, 103.0]])
    assert _max_drawdown_from_prices(path)[0] == pytest.approx(0.0)


def test_drawdown_is_measured_from_the_starting_price(fitted, prices):
    """A path that only ever falls must still report a drawdown, not zero."""
    path = np.array([[100.0, 90.0, 80.0]])
    assert _max_drawdown_from_prices(path)[0] == pytest.approx(-0.2)


# ---------------------------------------------------------------------------------------
# Portfolio aggregation
# ---------------------------------------------------------------------------------------


def test_portfolio_uses_simple_returns_not_log_returns():
    """The core rule from Section 8, tested on a case where the two visibly differ.

    Two assets, one up 50% and one down 30%, equally weighted. In simple returns the
    portfolio is up 10%. Averaging log returns would give exp((ln1.5 + ln0.7)/2) - 1 =
    +2.5%, which is wrong and understates the portfolio.
    """
    log_paths = np.array([[[np.log(1.5), np.log(0.7)]]])  # one path, one step, two tickers
    weights = np.array([0.5, 0.5])
    values = portfolio_value_paths(log_paths, weights, rebalance_every=0)

    assert values[0, -1] == pytest.approx(0.5 * 1.5 + 0.5 * 0.7)
    assert values[0, -1] == pytest.approx(1.10)

    wrong = np.exp((np.log(1.5) + np.log(0.7)) / 2)
    assert abs(values[0, -1] - wrong) > 0.07, "simple and log aggregation must differ here"


def test_buy_and_hold_lets_weights_drift():
    """Without rebalancing the winner's weight grows, so the portfolio tilts toward it."""
    log_paths = np.zeros((1, 2, 2))
    log_paths[0, 0, 0] = np.log(2.0)  # ticker A doubles on day 1
    weights = np.array([0.5, 0.5])

    drifting = portfolio_value_paths(log_paths, weights, rebalance_every=0)
    rebalanced = portfolio_value_paths(log_paths, weights, rebalance_every=1)
    assert drifting[0, -1] == pytest.approx(rebalanced[0, -1])  # no further moves, so equal

    # Now make A fall back on day 2: the rebalanced book sold some of the winner.
    log_paths[0, 1, 0] = np.log(0.5)
    drifting = portfolio_value_paths(log_paths, weights, rebalance_every=0)
    rebalanced = portfolio_value_paths(log_paths, weights, rebalance_every=1)
    assert rebalanced[0, -1] > drifting[0, -1]


def test_portfolio_starts_at_one():
    log_paths = np.zeros((5, 10, 3))
    weights = np.array([0.2, 0.3, 0.5])
    values = portfolio_value_paths(log_paths, weights, rebalance_every=0)
    assert np.allclose(values[:, 0], 1.0)
    assert np.allclose(values[:, -1], 1.0), "zero returns must leave the value unchanged"


def test_portfolio_risk_is_below_the_weighted_average_of_its_parts(fitted, prices):
    """Diversification must actually show up in the simulated portfolio CVaR."""
    model, _ = fitted
    spot = prices.iloc[-1][["NVDA", "AMD"]]
    weights = pd.Series({"NVDA": 0.5, "AMD": 0.5})
    result = run_simulation(
        model,
        spot,
        6000,
        63,
        np.random.default_rng(9),
        portfolio_weights=weights,
        dtype="float64",
    )

    portfolio_cvar = conditional_value_at_risk(result.portfolio_value - 1.0, 0.95)
    singles = [
        conditional_value_at_risk(np.expm1(result.terminal_log_returns[:, i]), 0.95)
        for i in range(2)
    ]
    assert portfolio_cvar < 0.5 * singles[0] + 0.5 * singles[1]


def test_contributions_sum_to_portfolio_cvar(fitted, prices):
    """The decomposition is only meaningful if the components add up."""
    model, _ = fitted
    spot = prices.iloc[-1][["NVDA", "AMD"]]
    result = run_simulation(model, spot, 5000, 63, np.random.default_rng(4), dtype="float64")

    weights = np.array([0.4, 0.6])
    contributions = contribution_to_cvar(result.terminal_log_returns, weights, level=0.95)

    portfolio_returns = np.expm1(result.terminal_log_returns) @ weights
    total = conditional_value_at_risk(portfolio_returns, 0.95)
    assert float(contributions.sum()) == pytest.approx(total, rel=1e-9)


# ---------------------------------------------------------------------------------------
# Risk table
# ---------------------------------------------------------------------------------------


def test_risk_table_probabilities_are_consistent(fitted, prices):
    model, _ = fitted
    spot = prices.iloc[-1][["NVDA", "AMD"]]
    result = run_simulation(model, spot, 5000, 63, np.random.default_rng(2), dtype="float64")
    table = risk_table(
        result.terminal_for("NVDA"),
        float(spot["NVDA"]),
        max_drawdown=result.max_drawdown[:, 0],
        price_threshold=float(spot["NVDA"]),
    )

    assert 0 <= table["prob_gain"] <= 1
    assert table["cvar_95"] >= table["var_95"]
    assert table["cvar_99"] >= table["var_99"]
    assert table["var_99"] >= table["var_95"]
    assert table["median_max_drawdown"] <= 0
    # Probability of finishing above spot is the same event as a gain.
    assert table["prob_above_threshold"] == pytest.approx(table["prob_gain"], abs=0.001)


# ---------------------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------------------


def test_sector_shock_scales_with_beta(fitted, prices):
    """A beta-2 name must fall roughly twice as far as a beta-1 name."""
    model, panel = fitted
    paths = np.zeros((500, 21, 2))
    betas = pd.Series({"NVDA": 2.0, "AMD": 1.0})

    result = run_scenario(
        "shock",
        {"kind": "sector_shock", "shock": -0.20},
        paths,
        ["NVDA", "AMD"],
        betas_sector=betas,
    )
    impact = result.table()
    assert impact.loc["NVDA", "scenario_median"] == pytest.approx(-0.40, abs=0.01)
    assert impact.loc["AMD", "scenario_median"] == pytest.approx(-0.20, abs=0.01)


def test_custom_shock_applies_only_to_named_tickers(fitted):
    paths = np.zeros((100, 10, 2))
    result = run_scenario(
        "export",
        {"kind": "custom_shock", "shocks": {"NVDA": -0.15}},
        paths,
        ["NVDA", "AMD"],
    )
    impact = result.table()
    assert impact.loc["NVDA", "scenario_median"] == pytest.approx(-0.15, abs=0.001)
    assert impact.loc["AMD", "scenario_median"] == pytest.approx(0.0, abs=0.001)


def test_volatility_multiplier_widens_without_moving_the_centre(fitted, prices, rng):
    """2x volatility must roughly double the spread and leave the median alone."""
    model, _ = fitted
    paths = np.asarray(model.simulate(4000, 42, rng), dtype=float)
    drift = model.daily_log_drift(42)

    result = run_scenario(
        "vol",
        {"kind": "vol_multiplier", "multiplier": 2.0, "days": 42},
        paths,
        model.tickers,
        daily_drift=drift,
    )
    baseline_spread = float(np.std(result.baseline_terminal[:, 0]))
    scenario_spread = float(np.std(result.terminal_log_returns[:, 0]))
    assert scenario_spread == pytest.approx(2 * baseline_spread, rel=0.05)

    impact = result.table()
    assert abs(impact.loc["NVDA", "median_impact"]) < 0.02


def test_historical_replay_reproduces_the_benchmark_move(fitted, prices):
    """A beta-1 name replaying a -30% benchmark window should land near -30%."""
    model, panel = fitted
    paths = np.zeros((500, 60, 2))
    benchmark = panel["SPY"].copy()
    window = benchmark.index[100:130]
    benchmark.loc[window] = np.log(0.99)  # 30 sessions of -1% each

    result = run_scenario(
        "replay",
        {"kind": "historical_replay", "start": window[0], "end": window[-1]},
        paths,
        ["NVDA", "AMD"],
        betas_sector=pd.Series({"NVDA": 1.0, "AMD": 0.5}),
        benchmark_returns=benchmark,
    )
    impact = result.table()
    expected = np.expm1(30 * np.log(0.99))
    assert impact.loc["NVDA", "scenario_median"] == pytest.approx(expected, abs=0.01)
    assert impact.loc["AMD", "scenario_median"] > impact.loc["NVDA", "scenario_median"]


def test_unknown_scenario_kind_raises(fitted):
    with pytest.raises(ValueError, match="unknown kind"):
        run_scenario("bad", {"kind": "nonsense"}, np.zeros((10, 5, 1)), ["NVDA"])

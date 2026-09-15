"""Model behaviour: the drift contract, the bootstrap's support, GARCH recovery.

The most important tests here are the drift ones. Section 8 of the brief says no model may
inherit the historical sample mean, and that is a property a model can lose silently
during a refactor -- nothing would crash, the numbers would just quietly become wrong.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from semimc.data.returns import log_returns
from semimc.models import build_model
from semimc.models.base import FitContext, NotFittedError
from semimc.models.bootstrap import (
    BlockBootstrapModel,
    optimal_block_length,
    stationary_bootstrap_indices,
)
from semimc.models.drift import annual_arithmetic_drift, historical_annual_arithmetic
from semimc.models.earnings_jumps import EarningsCalendarMissing, EarningsJumpModel
from semimc.models.factor import FactorModel
from semimc.models.fhs import FilteredHistoricalSimulation
from semimc.models.garch_t import GJRGarchTModel
from semimc.models.gbm import GBMModel

SINGLE_MODELS = ["gbm", "bootstrap", "garch_t"]
MULTI_MODELS = ["gbm", "bootstrap", "garch_t", "factor", "fhs"]


@pytest.fixture
def panel(prices) -> pd.DataFrame:
    return log_returns(prices)


@pytest.fixture
def context(panel, config) -> FitContext:
    from semimc.data.earnings import load_calendar

    return FitContext(
        as_of=panel.index[-1],
        risk_free_rate=0.04,
        benchmark_returns=panel.loc[:, ["SPY", "SOXX"]],
        market_ticker="SPY",
        sector_ticker="SOXX",
        drift_policy="capm",
        equity_risk_premium=0.05,
        earnings=load_calendar(config.path(config.data.earnings_calendar)),
    )


# ---------------------------------------------------------------------------------------
# The drift contract
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", MULTI_MODELS)
def test_every_model_imposes_the_configured_drift(name, panel, context, config, rng):
    """Simulated paths must be centred on the configured drift, not the sample mean.

    The drift is exact in expectation, not in any particular finite sample, so the
    tolerance is four standard errors of the simulated mean rather than machine epsilon.
    """
    training = panel.loc[:, ["NVDA", "AMD"]]
    model = build_model(name, config).fit(training, context)

    n_paths, horizon = 4000, 63
    simulated = np.asarray(model.simulate(n_paths, horizon, rng), dtype=float)
    target = model.daily_log_drift(horizon).to_numpy()
    realized = simulated.mean(axis=(0, 1))

    # Paths are independent, steps within a path are not, so the effective sample size for
    # the mean is the path count.
    standard_error = simulated.sum(axis=1).std(axis=0, ddof=1) / horizon / np.sqrt(n_paths)
    assert np.all(np.abs(realized - target) < 4 * standard_error), (
        f"{name}: realized daily mean {realized} vs target {target} (4 SE = {4 * standard_error})"
    )


@pytest.mark.parametrize("name", SINGLE_MODELS)
def test_no_model_inherits_the_sample_mean(name, panel, context, config, rng):
    """A ticker with a large historical drift must not carry it into the simulation."""
    training = panel.loc[:, ["NVDA"]].copy()
    # Give the history a huge upward drift: 0.4% a day is roughly 170% a year.
    training["NVDA"] = training["NVDA"] + 0.004

    model = build_model(name, config).fit(training, context)
    horizon = 21
    realized_annual = model.simulate(4000, horizon, rng).mean(axis=(0, 1))[0] * 252
    historical_annual = historical_annual_arithmetic(training["NVDA"])

    assert historical_annual > 0.8, "fixture setup should produce a large historical drift"
    assert realized_annual < 0.5, (
        f"{name} inherited the sample mean: simulated {realized_annual:.2f}/yr"
    )


def test_drift_policies_produce_the_expected_ordering(panel):
    """risk_free < capm for a high-beta name, and manual does exactly what it is told."""
    training = panel.loc[:, ["NVDA"]]
    benchmark = panel["SPY"]
    shared = {
        "tickers": ["NVDA"],
        "log_returns": training,
        "benchmark_returns": benchmark,
        "risk_free": 0.04,
        "equity_risk_premium": 0.05,
    }

    risk_free = annual_arithmetic_drift(policy="risk_free", **shared)["NVDA"]
    capm = annual_arithmetic_drift(policy="capm", **shared)["NVDA"]
    manual = annual_arithmetic_drift(
        policy="manual", manual_annual_return={"NVDA": 0.123}, **shared
    )["NVDA"]
    shrunk = annual_arithmetic_drift(policy="shrunk_historical", shrinkage_weight=0.25, **shared)[
        "NVDA"
    ]

    assert risk_free == pytest.approx(0.04)
    assert capm > risk_free, "a beta above zero must earn more than the risk-free rate"
    assert manual == pytest.approx(0.123)
    historical = historical_annual_arithmetic(training["NVDA"])
    assert shrunk == pytest.approx(0.25 * historical + 0.75 * capm)


def test_drift_uses_the_ito_correction(panel, context, config):
    """The log drift must be ``(mu - sigma^2/2)/252``, not ``mu/252``."""
    model = GBMModel(vol_window_days=500).fit(panel.loc[:, ["NVDA"]], context)
    sigma = float(model.annual_vol["NVDA"])
    mu = float(model.annual_arithmetic_drift["NVDA"])
    assert model.daily_log_drift(63)["NVDA"] == pytest.approx((mu - sigma**2 / 2) / 252)


def test_manual_drift_requires_every_ticker(panel, context, config):
    with pytest.raises(ValueError, match="manual_annual_return"):
        annual_arithmetic_drift(
            tickers=["NVDA", "AMD"],
            log_returns=panel,
            benchmark_returns=panel["SPY"],
            policy="manual",
            risk_free=0.04,
            manual_annual_return={"NVDA": 0.1},
        )


# ---------------------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------------------


def test_bootstrap_draws_only_from_the_training_window(panel, context, rng):
    """Every simulated return must be a training return plus the constant drift.

    This is the test that catches a bootstrap accidentally resampling the full history
    rather than the fit window.
    """
    training = panel.loc[:, ["NVDA"]].iloc[-400:]
    model = BlockBootstrapModel(block_length=5).fit(training, context)

    simulated = model.simulate(200, 30, rng)[:, :, 0]
    drift = model.daily_log_drift(30)["NVDA"]
    values = training["NVDA"].to_numpy()
    demeaned = np.round(values - values.mean(), 9)
    recovered = np.unique(np.round(simulated - drift, 9))

    assert set(recovered.tolist()).issubset(set(demeaned.tolist()))
    assert len(recovered) <= len(demeaned)


def test_bootstrap_cannot_exceed_the_worst_historical_day(panel, context, rng):
    """A documented limitation, asserted so it stays documented and true."""
    training = panel.loc[:, ["NVDA"]].iloc[-600:]
    model = BlockBootstrapModel(block_length=3).fit(training, context)
    simulated = model.simulate(500, 21, rng)[:, :, 0]
    drift = model.daily_log_drift(21)["NVDA"]
    worst_training = float((training["NVDA"] - training["NVDA"].mean()).min())
    assert (simulated - drift).min() >= worst_training - 1e-12


def test_stationary_bootstrap_index_structure(rng):
    """Indices must advance by one within a block and wrap at the end of the sample."""
    indices = stationary_bootstrap_indices(50, n_paths=200, horizon=40, block_length=8, rng=rng)
    assert indices.shape == (200, 40)
    assert indices.min() >= 0 and indices.max() < 50

    steps = (indices[:, 1:] - indices[:, :-1]) % 50
    continued = float(np.mean(steps == 1))
    # With mean block length 8 the continuation probability is 1 - 1/8 = 0.875.
    assert 0.80 < continued < 0.93


def test_optimal_block_length_is_larger_for_persistent_data(rng):
    """Autocorrelated data should get a longer block than white noise."""
    n = 1500
    white = pd.DataFrame({"x": rng.standard_normal(n)})
    persistent = np.zeros(n)
    for t in range(1, n):
        persistent[t] = 0.85 * persistent[t - 1] + rng.standard_normal()
    assert optimal_block_length(pd.DataFrame({"x": persistent})) > optimal_block_length(white)


# ---------------------------------------------------------------------------------------
# GARCH
# ---------------------------------------------------------------------------------------


def test_garch_recovers_known_parameters(panel, context):
    """Fitted parameters must be close to the ones the fixture was generated with.

    The fixture's NVDA series carries a GJR-GARCH with omega 0.05, alpha 0.06, gamma 0.08
    and beta 0.86 in percent units, plus market and sector components. The recovered
    persistence should land near the true 0.96; tolerances are wide because the series
    also contains factor exposure and earnings jumps the model does not know about.
    """
    model = GJRGarchTModel().fit(panel.loc[:, ["NVDA"]], context)
    params = model.params["NVDA"]

    assert 0.80 < params["persistence"] < 1.0, params
    assert params["gamma"] > 0, "the GJR asymmetry term should be positive for equities"
    assert 2.5 < params["nu"] < 30, f"Student-t nu out of range: {params['nu']}"


def test_garch_simulation_starts_from_todays_conditional_volatility(panel, context, rng):
    """Day-1 simulated variance must match the model's one-step-ahead forecast."""
    model = GJRGarchTModel().fit(panel.loc[:, ["NVDA"]], context)
    simulated = model.simulate(20_000, 5, rng)[:, :, 0]
    drift = model.daily_log_drift(5)["NVDA"]
    day_one_variance = float(np.var(simulated[:, 0] - drift, ddof=1))
    assert day_one_variance == pytest.approx(model._h0["NVDA"], rel=0.06)


def test_garch_variance_mean_reverts_towards_the_long_run(panel, context):
    """Average forecast variance over a long horizon must sit nearer the long-run level."""
    model = GJRGarchTModel().fit(panel.loc[:, ["NVDA"]], context)
    params = model.params["NVDA"]
    long_run = params["omega"] / (1 - params["persistence"]) * 252
    short = float(model.annualized_variance(1)["NVDA"])
    long = float(model.annualized_variance(252)["NVDA"])
    assert abs(long - long_run) < abs(short - long_run) or short == pytest.approx(long, rel=1e-6)


def test_garch_produces_volatility_clustering(panel, context, rng):
    """Squared simulated returns must be autocorrelated; GBM's are not."""
    garch = GJRGarchTModel().fit(panel.loc[:, ["NVDA"]], context)
    gbm = GBMModel(vol_window_days=500).fit(panel.loc[:, ["NVDA"]], context)

    def squared_autocorrelation(model) -> float:
        paths = model.simulate(2000, 120, rng)[:, :, 0]
        squared = (paths - paths.mean()) ** 2
        flat_a = squared[:, :-1].ravel()
        flat_b = squared[:, 1:].ravel()
        return float(np.corrcoef(flat_a, flat_b)[0, 1])

    assert squared_autocorrelation(garch) > squared_autocorrelation(gbm) + 0.02


def test_garch_has_fatter_tails_than_gbm(panel, context, rng):
    """The whole point of Student-t innovations, asserted."""
    garch = GJRGarchTModel().fit(panel.loc[:, ["NVDA"]], context)
    gbm = GBMModel(vol_window_days=500).fit(panel.loc[:, ["NVDA"]], context)
    terminal_garch = garch.simulate(8000, 21, rng)[:, :, 0].sum(axis=1)
    terminal_gbm = gbm.simulate(8000, 21, rng)[:, :, 0].sum(axis=1)
    assert np.percentile(terminal_garch, 0.5) < np.percentile(terminal_gbm, 0.5)


# ---------------------------------------------------------------------------------------
# Multi-asset models
# ---------------------------------------------------------------------------------------


def test_factor_model_reproduces_cross_sectional_correlation(panel, context, rng):
    """Factor exposure alone should recreate most of the observed co-movement."""
    training = panel.loc[:, ["NVDA", "AMD"]]
    model = FactorModel().fit(training, context)
    simulated = model.simulate(6000, 21, rng).reshape(-1, 2)
    simulated_correlation = float(np.corrcoef(simulated.T)[0, 1])
    sample_correlation = float(training.corr().iloc[0, 1])
    assert simulated_correlation == pytest.approx(sample_correlation, abs=0.10)


def test_fhs_preserves_cross_sectional_dependence(panel, context, rng):
    """Resampling residual rows by date must keep the tickers moving together."""
    training = panel.loc[:, ["NVDA", "AMD"]]
    model = FilteredHistoricalSimulation().fit(training, context)
    simulated = model.simulate(6000, 21, rng).reshape(-1, 2)
    assert float(np.corrcoef(simulated.T)[0, 1]) == pytest.approx(
        float(training.corr().iloc[0, 1]), abs=0.12
    )


def test_fhs_resamples_whole_dates_not_independent_draws(panel, context, rng):
    """By-date resampling must carry the residual correlation through to the simulation.

    This is the property that distinguishes filtered historical simulation from shuffling
    each ticker independently: whatever dependence the standardized residuals have, the
    simulated returns must inherit it. Asserting that the two correlations *match* is a
    stronger test than asserting the simulated one merely exceeds some threshold.
    """
    training = panel.loc[:, ["NVDA", "AMD"]]
    model = FilteredHistoricalSimulation().fit(training, context)
    residual_correlation = float(np.corrcoef(model._residuals.T)[0, 1])
    assert residual_correlation > 0.1, "fixture residuals should be positively correlated"

    simulated = model.simulate(4000, 10, rng).reshape(-1, 2)
    simulated_correlation = float(np.corrcoef(simulated.T)[0, 1])
    assert simulated_correlation == pytest.approx(residual_correlation, abs=0.06)


# ---------------------------------------------------------------------------------------
# Earnings overlay
# ---------------------------------------------------------------------------------------


@pytest.fixture
def earnings_setup(panel, context):
    """Training and context stopped 300 days early, so the calendar holds *future* reports.

    A live forecast only gets jumps if the calendar has dates ahead of the origin. Using
    an origin inside the fixture's span reproduces that, and is exactly what the
    backtester does at every rolling origin.
    """
    origin = panel.index[-300]
    return panel.loc[:origin, ["NVDA"]], context.truncated(origin)


def test_earnings_overlay_adds_variance_on_scheduled_days(earnings_setup, rng):
    """Jumps must widen the distribution relative to the same base model without them."""
    training, context = earnings_setup
    base = GBMModel(vol_window_days=500).fit(training, context)
    overlay = EarningsJumpModel(
        base_model=GBMModel(vol_window_days=500), min_earnings_observations=4
    ).fit(training, context)

    horizon = 126
    assert len(overlay._resolve_offsets(horizon)["NVDA"]) > 0, "fixture should schedule earnings"
    base_terminal = base.simulate(4000, horizon, rng)[:, :, 0].sum(axis=1)
    overlay_terminal = overlay.simulate(4000, horizon, rng)[:, :, 0].sum(axis=1)
    assert overlay_terminal.std() > base_terminal.std()


def test_earnings_overlay_does_not_add_drift(earnings_setup, rng):
    """Jumps are demeaned, so they contribute variance but no expected return."""
    training, context = earnings_setup
    overlay = EarningsJumpModel(
        base_model=GBMModel(vol_window_days=500), min_earnings_observations=4
    ).fit(training, context)
    n_paths, horizon = 6000, 126
    simulated = np.asarray(overlay.simulate(n_paths, horizon, rng), dtype=float)
    target = overlay.daily_log_drift(horizon).to_numpy()
    standard_error = simulated.sum(axis=1).std(axis=0, ddof=1) / horizon / np.sqrt(n_paths)
    assert np.all(np.abs(simulated.mean(axis=(0, 1)) - target) < 4 * standard_error)


def test_earnings_overlay_schedules_jumps_on_the_right_days(earnings_setup, rng):
    """Variance must spike on scheduled steps and not on neighbouring ones."""
    training, context = earnings_setup
    overlay = EarningsJumpModel(
        base_model=GBMModel(vol_window_days=500), min_earnings_observations=4
    ).fit(training, context)

    horizon = 126
    steps = overlay._resolve_offsets(horizon)["NVDA"]
    simulated = overlay.simulate(6000, horizon, rng)[:, :, 0]
    per_step_variance = simulated.var(axis=0)

    ordinary = [s for s in range(horizon) if s not in steps]
    assert per_step_variance[steps].mean() > 2 * per_step_variance[ordinary].mean()


def test_earnings_overlay_strips_jumps_from_training(panel, context, rng):
    """The base model must be fitted on data with the earnings reactions removed."""
    training = panel.loc[:, ["NVDA"]]
    overlay = EarningsJumpModel(
        base_model=GBMModel(vol_window_days=len(training)), min_earnings_observations=4
    ).fit(training, context)

    assert overlay.n_stripped["NVDA"] > 0
    plain = GBMModel(vol_window_days=len(training)).fit(training, context)
    # Removing the jumps must lower the base model's estimated volatility.
    assert float(overlay.base.annual_vol["NVDA"]) < float(plain.annual_vol["NVDA"])


def test_earnings_overlay_refuses_without_a_calendar(panel, context):
    """No calendar means no jumps -- and an explicit error, never invented dates."""
    bare = FitContext(
        as_of=context.as_of,
        risk_free_rate=0.04,
        benchmark_returns=context.benchmark_returns,
        earnings=None,
    )
    with pytest.raises(EarningsCalendarMissing):
        EarningsJumpModel(base_model=GBMModel()).fit(panel.loc[:, ["NVDA"]], bare)


# ---------------------------------------------------------------------------------------
# Interface contract
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", MULTI_MODELS)
def test_simulate_returns_the_documented_shape(name, panel, context, config, rng):
    model = build_model(name, config).fit(panel.loc[:, ["NVDA", "AMD"]], context)
    assert model.simulate(37, 11, rng).shape == (37, 11, 2)


def test_simulate_before_fit_raises(config, rng):
    with pytest.raises(NotFittedError):
        build_model("gbm", config).simulate(10, 5, rng)


def test_fit_rejects_data_after_the_origin(panel, context):
    """The model-level half of the leakage guard."""
    stale_context = FitContext(
        as_of=panel.index[-300], risk_free_rate=0.04, benchmark_returns=panel.loc[:, ["SPY"]]
    )
    with pytest.raises(ValueError, match="look-ahead"):
        GBMModel().fit(panel.loc[:, ["NVDA"]], stale_context)


def test_simulation_is_reproducible_from_the_seed(panel, context, config):
    """Same seed, same paths -- the basis of every run being reproducible."""
    model = build_model("garch_t", config).fit(panel.loc[:, ["NVDA"]], context)
    first = model.simulate(500, 21, np.random.default_rng(7))
    second = model.simulate(500, 21, np.random.default_rng(7))
    assert np.array_equal(first, second)
    third = model.simulate(500, 21, np.random.default_rng(8))
    assert not np.array_equal(first, third)

"""Statistical correctness: simulated output against closed-form values.

These are the tests that decide whether the engine is arithmetically right, as opposed to
merely running. Every tolerance is explicit and justified by Monte Carlo error rather than
tuned until it passed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from semimc.analytics.risk import (
    conditional_value_at_risk,
    normal_cvar,
    normal_var,
    value_at_risk,
)
from semimc.analytics.scoring import (
    christoffersen_independence,
    crps_from_samples,
    crps_normal,
    kupiec_pof,
    pit_value,
)
from semimc.models.base import FitContext
from semimc.models.gbm import GBMModel, nearest_psd_cholesky


def make_context(dates, benchmarks=None, policy="risk_free", rate=0.04) -> FitContext:
    return FitContext(
        as_of=dates[-1],
        risk_free_rate=rate,
        benchmark_returns=benchmarks if benchmarks is not None else pd.DataFrame(),
        drift_policy=policy,
        equity_risk_premium=0.05,
    )


# ---------------------------------------------------------------------------------------
# GBM moments
# ---------------------------------------------------------------------------------------


def test_gbm_log_return_mean_and_variance_match_closed_form(rng):
    """Simulated daily log returns must have the mean and variance GBM implies.

    Closed form: with an annual arithmetic drift ``mu`` and volatility ``sigma``, daily log
    returns are normal with mean ``(mu - sigma^2/2) / 252`` and variance ``sigma^2 / 252``.
    """
    dates = pd.bdate_range("2015-01-01", periods=1500)
    sigma_annual = 0.40
    daily_sigma = sigma_annual / np.sqrt(252)
    returns = pd.DataFrame({"AAA": rng.normal(0.0005, daily_sigma, len(dates))}, index=dates)

    model = GBMModel(vol_estimator="realized", vol_window_days=len(dates)).fit(
        returns, make_context(dates, policy="risk_free", rate=0.04)
    )

    n_paths, horizon = 40_000, 21
    simulated = model.simulate(n_paths, horizon, rng)[:, :, 0]

    fitted_sigma = float(model.annual_vol["AAA"])
    expected_mean = (0.04 - fitted_sigma**2 / 2) / 252
    expected_variance = fitted_sigma**2 / 252

    # Standard error of the mean over n_paths * horizon draws.
    standard_error = np.sqrt(expected_variance / (n_paths * horizon))
    assert abs(simulated.mean() - expected_mean) < 5 * standard_error
    assert simulated.var(ddof=1) == pytest.approx(expected_variance, rel=0.02)


def test_gbm_terminal_distribution_is_lognormal(rng):
    """Terminal log return over H days should be normal with mean H*mu and variance H*sigma^2."""
    dates = pd.bdate_range("2015-01-01", periods=1200)
    returns = pd.DataFrame({"AAA": rng.normal(0.0, 0.02, len(dates))}, index=dates)
    model = GBMModel(vol_window_days=len(dates)).fit(returns, make_context(dates))

    horizon = 63
    terminal = model.simulate(20_000, horizon, rng)[:, :, 0].sum(axis=1)
    daily = model.daily_log_drift(horizon)["AAA"]
    sigma = float(model.annual_vol["AAA"]) / np.sqrt(252)

    assert terminal.mean() == pytest.approx(
        horizon * daily, abs=4 * sigma * np.sqrt(horizon / 20_000)
    )
    assert terminal.std(ddof=1) == pytest.approx(sigma * np.sqrt(horizon), rel=0.03)

    # Normality of the terminal log return: skew and excess kurtosis near zero.
    assert abs(stats.skew(terminal)) < 0.1
    assert abs(stats.kurtosis(terminal)) < 0.15


def test_correlated_simulation_reproduces_target_correlation(rng):
    """A multi-ticker GBM must reproduce the training correlation matrix."""
    dates = pd.bdate_range("2015-01-01", periods=2000)
    target = np.array([[1.0, 0.75, 0.30], [0.75, 1.0, 0.45], [0.30, 0.45, 1.0]])
    factor = nearest_psd_cholesky(target)
    shocks = rng.standard_normal((len(dates), 3)) @ factor.T
    returns = pd.DataFrame(shocks * 0.02, index=dates, columns=["AAA", "BBB", "CCC"])

    model = GBMModel(vol_window_days=len(dates)).fit(returns, make_context(dates))
    simulated = model.simulate(20_000, 10, rng).reshape(-1, 3)
    realized = np.corrcoef(simulated.T)
    sample = returns.corr().to_numpy()

    # The model targets the *sample* correlation, so compare against that.
    assert np.abs(realized - sample).max() < 0.02


def test_nearest_psd_cholesky_handles_indefinite_matrix():
    """A slightly indefinite covariance must still factorize rather than raise."""
    indefinite = np.array([[1.0, 0.99, 0.99], [0.99, 1.0, 0.99], [0.99, 0.99, 1.0]])
    indefinite[0, 2] = indefinite[2, 0] = -0.999  # inconsistent, so not PSD
    factor = nearest_psd_cholesky(indefinite)
    assert np.all(np.isfinite(factor))
    assert factor.shape == (3, 3)


# ---------------------------------------------------------------------------------------
# Risk measures
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("level", [0.95, 0.99])
def test_var_and_cvar_match_normal_closed_form(rng, level):
    """Sample VaR and CVaR must converge to the closed-form normal values."""
    mu, sigma = 0.01, 0.20
    sample = rng.normal(mu, sigma, 500_000)

    assert value_at_risk(sample, level) == pytest.approx(normal_var(mu, sigma, level), abs=0.004)
    assert conditional_value_at_risk(sample, level) == pytest.approx(
        normal_cvar(mu, sigma, level), abs=0.006
    )


def test_cvar_is_at_least_var(rng):
    """Expected shortfall can never be smaller than the quantile it conditions on."""
    for _ in range(5):
        sample = rng.standard_t(df=5, size=20_000) * 0.1
        for level in (0.90, 0.95, 0.99):
            assert conditional_value_at_risk(sample, level) >= value_at_risk(sample, level)


def test_var_sign_convention_is_positive_loss():
    """A distribution that always loses 10% must report a VaR of +0.10, not -0.10."""
    sample = np.full(1000, -0.10)
    assert value_at_risk(sample, 0.95) == pytest.approx(0.10)
    assert conditional_value_at_risk(sample, 0.95) == pytest.approx(0.10)


# ---------------------------------------------------------------------------------------
# Scoring rules
# ---------------------------------------------------------------------------------------


def test_sample_crps_matches_closed_form_normal(rng):
    """The fair sample estimator must converge to the closed-form normal CRPS."""
    mu, sigma = 0.02, 0.15
    for observed in (-0.30, 0.0, 0.02, 0.25):
        sample = rng.normal(mu, sigma, 200_000)
        estimated = crps_from_samples(sample, observed)
        exact = crps_normal(mu, sigma, observed)
        assert estimated == pytest.approx(exact, abs=0.002), f"observed={observed}"


def test_crps_is_zero_for_a_perfect_point_forecast():
    """A degenerate forecast sitting exactly on the outcome scores 0."""
    assert crps_from_samples(np.full(500, 0.07), 0.07) == pytest.approx(0.0, abs=1e-12)


def test_crps_rewards_the_sharper_of_two_calibrated_forecasts(rng):
    """Between two unbiased forecasts, CRPS must prefer the narrower one."""
    observed = 0.0
    sharp = crps_from_samples(rng.normal(0.0, 0.05, 100_000), observed)
    vague = crps_from_samples(rng.normal(0.0, 0.25, 100_000), observed)
    assert sharp < vague


def test_crps_fair_estimator_is_not_biased_by_sample_size(rng):
    """The fair estimator must not improve simply because more paths were drawn.

    This is the reason for the ``m(m-1)`` denominator: the biased version falls
    systematically as ``m`` grows, which would let a model win a comparison by drawing
    more paths.
    """
    exact = crps_normal(0.0, 0.1, 0.05)
    small = np.mean([crps_from_samples(rng.normal(0, 0.1, 50), 0.05) for _ in range(4000)])
    large = np.mean([crps_from_samples(rng.normal(0, 0.1, 5000), 0.05) for _ in range(200)])
    assert small == pytest.approx(exact, abs=0.002)
    assert large == pytest.approx(exact, abs=0.002)


def test_pit_is_uniform_when_the_forecast_is_correct(rng):
    """Drawing the outcome from the forecast distribution gives uniform PIT values."""
    values = []
    for _ in range(4000):
        sample = rng.normal(0.0, 0.1, 500)
        observed = rng.normal(0.0, 0.1)
        values.append(pit_value(sample, observed, rng))
    statistic = stats.kstest(values, "uniform").pvalue
    assert statistic > 0.01, f"PIT not uniform (KS p={statistic})"


def test_pit_detects_an_overconfident_forecast(rng):
    """A too-narrow forecast pushes PIT mass into the tails."""
    values = [pit_value(rng.normal(0.0, 0.03, 500), rng.normal(0.0, 0.1), rng) for _ in range(2000)]
    extreme = np.mean([(v < 0.05) or (v > 0.95) for v in values])
    assert extreme > 0.3, "an overconfident forecast should breach its 90% interval often"


# ---------------------------------------------------------------------------------------
# VaR exception tests
# ---------------------------------------------------------------------------------------


def test_kupiec_accepts_a_correct_exception_rate():
    """250 observations with 13 exceptions at a 5% nominal rate must not reject."""
    result = kupiec_pof(250, 13, 0.05)
    assert result["p_value"] > 0.5


def test_kupiec_rejects_far_too_many_exceptions():
    """250 observations with 40 exceptions at 5% is a clear rejection."""
    result = kupiec_pof(250, 40, 0.05)
    assert result["p_value"] < 0.001


def test_kupiec_rejects_far_too_few_exceptions():
    """Overstating risk is a miscalibration too, and must also be caught."""
    assert kupiec_pof(500, 0, 0.05)["p_value"] < 0.001


def test_christoffersen_detects_clustered_exceptions():
    """Ten exceptions in a row is clustering, even though the rate may look fine."""
    clustered = np.zeros(200, dtype=int)
    clustered[50:60] = 1
    independent = np.zeros(200, dtype=int)
    independent[::20] = 1

    assert christoffersen_independence(clustered)["p_value"] < 0.01
    assert christoffersen_independence(independent)["p_value"] > 0.05

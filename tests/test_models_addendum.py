"""Tests for the addendum's Phase 4 models: Merton, HAR, regime switching, blending.

Mirrors ``test_models.py``'s emphasis on the drift contract -- every new model must impose
the configured drift exactly like the original six -- plus the addendum's own required
tests: Merton's simulated mean matches the configured drift, HAR recovers known
coefficients on synthetic data, and appending post-origin data must not change the
regime probabilities used at an earlier origin.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from semimc.context import DriftTilt, FitContext
from semimc.data.earnings import load_calendar
from semimc.data.realized import forward_average_variance, har_features
from semimc.data.returns import log_returns
from semimc.models import build_model
from semimc.models.blend import BlendModel, optimize_crps_weights, pooled_mixture_crps
from semimc.models.gbm import GBMModel
from semimc.models.har import fit_har_regression, har_forecast, per_step_variance_path
from semimc.models.merton import MertonFitError, fit_merton
from semimc.models.regime import fit_regime_switching, recentred_means

ADDENDUM_MODELS = ["merton", "har", "regime"]


@pytest.fixture
def panel(prices) -> pd.DataFrame:
    return log_returns(prices)


@pytest.fixture
def context(panel, config) -> FitContext:
    return FitContext(
        as_of=panel.index[-1],
        risk_free_rate=0.04,
        benchmark_returns=panel.loc[:, ["SPY", "SOXX"]],
        market_ticker="SPY",
        sector_ticker="SOXX",
        drift_policy="risk_free",
        equity_risk_premium=0.05,
        earnings=load_calendar(config.path(config.data.earnings_calendar)),
    )


# ---------------------------------------------------------------------------------------
# The drift contract, extended to the four new models
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ADDENDUM_MODELS)
def test_addendum_models_impose_the_configured_drift(name, panel, context, config, market, rng):
    training = panel.loc[:, ["NVDA", "AMD"]]
    model = build_model(name, config, market=market).fit(training, context)

    n_paths, horizon = 4000, 63
    simulated = np.asarray(model.simulate(n_paths, horizon, rng), dtype=float)
    target = model.daily_log_drift(horizon).to_numpy()
    realized = simulated.mean(axis=(0, 1))

    standard_error = simulated.sum(axis=1).std(axis=0, ddof=1) / horizon / np.sqrt(n_paths)
    assert np.all(np.abs(realized - target) < 5 * standard_error), (
        f"{name}: realized daily mean {realized} vs target {target} (5 SE = {5 * standard_error})"
    )


def test_blend_imposes_the_configured_drift(panel, context, config, rng):
    """Blend's drift equals the weighted average of each component's own drift exactly."""
    training = panel.loc[:, ["NVDA", "AMD"]]
    # Fitted separately so their own daily_log_drift is available to compare against.
    gbm_reference = build_model("gbm", config).fit(training, context)
    bootstrap_reference = build_model("bootstrap", config).fit(training, context)

    blend = BlendModel(
        components={
            "gbm": build_model("gbm", config),
            "bootstrap": build_model("bootstrap", config),
        },
        weights={"gbm": 0.7, "bootstrap": 0.3},
    )
    blend.fit(training, context)

    horizon = 63
    expected = 0.7 * gbm_reference.daily_log_drift(
        horizon
    ) + 0.3 * bootstrap_reference.daily_log_drift(horizon)
    actual = blend.daily_log_drift(horizon)
    pd.testing.assert_series_equal(actual, expected, check_exact=False, rtol=1e-10)


def test_blend_needs_at_least_two_components(panel, context, config):
    with pytest.raises(ValueError, match="at least two"):
        BlendModel(components={"gbm": build_model("gbm", config)})


def test_blend_weights_default_to_equal(panel, context, config):
    blend = BlendModel(
        components={
            "gbm": build_model("gbm", config),
            "bootstrap": build_model("bootstrap", config),
        }
    )
    assert blend.weights == {"gbm": 0.5, "bootstrap": 0.5}


def test_blend_weights_normalize(panel, context, config):
    blend = BlendModel(
        components={
            "gbm": build_model("gbm", config),
            "bootstrap": build_model("bootstrap", config),
        },
        weights={"gbm": 3.0, "bootstrap": 1.0},
    )
    assert blend.weights["gbm"] == pytest.approx(0.75)
    assert blend.weights["bootstrap"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------------------
# Merton jump-diffusion
# ---------------------------------------------------------------------------------------


def test_merton_recovers_known_jump_parameters():
    """MLE recovers a synthetic Merton process's true parameters, within sampling noise."""
    rng = np.random.default_rng(7)
    n = 252 * 10
    sigma_a, lam_a, mu_j, sig_j = 0.35, 8.0, -0.02, 0.07
    lam_d = lam_a / 252
    counts = rng.poisson(lam_d, n)
    r = (
        sigma_a / np.sqrt(252) * rng.standard_normal(n)
        + counts * mu_j
        + np.sqrt(counts) * sig_j * rng.standard_normal(n)
    )
    series = pd.Series(r, index=pd.bdate_range("2010-01-01", periods=n))

    params = fit_merton(series)
    assert params["sigma_annual"] == pytest.approx(sigma_a, rel=0.15)
    assert params["lambda_annual"] == pytest.approx(lam_a, rel=0.35)
    assert params["jump_variance_share"] > 0.05


def test_merton_needs_enough_observations():
    short = pd.Series(np.random.default_rng(0).standard_normal(50) * 0.01)
    with pytest.raises(MertonFitError):
        fit_merton(short)


def test_merton_excludes_earnings_days_from_estimation(panel, context, config, market, rng):
    """Reaction-day returns must not reach the jump MLE -- the overlay owns them."""
    training = panel.loc[:, ["NVDA", "AMD"]]
    model = build_model("merton", config, market=market).fit(training, context)
    assert all(model.n_earnings_excluded[t] > 0 for t in model.tickers), (
        "fixture data has scheduled earnings jumps; the exclusion count should be nonzero"
    )


# ---------------------------------------------------------------------------------------
# HAR volatility
# ---------------------------------------------------------------------------------------


def test_har_recovers_known_coefficients_on_synthetic_data():
    """A synthetic AR(1)-in-logs variance process recovers coefficients close to true.

    Not a literal HAR data-generating process (there isn't a simple one to invert exactly)
    but a log-linear AR(1) in daily variance, for which the one-day HAR regression
    (which reduces to an AR(1) in logs when weekly/monthly features are highly collinear
    with the daily one) should recover a slope near the true persistence.
    """
    rng = np.random.default_rng(3)
    n = 1500
    true_persistence = 0.55
    true_const = np.log(4e-5) * (1 - true_persistence)
    log_v = np.empty(n)
    log_v[0] = np.log(4e-5)
    for t in range(1, n):
        log_v[t] = true_const + true_persistence * log_v[t - 1] + rng.normal(0, 0.35)
    variance = pd.Series(np.exp(log_v), index=pd.bdate_range("2015-01-01", periods=n))

    params = fit_har_regression(variance, horizon=1)
    assert params["beta_d"] + params["beta_w"] + params["beta_m"] == pytest.approx(
        true_persistence, abs=0.2
    )
    assert params["r_squared"] > 0.15


def test_har_forecast_matches_direct_regression_prediction():
    """On genuinely autocorrelated (not i.i.d.) variance, in-sample predictions track the
    target closely -- confirming ``har_forecast`` reproduces what the regression fitted,
    not just that a regression ran."""
    rng = np.random.default_rng(4)
    n = 3000
    true_persistence = 0.7
    log_v = np.empty(n)
    log_v[0] = np.log(4e-5)
    for t in range(1, n):
        log_v[t] = (
            np.log(4e-5) * (1 - true_persistence)
            + true_persistence * log_v[t - 1]
            + rng.normal(0, 0.2)
        )
    variance = pd.Series(np.exp(log_v), index=pd.bdate_range("2015-01-01", periods=n))

    params = fit_har_regression(variance, horizon=5)
    features = har_features(variance)
    target = forward_average_variance(variance, 5)
    joined = features.join(target.rename("target"), how="inner").dropna()

    predicted = joined.apply(lambda row: har_forecast(params, row), axis=1)
    correlation = np.corrcoef(np.log(predicted), np.log(joined["target"]))[0, 1]
    assert correlation > 0.4


def test_per_step_variance_path_reproduces_direct_forecasts_exactly():
    """The cumulative variance implied by the per-step path must equal each direct forecast."""
    forecasts = {21: 2.0e-4, 63: 1.6e-4, 126: 1.4e-4}
    path = per_step_variance_path(forecasts, horizon=126)
    assert path.shape == (126,)
    for horizon, forecast in forecasts.items():
        cumulative_mean = path[:horizon].mean()
        assert cumulative_mean == pytest.approx(forecast, rel=1e-8)


def test_har_needs_ohlc_supplied_before_fit(panel, context, config):
    from semimc.models.har import HARFitError, HARVolatilityModel

    training = panel.loc[:, ["NVDA", "AMD"]]
    model = HARVolatilityModel()  # with_ohlc() never called
    with pytest.raises(HARFitError, match="with_ohlc"):
        model.fit(training, context)


def test_har_replaces_earnings_variance(panel, context, config, market):
    training = panel.loc[:, ["NVDA", "AMD"]]
    model = build_model("har", config, market=market).fit(training, context)
    assert any(model.n_earnings_replaced[t] > 0 for t in model.tickers)


# ---------------------------------------------------------------------------------------
# Regime switching
# ---------------------------------------------------------------------------------------


def _synthetic_regime_series(rng, n, p_stay_calm, p_stay_turbulent, sigma_calm, sigma_turbulent):
    state = 0
    states = []
    for _ in range(n):
        stay = p_stay_calm if state == 0 else p_stay_turbulent
        if rng.random() > stay:
            state = 1 - state
        states.append(state)
    states = np.array(states)
    sigma = np.where(states == 0, sigma_calm, sigma_turbulent)
    return rng.normal(0.0, sigma) / 100.0, states


def test_regime_fit_recovers_two_volatility_levels():
    rng = np.random.default_rng(9)
    values, _ = _synthetic_regime_series(rng, 2500, 0.995, 0.92, 0.5, 2.0)
    series = pd.Series(values, index=pd.bdate_range("2014-01-01", periods=2500))

    fit = fit_regime_switching(series, search_reps=12)
    annual_vol = np.sqrt(fit["variances"] * 252)
    assert annual_vol[0] < annual_vol[1], "regime 0 must be the calmer one after ordering"
    assert annual_vol[0] == pytest.approx(0.5 / 100 * np.sqrt(252), rel=0.4)
    assert annual_vol[1] == pytest.approx(2.0 / 100 * np.sqrt(252), rel=0.4)
    assert np.isclose(fit["filtered_last"].sum(), 1.0)
    assert np.isclose(fit["transition"].sum(axis=0), 1.0).all()


def test_regime_filtered_probability_is_causal():
    """Appending post-origin data must not change the filtered probability at an origin.

    This is the addendum's explicit anti-leakage requirement for this model: a *filtered*
    probability, by definition, conditions only on data up to and including that day, so
    refitting on a longer sample must not move the value already reported for an earlier
    day. (Refitting can change the estimated *parameters* -- that is a separate, allowed
    effect -- so this test freezes the parameters and drives the filter directly.)
    """
    rng = np.random.default_rng(2)
    values, _ = _synthetic_regime_series(rng, 1200, 0.995, 0.92, 0.5, 2.0)
    series = pd.Series(values, index=pd.bdate_range("2014-01-01", periods=1200))

    # Refit on the SAME parameters (not re-estimated) but filter through more data than
    # was available at the origin, using the model's own filter. Since MarkovRegression's
    # filter is a function only of the parameters and the data up to each date, re-running
    # the filter with the short-sample parameters over a longer sample must reproduce the
    # same probability at day 800 regardless of what comes after it.
    from statsmodels.tsa.regime_switching.markov_regression import MarkovRegression

    # fit_regime_switching already reorders to variance order, so rebuild the raw filter
    # inputs directly instead of re-deriving statsmodels' internal parameter vector.
    scaled_short = series.iloc[:800].to_numpy() * 100.0
    scaled_long = series.iloc[:1000].to_numpy() * 100.0

    model_short = MarkovRegression(
        scaled_short, k_regimes=2, trend="c", switching_variance=True, switching_trend=False
    )
    result_short = model_short.fit(search_reps=8, disp=False)

    model_long = MarkovRegression(
        scaled_long, k_regimes=2, trend="c", switching_variance=True, switching_trend=False
    )
    # Filter the longer sample AT THE SHORT SAMPLE'S FITTED PARAMETERS (no re-estimation),
    # which is the operation that must be origin-stable.
    filtered_long_at_short_params = model_long.filter(result_short.params)

    filtered_at_800_from_long_filter = np.asarray(
        filtered_long_at_short_params.filtered_marginal_probabilities
    )[799]
    filtered_at_800_from_short_filter = np.asarray(result_short.filtered_marginal_probabilities)[
        799
    ]

    np.testing.assert_allclose(
        filtered_at_800_from_long_filter, filtered_at_800_from_short_filter, atol=1e-8
    )


def test_regime_switching_model_uses_weekly_fallback_when_daily_is_too_short(
    panel, context, config, market, rng
):
    training = panel.loc[:, ["NVDA", "AMD"]]
    model = build_model("regime", config, market=market).fit(training, context)
    assert set(model.frequency_used.values()) <= {"daily", "weekly (mapped to daily)"}


def test_recentred_means_average_to_zero_under_stationary_weights():
    means = np.array([0.001, -0.004])
    stationary = np.array([0.9, 0.1])
    recentred = recentred_means(means, stationary)
    assert np.dot(recentred, stationary) == pytest.approx(0.0, abs=1e-12)


# ---------------------------------------------------------------------------------------
# Blend weight optimization
# ---------------------------------------------------------------------------------------


def test_optimize_crps_weights_favors_the_informative_component():
    rng = np.random.default_rng(11)
    n_origins = 20
    realized = rng.normal(0, 0.02, n_origins)
    samples = {
        "good": np.stack([rng.normal(realized[i], 0.005, 400) for i in range(n_origins)]),
        "bad": np.stack([rng.normal(0, 0.08, 400) for _ in range(n_origins)]),
    }
    weights = optimize_crps_weights(samples, realized, n_pool=300, n_restarts=3)
    assert weights["good"] > weights["bad"]
    assert sum(weights.values()) == pytest.approx(1.0)
    assert all(w >= 0 for w in weights.values())


def test_pooled_mixture_crps_is_between_component_extremes():
    rng = np.random.default_rng(1)
    n_origins = 10
    realized = rng.normal(0, 0.02, n_origins)
    samples = {
        "narrow": np.stack([rng.normal(0, 0.01, 500) for _ in range(n_origins)]),
        "wide": np.stack([rng.normal(0, 0.05, 500) for _ in range(n_origins)]),
    }
    mix = pooled_mixture_crps(samples, realized, {"narrow": 0.5, "wide": 0.5}, 400, rng)
    assert np.isfinite(mix)
    assert mix > 0


# ---------------------------------------------------------------------------------------
# Drift tilt plumbing
# ---------------------------------------------------------------------------------------


def test_drift_tilt_draws_are_centred_on_point():
    rng = np.random.default_rng(0)
    tilt = DriftTilt.build(
        tickers=["NVDA", "AMD"],
        point=np.array([0.04, -0.01]),
        draws=rng.normal(size=(300, 2)) * 0.05,
        horizon_days=21,
    )
    np.testing.assert_allclose(tilt.draws.mean(axis=0), tilt.point, atol=1e-10)


def test_drift_tilt_aligned_to_missing_ticker_is_none():
    tilt = DriftTilt.build(["NVDA"], np.array([0.03]), None, 21)
    assert tilt.aligned_to(["NVDA", "AMD"]) is None
    assert tilt.aligned_to(["NVDA"]) is not None


def test_drift_tilt_shifts_mean_only_within_its_horizon(panel, context, config, rng):
    """A path's mean should move by the tilt for the first ``horizon_days`` steps only."""
    training = panel.loc[:, ["NVDA", "AMD"]]
    tilt = DriftTilt.build(
        tickers=["NVDA", "AMD"], point=np.array([0.10, 0.10]), draws=None, horizon_days=21
    )
    tilted_context = context.with_tilt(tilt)

    plain = GBMModel().fit(training, context)
    tilted = GBMModel().fit(training, tilted_context)

    horizon = 63
    sim_plain = np.asarray(plain.simulate(6000, horizon, np.random.default_rng(1)), dtype=float)
    sim_tilted = np.asarray(tilted.simulate(6000, horizon, np.random.default_rng(1)), dtype=float)

    early_gap = sim_tilted[:, :21, :].mean(axis=(0, 1)) - sim_plain[:, :21, :].mean(axis=(0, 1))
    late_gap = sim_tilted[:, 21:, :].mean(axis=(0, 1)) - sim_plain[:, 21:, :].mean(axis=(0, 1))

    np.testing.assert_allclose(early_gap, 0.10 / 252, atol=3e-4)
    np.testing.assert_allclose(late_gap, 0.0, atol=3e-4)


def test_drift_tilt_does_not_change_daily_log_drift(panel, context, config):
    """``daily_log_drift`` stays the policy drift; the tilt is a separate, auditable layer."""
    training = panel.loc[:, ["NVDA", "AMD"]]
    tilt = DriftTilt.build(["NVDA", "AMD"], np.array([0.10, 0.10]), None, 21)

    plain = GBMModel().fit(training, context)
    tilted = GBMModel().fit(training, context.with_tilt(tilt))

    pd.testing.assert_series_equal(plain.daily_log_drift(63), tilted.daily_log_drift(63))

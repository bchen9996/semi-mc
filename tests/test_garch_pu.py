"""`garch_t_pu`: GJR-GARCH-t with per-path parameter draws (MODEL_NOTES section 18).

The wiring check is exact (``covariance_scale = 0`` must reproduce ``garch_t`` bit for
bit); everything else is distributional and toleranced by Monte Carlo error."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from semimc.data.returns import log_returns
from semimc.models import build_model
from semimc.models.base import FitContext
from semimc.models.garch_t import GJRGarchTModel, GJRGarchTParamUncertaintyModel


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


def test_zero_covariance_scale_reproduces_garch_t_exactly(panel, context):
    training = panel.loc[:, ["NVDA", "AMD"]]
    point = GJRGarchTModel().fit(training, context)
    pu = GJRGarchTParamUncertaintyModel(covariance_scale=0.0).fit(training, context)

    a = point.simulate(500, 30, np.random.default_rng(7))
    b = pu.simulate(500, 30, np.random.default_rng(7))

    np.testing.assert_array_equal(a, b)
    assert pu.describe()["uncertainty_applied"] == {"NVDA": False, "AMD": False}


def test_every_used_draw_is_a_valid_stationary_parameter_set(panel, context):
    pu = GJRGarchTParamUncertaintyModel().fit(panel.loc[:, ["NVDA"]], context)
    sets = pu._draw_parameter_sets("NVDA", 5000, np.random.default_rng(3))

    persistence = sets["alpha"] + sets["gamma"] / 2 + sets["beta"]
    assert (persistence < 1).all()
    assert (sets["omega"] > 0).all()
    assert (sets["alpha"] >= 0).all() and (sets["beta"] >= 0).all()
    assert (sets["alpha"] + sets["gamma"] >= 0).all()
    assert (sets["nu"] > 2.05).all()
    assert (sets["h1"] > 0).all()
    # The draws genuinely vary -- not every path fell back to the point estimate.
    assert sets["valid"].mean() > 0.5
    assert np.std(sets["beta"]) > 0


def test_with_truncation_inactive_draws_centre_on_the_point_estimate(panel, context):
    # At a tiny covariance scale no draw nears the stationarity boundary, so the draws
    # must centre on the MLE -- what catches a scaled/unscaled units slip. (At full scale
    # the retained draws are the normal approximation restricted to the stationary
    # region, which legitimately shifts them off the boundary; see MODEL_NOTES 18.)
    pu = GJRGarchTParamUncertaintyModel(covariance_scale=1e-4).fit(panel.loc[:, ["NVDA"]], context)
    sets = pu._draw_parameter_sets("NVDA", 5000, np.random.default_rng(5))
    point = pu.params["NVDA"]
    assert sets["valid"].all()
    for key in ("omega", "alpha", "gamma", "beta", "nu"):
        assert np.mean(sets[key]) == pytest.approx(point[key], rel=0.02)
    assert np.mean(sets["h1"]) == pytest.approx(pu._h0["NVDA"], rel=0.02)


def test_arithmetic_drift_is_the_policy_target(panel, context):
    """The policy fixes the *arithmetic* drift, ``E[S_T]/S_0 = exp(mu_A * T)`` (MODEL_NOTES
    section 0); each path's Ito correction is its own parameters', so this holds per
    parameter set. Checked at a short horizon, where the stochastic-variance convexity
    every GARCH shares is small; tolerance four standard errors of the sample mean."""
    pu = GJRGarchTParamUncertaintyModel().fit(panel.loc[:, ["NVDA"]], context)
    horizon, n_paths = 10, 40_000
    growth = np.exp(pu.simulate(n_paths, horizon, np.random.default_rng(11))[:, :, 0].sum(1))
    target = np.exp(float(pu.annual_arithmetic_drift["NVDA"]) * horizon / 252)
    standard_error = growth.std(ddof=1) / np.sqrt(n_paths)
    assert abs(growth.mean() - target) < 4 * standard_error


def test_multi_ticker_simulation_keeps_the_shape_contract(panel, context):
    pu = GJRGarchTParamUncertaintyModel().fit(panel.loc[:, ["NVDA", "AMD"]], context)
    out = pu.simulate(300, 15, np.random.default_rng(1))
    assert out.shape == (300, 15, 2)
    assert np.isfinite(out).all()
    info = pu.describe()
    assert set(info["last_valid_fraction"]) == {"NVDA", "AMD"}
    assert all(0 <= v <= 1 for v in info["last_valid_fraction"].values())


def test_only_gjr_garch_1_1_is_supported():
    with pytest.raises(ValueError, match="GJR-GARCH\\(1,1\\)"):
        GJRGarchTParamUncertaintyModel(p=2)


def test_build_model_wires_the_config(config):
    cfg = config.model_copy(
        update={
            "models": config.models.model_copy(
                update={
                    "garch_t_pu": config.models.garch_t_pu.model_copy(
                        update={"covariance_scale": 0.5, "max_redraws": 3}
                    )
                }
            )
        }
    )
    model = build_model("garch_t_pu", cfg)
    assert isinstance(model, GJRGarchTParamUncertaintyModel)
    assert model.covariance_scale == 0.5
    assert model.max_redraws == 3


# -- the simulated-variance cap (GJRGarchTModel, inherited by garch_t_pu) -----------------


def test_default_cap_is_twice_the_in_sample_peak_conditional_variance(panel, context):
    from semimc.models.garch_t import _fit_arch_gjr_t

    model = GJRGarchTModel().fit(panel.loc[:, ["NVDA"]], context)
    result, _ = _fit_arch_gjr_t(panel["NVDA"], 1, 1, 1, 100.0, "NVDA")
    peak = float(result.conditional_volatility.max() ** 2) / 100.0**2
    assert model._variance_cap["NVDA"] == pytest.approx(2.0 * peak)


def test_the_cap_bounds_every_simulated_step(panel, context):
    # A deliberately tight cap (a tenth of the in-sample peak) so it binds on every path:
    # the cross-path std of each day's shock can then never exceed sqrt(cap).
    model = GJRGarchTModel(variance_cap_multiple=0.1).fit(panel.loc[:, ["NVDA"]], context)
    shocks = model._simulate_zero_mean(20_000, 40, np.random.default_rng(4))[:, 1:, 0]
    cap = model._variance_cap["NVDA"]
    assert (shocks.std(axis=0) <= np.sqrt(cap) * 1.05).all()


@pytest.mark.parametrize("disabled", [None, 0.0])
def test_a_disabled_cap_matches_a_cap_too_high_to_bind(panel, context, disabled):
    off = GJRGarchTModel(variance_cap_multiple=disabled).fit(panel.loc[:, ["NVDA"]], context)
    huge = GJRGarchTModel(variance_cap_multiple=1e12).fit(panel.loc[:, ["NVDA"]], context)
    assert off._variance_cap == {}
    np.testing.assert_array_equal(
        off.simulate(400, 60, np.random.default_rng(9)),
        huge.simulate(400, 60, np.random.default_rng(9)),
    )


def test_the_cap_applies_to_garch_t_pu_paths_too(panel, context):
    pu = GJRGarchTParamUncertaintyModel(variance_cap_multiple=0.1).fit(
        panel.loc[:, ["NVDA"]], context
    )
    shocks = pu.simulate(20_000, 40, np.random.default_rng(4))[:, 1:, 0]
    cap = pu._variance_cap["NVDA"]
    # simulate() adds drift and the per-path Ito term; both are tiny next to sqrt(cap).
    assert (shocks.std(axis=0) <= np.sqrt(cap) * 1.1).all()


def test_config_zero_disables_the_cap(config):
    cfg = config.model_copy(
        update={
            "models": config.models.model_copy(
                update={
                    "garch_t": config.models.garch_t.model_copy(
                        update={"variance_cap_multiple": 0.0}
                    )
                }
            )
        }
    )
    assert build_model("garch_t", cfg).variance_cap_multiple is None
    assert build_model("garch_t", config).variance_cap_multiple == 2.0

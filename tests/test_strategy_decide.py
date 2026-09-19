"""`strategy/decide.py`: the frozen `Decision` snapshot and the one entry point
(`decide()`) a later phase's live job, the shadow book, and tests all call.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from semimc.strategy.decide import (
    Decision,
    decide,
    decide_s0,
    decide_s3,
    decide_sector_shadow,
    decide_vol_twin_shadow,
)


def _config_with_industry_series(config, tmp_path):
    """A config variant whose `signals.external_dir` points at a synthetic, multi-year
    industry-cycle series -- long enough to give `decide_sector_shadow` both a fittable
    regression (>=12 pre-as_of monthly rows) and a defined YoY reading at `as_of`."""
    periods = pd.date_range("2015-01-31", "2023-08-31", freq="ME")
    values = 100.0 + np.arange(len(periods)) * 0.5 + 5.0 * np.sin(np.arange(len(periods)) / 6.0)
    release_dates = periods + pd.Timedelta(days=45)
    frame = pd.DataFrame(
        {"period": periods, "value": values, "release_date": release_dates, "source": "test"}
    )
    frame.to_csv(tmp_path / "IPG3344S.csv", index=False)
    return config.model_copy(
        update={"signals": config.signals.model_copy(update={"external_dir": str(tmp_path)})}
    )


def test_decide_s0_is_equal_weight():
    as_of = pd.Timestamp("2024-01-31")
    decision = decide_s0(["NVDA", "AMD"], as_of)
    assert decision.sleeve == "s0"
    assert decision.as_of == as_of
    assert decision.weights.sum() == pytest.approx(1.0)
    assert decision.weights.tolist() == pytest.approx([0.5, 0.5])
    assert decision.diagnostics["kind"] == "equal_weight"


def test_decide_s3_scales_and_records_diagnostics(market, config):
    as_of = market.log_returns.index[-50]
    rng = np.random.default_rng(11)
    decision = decide_s3(market, config, ["NVDA", "AMD"], as_of, rng, n_paths=500)
    assert decision.sleeve == "s3"
    assert decision.weights.sum() <= config.paper.s3_max_leverage + 1e-9
    assert decision.diagnostics["target_vol"] == pytest.approx(config.paper.s3_target_vol)
    assert decision.diagnostics["forecast_annual_vol"] > 0
    assert decision.diagnostics["scale"] == pytest.approx(
        decision.weights.sum() / 1.0
    )  # base weights summed to 1


def test_decide_vol_twin_shadow_uses_trailing_vol_not_a_model(market, config):
    as_of = market.log_returns.index[-50]
    decision = decide_vol_twin_shadow(market, config, ["NVDA", "AMD"], as_of)
    assert decision.sleeve == "vol_twin_shadow"
    assert decision.diagnostics["kind"] == "vol_target_trailing"
    assert "trailing_annual_vol" in decision.diagnostics
    assert decision.diagnostics["trailing_annual_vol"] > 0


def test_s3_and_its_shadow_twin_generally_disagree(market, config):
    """T3 exists to compare a model forecast against trailing realized vol -- if the two
    sizing methods always produced identical scale factors, the comparison would be
    vacuous. They needn't differ by much, but they're computed from different data and
    should not be bit-identical."""
    as_of = market.log_returns.index[-50]
    rng = np.random.default_rng(3)
    s3 = decide_s3(market, config, ["NVDA", "AMD"], as_of, rng, n_paths=1000)
    twin = decide_vol_twin_shadow(market, config, ["NVDA", "AMD"], as_of)
    assert s3.diagnostics["forecast_annual_vol"] != pytest.approx(
        twin.diagnostics["trailing_annual_vol"], rel=1e-6
    )


def test_decide_sector_shadow_tilts_gross_exposure_when_industry_series_present(
    market, config, tmp_path
):
    cfg = _config_with_industry_series(config, tmp_path)
    as_of = market.log_returns.index[-50]
    decision = decide_sector_shadow(market, cfg, ["NVDA", "AMD"], as_of)
    assert decision.sleeve == "sector_shadow"
    assert decision.diagnostics["kind"] == "sector_tilt_shadow"
    assert decision.diagnostics["tilt_applied"] is True
    assert decision.diagnostics["n_observations"] >= 12
    assert 0.0 <= decision.diagnostics["gross_multiplier"] <= cfg.paper.s3_max_leverage
    assert decision.weights.sum() == pytest.approx(decision.diagnostics["gross_multiplier"])
    assert list(decision.weights.index) == ["NVDA", "AMD"]


def test_decide_sector_shadow_degrades_to_s0_when_industry_series_missing(market, config, tmp_path):
    """No `data/external/<series>.csv` on disk -- must not crash a decision job over a
    missing shadow input; it just trades nothing extra. Points `external_dir` at an empty
    tmp dir rather than trusting the fixture config's default, which (like every other
    `Config.path()`-resolved field the fixture doesn't override) resolves against the real
    repo's `project_root` and would otherwise pick up the real `data/external/IPG3344S.csv`."""
    cfg = config.model_copy(
        update={"signals": config.signals.model_copy(update={"external_dir": str(tmp_path)})}
    )
    as_of = market.log_returns.index[-50]
    decision = decide_sector_shadow(market, cfg, ["NVDA", "AMD"], as_of)
    assert decision.sleeve == "sector_shadow"
    assert decision.diagnostics["tilt_applied"] is False
    assert decision.weights.tolist() == pytest.approx([0.5, 0.5])


def test_decide_dispatches_to_the_named_sleeve(market, config, tmp_path):
    cfg = _config_with_industry_series(config, tmp_path)
    as_of = market.log_returns.index[-50]
    rng = np.random.default_rng(5)
    assert decide("s0", market, cfg, ["NVDA", "AMD"], as_of, rng).sleeve == "s0"
    assert decide("s3", market, cfg, ["NVDA", "AMD"], as_of, rng).sleeve == "s3"
    assert decide("vol_twin_shadow", market, cfg, ["NVDA", "AMD"], as_of, rng).sleeve == (
        "vol_twin_shadow"
    )
    assert decide("sector_shadow", market, cfg, ["NVDA", "AMD"], as_of, rng).sleeve == (
        "sector_shadow"
    )


def test_decide_rejects_an_unknown_sleeve(market, config):
    as_of = market.log_returns.index[-50]
    with pytest.raises(ValueError, match="unknown sleeve"):
        decide("s1", market, config, ["NVDA", "AMD"], as_of, np.random.default_rng(0))


# -- Decision.snapshot_hash -----------------------------------------------------------


def test_snapshot_hash_is_deterministic():
    as_of = pd.Timestamp("2024-01-31")
    weights = pd.Series({"NVDA": 0.6, "AMD": 0.4})
    a = Decision(as_of=as_of, sleeve="s0", weights=weights, diagnostics={"note": "a"})
    b = Decision(
        as_of=as_of, sleeve="s0", weights=weights.copy(), diagnostics={"note": "different"}
    )
    assert a.snapshot_hash == b.snapshot_hash  # diagnostics excluded on purpose


def test_snapshot_hash_changes_with_the_weights():
    as_of = pd.Timestamp("2024-01-31")
    a = Decision(as_of=as_of, sleeve="s0", weights=pd.Series({"NVDA": 0.6, "AMD": 0.4}))
    b = Decision(as_of=as_of, sleeve="s0", weights=pd.Series({"NVDA": 0.5, "AMD": 0.5}))
    assert a.snapshot_hash != b.snapshot_hash


def test_snapshot_hash_changes_with_the_sleeve_or_date():
    as_of = pd.Timestamp("2024-01-31")
    weights = pd.Series({"NVDA": 1.0})
    base = Decision(as_of=as_of, sleeve="s0", weights=weights)
    other_sleeve = Decision(as_of=as_of, sleeve="s3", weights=weights)
    other_date = Decision(as_of=pd.Timestamp("2024-02-29"), sleeve="s0", weights=weights)
    assert base.snapshot_hash != other_sleeve.snapshot_hash
    assert base.snapshot_hash != other_date.snapshot_hash


def test_snapshot_hash_is_order_independent():
    """Weight order shouldn't matter -- the hash sorts by ticker first."""
    as_of = pd.Timestamp("2024-01-31")
    a = Decision(as_of=as_of, sleeve="s0", weights=pd.Series({"NVDA": 0.6, "AMD": 0.4}))
    b = Decision(as_of=as_of, sleeve="s0", weights=pd.Series({"AMD": 0.4, "NVDA": 0.6}))
    assert a.snapshot_hash == b.snapshot_hash

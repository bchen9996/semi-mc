"""`strategy/weights.py`: the pure weight-combining primitives every sleeve builds on."""

from __future__ import annotations

import pytest

from semimc.strategy.weights import equal_weight, scale_to_target_vol


def test_equal_weight_sums_to_one_and_is_flat():
    weights = equal_weight(["NVDA", "AMD", "AVGO"])
    assert weights.sum() == pytest.approx(1.0)
    assert weights.tolist() == pytest.approx([1 / 3, 1 / 3, 1 / 3])
    assert list(weights.index) == ["NVDA", "AMD", "AVGO"]


def test_scale_to_target_vol_scales_down_when_forecast_exceeds_target():
    weights = equal_weight(["NVDA", "AMD"])
    scaled = scale_to_target_vol(weights, forecast_annual_vol=0.50, target_vol=0.25)
    assert scaled.sum() == pytest.approx(0.5)  # halved: 0.25 / 0.50


def test_scale_to_target_vol_caps_at_max_leverage_no_leverage_by_default():
    weights = equal_weight(["NVDA", "AMD"])
    # forecast vol far below target would otherwise call for >1x leverage
    scaled = scale_to_target_vol(weights, forecast_annual_vol=0.05, target_vol=0.25)
    assert scaled.sum() == pytest.approx(1.0)  # capped, not 5x


def test_scale_to_target_vol_respects_a_higher_max_leverage():
    weights = equal_weight(["NVDA", "AMD"])
    scaled = scale_to_target_vol(
        weights, forecast_annual_vol=0.05, target_vol=0.25, max_leverage=2.0
    )
    assert scaled.sum() == pytest.approx(2.0)


def test_scale_to_target_vol_goes_to_cash_on_a_non_positive_forecast():
    weights = equal_weight(["NVDA", "AMD"])
    for degenerate in (0.0, -0.1):
        scaled = scale_to_target_vol(weights, forecast_annual_vol=degenerate, target_vol=0.25)
        assert scaled.sum() == pytest.approx(0.0)

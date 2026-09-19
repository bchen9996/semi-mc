"""``analytics/paper_evaluation.py``: T2/T3's shadow replay and sleeve comparison. No
ledger or config fixture needed for the replay/bootstrap math itself -- a tiny stand-in
exposing only what each function actually reads keeps these pins fully closed-form.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from semimc.analytics.paper_evaluation import (
    compare_sleeves,
    paired_block_bootstrap,
    replay_shadow_equity_curve,
)


class _FixedRiskFree:
    def __init__(self, rate: float = 0.0) -> None:
        self.rate = rate

    def rate_at(self, as_of) -> float:  # noqa: ANN001
        return self.rate


class _FakeMarket:
    """Exposes only ``.log_returns``/``.risk_free`` -- everything this module reads."""

    def __init__(self, log_returns: pd.DataFrame, risk_free_rate: float = 0.0) -> None:
        self.log_returns = log_returns
        self.risk_free = _FixedRiskFree(risk_free_rate)


def _log_returns_from_multipliers(dates, multipliers: dict[str, list[float]]) -> pd.DataFrame:
    return pd.DataFrame({t: np.log(m) for t, m in multipliers.items()}, index=dates)


def test_replay_shadow_equity_curve_attributes_the_rebalance_day_to_the_outgoing_weights():
    dates = pd.date_range("2026-01-05", periods=4, freq="D")
    # dates[0]: unused (before any weights apply). dates[1]: A +10%. dates[2]: A +5%, B
    # +10% -- the rebalance day itself, attributed to decision 0's still-outgoing A=100%
    # weights, not decision 1's incoming B=100%. dates[3]: A -5%, B +0% -- the first day
    # decision 1's weights actually apply.
    market = _FakeMarket(
        _log_returns_from_multipliers(
            dates, {"A": [1.0, 1.10, 1.05, 0.95], "B": [1.0, 1.00, 1.10, 1.00]}
        )
    )
    decisions = [
        {"as_of": dates[0], "weights": {"A": 1.0, "B": 0.0}},
        {"as_of": dates[2], "weights": {"A": 0.0, "B": 1.0}},
    ]

    curve = replay_shadow_equity_curve(decisions, market, ["A", "B"], starting_equity=100.0)

    assert list(curve.index) == [dates[1], dates[2], dates[3]]
    expected = [100.0 * 1.10, 100.0 * 1.10 * 1.05, 100.0 * 1.10 * 1.05 * 1.00]
    assert curve.tolist() == pytest.approx(expected)


def test_replay_shadow_equity_curve_with_no_decisions_is_empty():
    market = _FakeMarket(pd.DataFrame({"A": []}))
    assert replay_shadow_equity_curve([], market, ["A"]).empty


def test_replay_shadow_equity_curve_ignores_untraded_tickers_via_zero_fill():
    dates = pd.date_range("2026-01-05", periods=2, freq="D")
    market = _FakeMarket(_log_returns_from_multipliers(dates, {"A": [1.0, 1.20]}))
    decisions = [{"as_of": dates[0], "weights": {"A": 1.0}}]  # no "B" key at all

    curve = replay_shadow_equity_curve(decisions, market, ["A"], starting_equity=100.0)
    assert curve.iloc[-1] == pytest.approx(120.0)


def test_paired_block_bootstrap_ci_excludes_zero_for_a_clear_synthetic_effect():
    rng = np.random.default_rng(0)
    n = 500
    control = pd.Series(rng.normal(0.0002, 0.01, n))
    candidate = pd.Series(control.to_numpy() + 0.01)  # unmistakable, constant +100bps/day

    result = paired_block_bootstrap(
        control, candidate, lambda f: float((f["candidate"] - f["control"]).mean()), 21, 500, rng
    )
    assert result.point_estimate == pytest.approx(0.01)
    assert result.ci_low > 0  # a +100bps/day effect is nowhere near a zero-difference CI


def test_paired_block_bootstrap_handles_no_overlap():
    control = pd.Series([1.0, 2.0], index=pd.date_range("2026-01-01", periods=2))
    candidate = pd.Series([1.0, 2.0], index=pd.date_range("2026-02-01", periods=2))
    result = paired_block_bootstrap(
        control, candidate, lambda f: 0.0, 21, 100, np.random.default_rng(0)
    )
    assert result.n_bootstrap == 0
    assert np.isnan(result.point_estimate)


def test_compare_sleeves_reports_higher_vol_for_the_more_volatile_candidate(config):
    rng = np.random.default_rng(1)
    dates = pd.date_range("2026-01-05", periods=300, freq="B")
    market = _FakeMarket(pd.DataFrame(index=dates))  # compare_sleeves never reads returns
    control_returns = rng.normal(0.0003, 0.005, len(dates))
    candidate_returns = rng.normal(0.0003, 0.02, len(dates))  # 4x the daily vol, same drift
    control_equity = pd.Series(100.0 * np.cumprod(1 + control_returns), index=dates)
    candidate_equity = pd.Series(100.0 * np.cumprod(1 + candidate_returns), index=dates)

    result = compare_sleeves(
        control_equity, "control", candidate_equity, "candidate", market, config, rng
    )

    assert result.candidate.annualized_vol > result.control.annualized_vol
    assert result.annualized_vol_difference.point_estimate > 0
    assert result.annualized_vol_difference.ci_low > 0
    assert result.n_overlapping_days == len(dates) - 1  # one lost to pct_change()

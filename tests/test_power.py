"""`analytics/power.py`: how much more data a signal-search result would need, and how
much a correlated cross-section is actually worth. Pure functions, closed-form checks.
"""

from __future__ import annotations

import pytest

from semimc.analytics.power import effective_independent_units, required_sample_size


def test_required_sample_size_pins_the_relative_strength_63d_example():
    """research_log.md, 2026-09-15: relative_strength @ 63d, rank_ic_t=2.034, n=127
    monthly dates. To reach the addendum's own "~3" skepticism bar takes ~276 dates."""
    needed = required_sample_size(observed_t=2.034, observed_n=127, target_t=3.0)
    assert needed == pytest.approx(127 * (3.0 / 2.034) ** 2)
    assert needed == pytest.approx(276.3, abs=0.5)


def test_required_sample_size_scales_with_the_square_of_the_target_ratio():
    assert required_sample_size(2.0, 100, 4.0) == pytest.approx(400.0)
    assert required_sample_size(2.0, 100, 2.0) == pytest.approx(100.0)  # already there


def test_required_sample_size_is_infinite_for_a_non_positive_observed_effect():
    assert required_sample_size(0.0, 100, 3.0) == float("inf")
    assert required_sample_size(-1.5, 100, 3.0) == float("inf")
    assert required_sample_size(2.0, 0, 3.0) == float("inf")


def test_effective_independent_units_pins_the_semis_universe_example():
    """14 tickers, average pairwise correlation 0.58 (computed from real cached prices) ->
    only ~1.64 effectively independent names."""
    assert effective_independent_units(14, 0.58) == pytest.approx(1.64, abs=0.01)


def test_effective_independent_units_zero_correlation_is_fully_independent():
    assert effective_independent_units(14, 0.0) == pytest.approx(14.0)


def test_effective_independent_units_perfect_correlation_collapses_to_one():
    assert effective_independent_units(14, 1.0) == pytest.approx(1.0)


def test_effective_independent_units_more_correlated_names_help_less():
    """Confirms the finding that drove the design choice: growing a highly correlated
    universe from 14 to 40 names barely moves the effective count, while the same growth
    at a lower correlation helps far more."""
    same_correlation = effective_independent_units(40, 0.58)
    lower_correlation = effective_independent_units(40, 0.30)
    assert same_correlation == pytest.approx(1.69, abs=0.02)
    assert lower_correlation > same_correlation
    assert lower_correlation == pytest.approx(3.15, abs=0.02)


def test_effective_independent_units_rejects_correlation_outside_valid_range():
    with pytest.raises(ValueError, match="avg_pairwise_correlation"):
        effective_independent_units(14, 1.5)
    with pytest.raises(ValueError, match="avg_pairwise_correlation"):
        effective_independent_units(14, -1.5)

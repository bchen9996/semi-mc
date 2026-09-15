"""Data layer: returns, cache, validation, earnings mapping, risk-free lookups."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from semimc.data.earnings import (
    abnormal_earnings_returns,
    load_calendar,
    strip_earnings_returns,
)
from semimc.data.provider import CSVProvider, ProviderError
from semimc.data.returns import (
    common_calendar,
    estimate_beta,
    ewma_volatility,
    log_returns,
    residualize,
    simple_returns,
    summary_statistics,
)
from semimc.data.riskfree import RiskFreeCurve, load_risk_free_series
from semimc.data.validation import validate_prices

# ---------------------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------------------


def test_log_returns_are_additive_across_time(prices):
    """Summing log returns must reproduce the total move exactly."""
    series = prices["NVDA"].dropna()
    total = float(np.log(series.iloc[-1] / series.iloc[0]))
    assert float(log_returns(series).sum()) == pytest.approx(total, rel=1e-12)


def test_simple_and_log_returns_are_consistent(prices):
    simple = simple_returns(prices["NVDA"])
    log = log_returns(prices["NVDA"])
    assert np.allclose(np.log1p(simple.to_numpy()), log.to_numpy())


def test_a_ten_for_one_split_shows_up_as_a_crash_when_unadjusted(fixtures_dir):
    """The reason adjusted prices are mandatory, demonstrated on the broken fixture.

    BROKEN.csv divides every price from row 1200 by ten, exactly as an unadjusted series
    would after a 10-for-1 split. The result is a single -90% day that never happened.
    """
    broken = pd.read_csv(fixtures_dir / "BROKEN.csv", parse_dates=["date"]).set_index("date")
    clean = pd.read_csv(fixtures_dir / "NVDA.csv", parse_dates=["date"]).set_index("date")

    worst_broken = float(log_returns(broken["adj_close"]).min())
    worst_clean = float(log_returns(clean["adj_close"]).min())

    assert np.expm1(worst_broken) < -0.85, "the unadjusted split should read as a ~90% crash"
    assert np.expm1(worst_clean) > -0.5, "the adjusted series should have no such day"


def test_common_calendar_reports_what_it_dropped(prices):
    """Alignment must be explicit about the observations it removes."""
    modified = prices.copy()
    modified.loc[modified.index[:500], "AMD"] = np.nan

    aligned, dropped = common_calendar(modified, ["NVDA", "AMD"], min_observations=100)
    assert len(aligned) == len(prices) - 500
    assert dropped["NVDA"] == 500, "NVDA lost 500 rows to AMD's short history"


def test_common_calendar_excludes_a_ticker_that_is_too_short(prices):
    """A very short history must be excluded rather than truncating everyone else."""
    modified = prices.copy()
    modified.loc[modified.index[:-30], "AMD"] = np.nan
    aligned, dropped = common_calendar(modified, ["NVDA", "AMD"], min_observations=100)
    assert "AMD" not in aligned.columns
    assert len(aligned) == len(prices)


def test_beta_estimation_recovers_a_known_loading(rng):
    """A series built as 1.5x a factor must come back with a beta near 1.5."""
    factor = pd.Series(rng.normal(0, 0.01, 2000))
    asset = 1.5 * factor + pd.Series(rng.normal(0, 0.005, 2000))
    assert estimate_beta(asset, factor) == pytest.approx(1.5, abs=0.05)


def test_beta_returns_nan_on_too_little_overlap():
    short = pd.Series(np.arange(10, dtype=float))
    assert np.isnan(estimate_beta(short, short))


def test_residualize_removes_the_factor(prices):
    """The sector factor must end up uncorrelated with the market factor."""
    returns = log_returns(prices)
    residual = residualize(returns["SOXX"], returns["SPY"])
    assert abs(estimate_beta(residual, returns["SPY"])) < 1e-9


def test_ewma_volatility_reacts_faster_than_a_long_window(rng):
    """After a volatility spike, EWMA should read higher than a one-year realized window."""
    calm = rng.normal(0, 0.005, 240)
    storm = rng.normal(0, 0.05, 12)
    series = pd.Series(np.concatenate([calm, storm]))

    ewma = ewma_volatility(series, lam=0.94)
    realized = float(series.std(ddof=1) * np.sqrt(252))
    assert ewma > realized


def test_summary_statistics_reports_per_ticker_histories(prices):
    """Each ticker is described on its own observations, not a padded common window."""
    returns = log_returns(prices)
    stats = summary_statistics(returns, market=returns["SPY"], sector=returns["SOXX"])
    assert set(stats.index) == set(returns.columns)
    assert (stats["ann_vol"] > 0).all()
    assert stats.loc["NVDA", "excess_kurtosis"] > 0, "fixture uses t innovations, so fat-tailed"


# ---------------------------------------------------------------------------------------
# Provider and cache
# ---------------------------------------------------------------------------------------


def test_csv_provider_returns_the_documented_frame(fixture_provider):
    frame = fixture_provider.fetch(["NVDA", "AMD"], start=pd.Timestamp("2016-01-01").date())
    assert list(frame.columns) == ["date", "ticker", "adj_close", "volume"]
    assert set(frame["ticker"]) == {"NVDA", "AMD"}
    assert frame["adj_close"].gt(0).all()
    assert frame["date"].is_monotonic_increasing is False  # sorted by ticker, then date


def test_csv_provider_raises_when_nothing_is_found(tmp_path):
    with pytest.raises(ProviderError):
        CSVProvider(tmp_path).fetch(["NOPE"], start=pd.Timestamp("2020-01-01").date())


def test_missing_ticker_is_reported_not_invented(fixture_provider):
    """A symbol with no file is simply absent from the result."""
    frame = fixture_provider.fetch(["NVDA", "DELISTED"], start=pd.Timestamp("2016-01-01").date())
    assert set(frame["ticker"]) == {"NVDA"}


def test_cache_roundtrip_and_staleness(populated_cache, fixture_provider):
    """A fresh cache must not refetch; the wide frame must match what went in."""
    wide = populated_cache.prices(["NVDA", "AMD"])
    assert list(wide.columns) == ["NVDA", "AMD"]
    assert len(wide) == 2000

    statuses = populated_cache.refresh(
        fixture_provider, ["NVDA"], start=pd.Timestamp("2000-01-01").date(), max_staleness_days=7
    )
    assert statuses[0].action == "cached"

    forced = populated_cache.refresh(
        fixture_provider, ["NVDA"], start=pd.Timestamp("2000-01-01").date(), force=True
    )
    assert forced[0].action == "fetched"


def test_cache_coverage_lists_every_ticker(populated_cache):
    coverage = populated_cache.coverage()
    assert set(coverage["ticker"]) == {"NVDA", "AMD", "SPY", "SOXX"}
    assert (coverage["rows"] == 2000).all()


# ---------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------


def test_validation_catches_injected_defects(fixtures_dir):
    """The broken fixture's gap, stale run, zero volume and split must all be flagged."""
    broken = pd.read_csv(fixtures_dir / "BROKEN.csv", parse_dates=["date"]).set_index("date")
    prices = broken[["adj_close"]].rename(columns={"adj_close": "NVDA"})
    volumes = broken[["volume"]].rename(columns={"volume": "NVDA"})

    report = validate_prices(
        prices, ["NVDA"], volumes=volumes, min_observations=100, max_gap_days=5, outlier_sigma=8.0
    )
    kinds = set(report.to_frame()["kind"])

    assert "gap" in kinds, "the 12-day hole should be reported"
    assert "stale_price" in kinds, "three identical closes should be reported"
    assert "zero_volume" in kinds, "a zero-volume day should be reported"
    assert "outlier" in kinds, "the unadjusted split should be reported as an outlier"


def test_validation_reports_a_missing_ticker_as_an_error(prices):
    report = validate_prices(prices, ["NVDA", "GONE"], min_observations=100)
    errors = [i for i in report.errors if i.ticker == "GONE"]
    assert errors and errors[0].kind == "missing"
    assert not report.ok


def test_validation_flags_a_short_history(prices):
    """ARM's situation: a real ticker with too little history for a full fit window."""
    short = prices.iloc[-200:].copy()
    report = validate_prices(short, ["NVDA"], min_observations=750)
    assert any(i.kind == "short_history" for i in report.issues)


def test_clean_data_produces_no_errors(prices, volumes):
    report = validate_prices(prices, list(prices.columns), volumes=volumes, min_observations=100)
    assert report.ok, [i.detail for i in report.errors]


def test_validation_never_modifies_prices(prices):
    """The report only reports; the caller decides what to do."""
    before = prices.copy()
    validate_prices(prices, list(prices.columns), min_observations=100)
    pd.testing.assert_frame_equal(prices, before)


# ---------------------------------------------------------------------------------------
# Earnings
# ---------------------------------------------------------------------------------------


def test_after_close_reports_move_the_next_session():
    """AMC on Monday is priced Tuesday; BMO on Monday is priced Monday."""
    sessions = pd.DatetimeIndex(pd.bdate_range("2024-01-01", periods=10))
    calendar = load_calendar_from_rows(
        [
            {"ticker": "AAA", "date": sessions[3], "timing": "amc"},
            {"ticker": "BBB", "date": sessions[3], "timing": "bmo"},
        ]
    )
    assert calendar.reaction_days("AAA", sessions)[0] == sessions[4]
    assert calendar.reaction_days("BBB", sessions)[0] == sessions[3]


def load_calendar_from_rows(rows):
    from semimc.data.earnings import EarningsCalendar

    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"])
    return EarningsCalendar(frame)


def test_unknown_timing_is_treated_as_after_close():
    sessions = pd.DatetimeIndex(pd.bdate_range("2024-01-01", periods=10))
    calendar = load_calendar_from_rows(
        [{"ticker": "AAA", "date": sessions[2], "timing": "unknown"}]
    )
    assert calendar.reaction_days("AAA", sessions)[0] == sessions[3]


def test_abnormal_return_removes_the_market_component():
    """A day where the stock moved exactly beta times the market has zero abnormal return."""
    dates = pd.DatetimeIndex(pd.bdate_range("2024-01-01", periods=5))
    benchmark = pd.Series([0.01, -0.02, 0.005, 0.0, 0.03], index=dates)
    asset = 2.0 * benchmark
    abnormal = abnormal_earnings_returns(asset, benchmark, dates[[1, 4]], beta=2.0)
    assert np.allclose(abnormal.to_numpy(), 0.0, atol=1e-12)


def test_stripping_replaces_earnings_days_with_the_systematic_part():
    dates = pd.DatetimeIndex(pd.bdate_range("2024-01-01", periods=5))
    benchmark = pd.Series([0.01, -0.02, 0.005, 0.0, 0.03], index=dates)
    asset = pd.Series([0.02, 0.25, 0.01, 0.0, 0.06], index=dates)  # day 1 is an earnings pop

    stripped, n = strip_earnings_returns(asset, benchmark, dates[[1]], beta=2.0)
    assert n == 1
    assert stripped.iloc[1] == pytest.approx(2.0 * -0.02)
    assert stripped.iloc[0] == asset.iloc[0], "non-earnings days must be untouched"


def test_calendar_truncation_is_point_in_time(fixtures_dir):
    calendar = load_calendar(fixtures_dir / "earnings_calendar.csv")
    cutoff = pd.Timestamp("2020-01-01")
    assert (calendar.truncated(cutoff).events["date"] <= cutoff).all()


def test_missing_calendar_yields_an_empty_one(tmp_path):
    assert load_calendar(tmp_path / "nope.csv").is_empty()


def test_future_reaction_offsets_land_inside_the_horizon(fixtures_dir):
    calendar = load_calendar(fixtures_dir / "earnings_calendar.csv")
    offsets = calendar.future_reaction_offsets("NVDA", pd.Timestamp("2019-01-02"), horizon=126)
    assert offsets, "a quarterly reporter should have reports in a 126-day window"
    assert all(0 <= o < 126 for o in offsets)
    assert len(offsets) <= 3, "at most two or three reports fit in six months"


# ---------------------------------------------------------------------------------------
# Risk-free curve
# ---------------------------------------------------------------------------------------


def test_risk_free_lookup_is_backward_looking(fixtures_dir):
    series = load_risk_free_series(fixtures_dir / "risk_free_3m.csv")
    curve = RiskFreeCurve(series, fallback=0.042)

    stamp = series.index[500]
    assert curve.rate_at(stamp) == pytest.approx(float(series.iloc[500]))
    # A date between observations must use the previous one, never the next.
    between = stamp + pd.Timedelta(days=1)
    assert curve.rate_at(between) == pytest.approx(float(series.loc[:between].iloc[-1]))


def test_risk_free_before_the_series_falls_back(fixtures_dir):
    series = load_risk_free_series(fixtures_dir / "risk_free_3m.csv")
    curve = RiskFreeCurve(series, fallback=0.037)
    assert curve.rate_at(pd.Timestamp("1990-01-01")) == pytest.approx(0.037)


def test_risk_free_percent_and_decimal_are_both_accepted(tmp_path):
    """4.2 and 0.042 must both come back as 0.042."""
    percent = tmp_path / "percent.csv"
    percent.write_text("date,rate\n2020-01-01,4.2\n2020-01-02,4.3\n", encoding="utf-8")
    decimal = tmp_path / "decimal.csv"
    decimal.write_text("date,rate\n2020-01-01,0.042\n2020-01-02,0.043\n", encoding="utf-8")

    assert float(load_risk_free_series(percent).iloc[0]) == pytest.approx(0.042)
    assert float(load_risk_free_series(decimal).iloc[0]) == pytest.approx(0.042)


def test_risk_free_truncation_removes_future_rates(fixtures_dir):
    series = load_risk_free_series(fixtures_dir / "risk_free_3m.csv")
    curve = RiskFreeCurve(series, 0.042).truncated(pd.Timestamp("2018-01-01"))
    assert curve.series.index.max() <= pd.Timestamp("2018-01-01")

"""The forward forecast journal (`journal.py`) and its two quantile-grid scoring helpers
(`analytics/scoring.py`). Recording runs against a fixture market truncated at an
earlier date, so the full fixture market can then score it -- the same "record now,
outcome later" sequence a real journal goes through, compressed into one test."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from semimc.analytics.scoring import crps_from_quantiles, crps_normal, pit_from_quantiles
from semimc.backtest.walkforward import _realized_outcome
from semimc.cli import app
from semimc.config import load_config
from semimc.data import MarketData
from semimc.journal import (
    JOURNAL_COLUMNS,
    QUANTILE_COLUMNS,
    QUANTILE_LEVELS,
    append_journal,
    is_last_weekday_of_month,
    next_open_after,
    read_journal,
    record_forecasts,
    render_score_markdown,
    score_journal,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

VARIANTS = [
    {"model": "gbm", "drift_policy": "shrunk_historical"},
    {"model": "gbm", "drift_policy": "risk_free"},
]


@pytest.fixture
def journal_config(config, tmp_path):
    overrides = {
        "data.provider": "csv",
        "data.csv_dir": str(FIXTURES),
        "data.cache_dir": str(tmp_path / "cache"),
        "data.earnings_calendar": str(FIXTURES / "earnings_calendar.csv"),
        "backtest.risk_free_series": str(FIXTURES / "risk_free_3m.csv"),
        "universe.groups": {"fixture": ["NVDA", "AMD"]},
        "fit.min_observations": 300,
        "fit.window_years": 4,
        "horizons": [5, 21],
        "journal.variants": VARIANTS,
        "journal.baseline": VARIANTS[0],
        "journal.n_paths": 400,
        "journal.path": str(tmp_path / "journal" / "forecasts.csv"),
    }
    return load_config(overrides=overrides)


def _truncated(market: MarketData, cut: pd.Timestamp, config) -> MarketData:
    return MarketData(
        prices=market.prices.loc[:cut],
        volumes=market.volumes.loc[:cut],
        config=config,
        risk_free=market.risk_free,
        earnings=market.earnings,
        ohlc={k: v.loc[:cut] for k, v in market.ohlc.items()},
    )


@pytest.fixture
def cut(market) -> pd.Timestamp:
    # 10 trading days before the end: the 5-day horizon resolves, the 21-day one doesn't.
    return market.prices.index[-11]


def _record(market, journal_config, cut, **kwargs):
    early = _truncated(market, cut, journal_config)
    recorded_at = dt.datetime.combine(cut.date(), dt.time(21, 0), tzinfo=dt.UTC)
    return record_forecasts(early, journal_config, recorded_at=recorded_at, **kwargs)


# -- quantile-grid scoring --------------------------------------------------------------


def test_crps_from_quantiles_converges_to_the_normal_closed_form_on_a_fine_grid():
    # CRPS(N(mu, s), y) = s * [z(2 Phi(z) - 1) + 2 phi(z) - 1/sqrt(pi)], z = (y - mu)/s.
    mu, sigma, y = 0.01, 0.08, 0.05
    levels = np.arange(1, 10_000) / 10_000
    quantiles = stats.norm.ppf(levels, loc=mu, scale=sigma)
    exact = crps_normal(mu, sigma, y)
    assert crps_from_quantiles(quantiles, levels, y) == pytest.approx(exact, rel=1e-3)


def test_crps_from_quantiles_on_the_journal_grid_is_close_to_the_closed_form():
    # The 1%..99% grid leaves the outer tails unresolved: a small error, either sign.
    mu, sigma = 0.0, 0.1
    quantiles = stats.norm.ppf(QUANTILE_LEVELS, loc=mu, scale=sigma)
    for y in (-0.15, 0.0, 0.12):
        exact = crps_normal(mu, sigma, y)
        assert crps_from_quantiles(quantiles, QUANTILE_LEVELS, y) == pytest.approx(exact, rel=0.03)


def test_pit_from_quantiles_inverts_the_cdf_and_splits_the_unresolved_tails():
    quantiles = QUANTILE_LEVELS.copy()  # uniform(0, 1): the quantile equals the level
    assert pit_from_quantiles(quantiles, QUANTILE_LEVELS, 0.375) == pytest.approx(0.375)
    assert pit_from_quantiles(quantiles, QUANTILE_LEVELS, -1.0) == pytest.approx(0.005)
    assert pit_from_quantiles(quantiles, QUANTILE_LEVELS, 2.0) == pytest.approx(0.995)


# -- recording ----------------------------------------------------------------------------


def test_record_writes_one_row_per_ticker_variant_and_horizon_at_the_latest_date(
    market, journal_config, cut
):
    result = _record(market, journal_config, cut, git_commit="abc123")

    assert result.skipped_reason is None
    assert result.failures == []
    assert result.origin == cut
    assert len(result.rows) == 2 * len(VARIANTS) * 2  # tickers x variants x horizons
    assert set(result.rows["ticker"]) == {"NVDA", "AMD"}
    assert set(result.rows["horizon"]) == {5, 21}
    assert (result.rows["git_commit"] == "abc123").all()
    quantiles = result.rows[QUANTILE_COLUMNS].to_numpy(dtype=float)
    assert (np.diff(quantiles, axis=1) >= 0).all()


def test_each_variant_gets_its_own_drift_policy(market, journal_config, cut):
    rows = _record(market, journal_config, cut).rows
    one = rows[(rows["ticker"] == "NVDA") & (rows["horizon"] == 21)].set_index("drift_policy")
    # Same model, same shocks up to the stream split -- the medians differ because the
    # drift does. Equal medians would mean the variant's policy never reached the model.
    assert one.loc["shrunk_historical", "q50"] != pytest.approx(one.loc["risk_free", "q50"])


def test_record_is_reproducible_from_its_seed(market, journal_config, cut):
    first = _record(market, journal_config, cut).rows
    second = _record(market, journal_config, cut).rows
    pd.testing.assert_frame_equal(first[QUANTILE_COLUMNS], second[QUANTILE_COLUMNS])


def test_record_refuses_once_the_next_session_could_have_opened(market, journal_config, cut):
    early = _truncated(market, cut, journal_config)
    late = next_open_after(cut) + dt.timedelta(minutes=1)
    result = record_forecasts(early, journal_config, recorded_at=late)
    assert result.rows.empty
    assert "outcome window" in result.skipped_reason


def test_next_open_after_is_the_following_weekdays_new_york_open():
    # Friday 2026-09-25 -> Monday 09:30 EDT = 13:30 UTC; a Sunday run is still in time.
    friday = pd.Timestamp("2026-09-25")
    assert next_open_after(friday) == dt.datetime(2026, 9, 28, 13, 30, tzinfo=dt.UTC)
    # After the 2026-11-01 DST change: 09:30 EST = 14:30 UTC.
    assert next_open_after(pd.Timestamp("2026-11-02")) == dt.datetime(
        2026, 11, 3, 14, 30, tzinfo=dt.UTC
    )


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        ("2026-09-30", True),  # a Wednesday month end
        ("2026-09-25", False),
        ("2026-10-30", True),  # Friday; the 31st is a Saturday
        ("2027-05-28", False),  # Friday before Memorial Day Monday the 31st: skipped month
    ],
)
def test_is_last_weekday_of_month(day, expected):
    assert is_last_weekday_of_month(pd.Timestamp(day)) is expected


def test_month_end_only_skips_a_mid_month_origin(market, journal_config):
    mid = market.prices.index[(market.prices.index.day > 5) & (market.prices.index.day < 25)][-1]
    result = _record(market, journal_config, mid, month_end_only=True)
    assert result.rows.empty
    assert "not the last weekday" in result.skipped_reason


def test_month_end_only_records_the_last_weekday_of_a_month(market, journal_config):
    index = market.prices.index
    month_ends = index[[is_last_weekday_of_month(d) for d in index]]
    result = _record(market, journal_config, month_ends[-1], month_end_only=True)
    assert result.skipped_reason is None
    assert not result.rows.empty


def test_record_skips_an_origin_already_in_the_journal(market, journal_config, cut):
    first = _record(market, journal_config, cut)
    again = _record(market, journal_config, cut, existing=first.rows)
    assert again.rows.empty
    assert "already in the journal" in again.skipped_reason


def test_append_journal_never_rewrites_existing_lines(market, journal_config, cut, tmp_path):
    path = tmp_path / "forecasts.csv"
    rows = _record(market, journal_config, cut).rows
    append_journal(path, rows.iloc[:3])
    before = path.read_text(encoding="utf-8")
    append_journal(path, rows.iloc[3:])
    after = path.read_text(encoding="utf-8")

    assert after.startswith(before)
    assert after.count("recorded_at,") == 1  # one header
    back = read_journal(path)
    assert list(back.columns) == JOURNAL_COLUMNS
    assert len(back) == len(rows)


# -- scoring ------------------------------------------------------------------------------


def test_score_resolves_elapsed_horizons_and_leaves_the_rest_pending(market, journal_config, cut):
    rows = _record(market, journal_config, cut).rows

    result = score_journal(rows, market, journal_config)

    assert set(result.scored["horizon"]) == {5}
    assert result.n_pending == len(rows[rows["horizon"] == 21])
    assert result.scored["pit"].between(0, 1).all()
    assert {"hit_50", "hit_90"} <= set(result.scored.columns)


def test_score_uses_the_backtesters_outcome_from_current_prices(market, journal_config, cut):
    rows = _record(market, journal_config, cut).rows
    scored = score_journal(rows, market, journal_config).scored
    row = scored[scored["ticker"] == "NVDA"].iloc[0]
    log_return, _, _ = _realized_outcome(market.prices["NVDA"], cut, 5)
    assert row["realized_simple_return"] == pytest.approx(np.expm1(log_return))


def test_the_baseline_has_zero_skill_against_itself(market, journal_config, cut):
    rows = _record(market, journal_config, cut).rows
    summary = score_journal(rows, market, journal_config).summary
    baseline = summary[summary["drift_policy"] == "shrunk_historical"].iloc[0]
    assert baseline["crps_skill_vs_baseline"] == pytest.approx(0.0)
    assert baseline["n_origins"] == 1
    assert baseline["n_independent_origins"] == 1


def test_render_reports_pending_forecasts_and_withholds_significance_tests(
    market, journal_config, cut
):
    rows = _record(market, journal_config, cut).rows
    text = render_score_markdown(score_journal(rows, market, journal_config))
    assert "Pending (horizon not reached yet): 4" in text  # 2 tickers x 2 variants x 21d
    assert "| 5d | gbm/risk_free |" in text
    assert "No significance tests" in text


def test_render_with_nothing_resolved_says_so(market, journal_config):
    rows = _record(market, journal_config, market.prices.index[-1]).rows
    text = render_score_markdown(score_journal(rows, market, journal_config))
    assert "Nothing to score yet" in text


def test_a_coverage_level_off_the_quantile_grid_is_rejected(market, journal_config, cut):
    rows = _record(market, journal_config, cut).rows
    odd = journal_config.model_copy(
        update={"backtest": journal_config.backtest.model_copy(update={"coverage_levels": [0.555]})}
    )
    with pytest.raises(ValueError, match="quantile grid"):
        score_journal(rows, market, odd)


def test_config_rejects_a_baseline_outside_the_variants():
    with pytest.raises(ValueError, match="journal.baseline"):
        load_config(
            overrides={
                "journal.variants": VARIANTS,
                "journal.baseline": {"model": "garch_t", "drift_policy": "capm"},
            }
        )


# -- CLI ----------------------------------------------------------------------------------


def _journal_overrides(tmp_path) -> list[str]:
    fixtures = FIXTURES.as_posix()
    return [
        "--set",
        "data.provider=csv",
        "--set",
        f"data.csv_dir={fixtures}",
        "--set",
        f"data.cache_dir={(tmp_path / 'cache').as_posix()}",
        "--set",
        f"data.earnings_calendar={fixtures}/earnings_calendar.csv",
        "--set",
        f"backtest.risk_free_series={fixtures}/risk_free_3m.csv",
        "--set",
        "universe.groups={fixture: [NVDA, AMD]}",
        "--set",
        "fit.min_observations=300",
        "--set",
        "fit.window_years=4",
        "--set",
        "horizons=[5, 21]",
        "--set",
        "journal.variants=[{model: gbm, drift_policy: shrunk_historical}]",
        "--set",
        "journal.baseline={model: gbm, drift_policy: shrunk_historical}",
        "--set",
        "journal.n_paths=300",
        "--set",
        f"journal.path={(tmp_path / 'journal' / 'forecasts.csv').as_posix()}",
        "--set",
        f"journal.scores_dir={(tmp_path / 'journal' / 'scores').as_posix()}",
    ]


def test_cli_record_then_score_then_record_again_is_skipped(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    # The fixtures end years ago; the record-window guard has its own unit tests above.
    monkeypatch.setattr(
        "semimc.journal.next_open_after",
        lambda origin: dt.datetime(2100, 1, 1, tzinfo=dt.UTC),
    )

    runner = CliRunner()
    fetched = runner.invoke(app, ["fetch"] + _journal_overrides(tmp_path))
    assert fetched.exit_code == 0, fetched.output

    first = runner.invoke(app, ["journal", "record"] + _journal_overrides(tmp_path))
    assert first.exit_code == 0, first.output
    assert "Recorded 4 forecasts" in first.output  # 2 tickers x 1 variant x 2 horizons

    again = runner.invoke(app, ["journal", "record"] + _journal_overrides(tmp_path))
    assert again.exit_code == 0, again.output
    assert "already in the journal" in again.output

    scored = runner.invoke(app, ["journal", "score"] + _journal_overrides(tmp_path))
    assert scored.exit_code == 0, scored.output
    assert "4 still pending" in scored.output
    [report] = list((tmp_path / "journal" / "scores").glob("*.md"))
    assert "Nothing to score yet" in report.read_text(encoding="utf-8")

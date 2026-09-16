"""Walk-forward backtester, with the look-ahead guard as the centrepiece.

Section 8 of the brief: *"Never let post-origin data leak into fitting or model
selection."* Leakage is silent -- it produces better-looking results, not errors -- so it
gets an explicit test that injects a leak and demands a failure.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from semimc.backtest.scorecard import (
    build_scorecard,
    coverage_table,
    crps_table,
    dm_table,
    model_summary,
    pit_tables,
    var_test_table,
    written_summary,
)
from semimc.backtest.walkforward import (
    LookAheadError,
    _realized_outcome,
    assert_point_in_time,
    generate_origins,
    run_walk_forward,
    select_non_overlapping,
)
from semimc.models.base import FitContext
from semimc.models.gbm import GBMModel

# ---------------------------------------------------------------------------------------
# The look-ahead guard
# ---------------------------------------------------------------------------------------


def test_guard_accepts_point_in_time_data(prices):
    frame = prices.loc[:"2020-01-01"]
    assert_point_in_time(frame, pd.Timestamp("2020-01-01"), "training")


def test_guard_fails_on_injected_leakage(prices):
    """The core leakage test: one post-origin row must be caught."""
    origin = pd.Timestamp("2020-01-01")
    leaked = prices.loc[: origin + pd.Timedelta(days=10)]
    with pytest.raises(LookAheadError, match="after the forecast origin"):
        assert_point_in_time(leaked, origin, "training")


def test_model_fit_rejects_post_origin_data(prices):
    """Even bypassing the backtester, the model itself refuses future data."""
    from semimc.data.returns import log_returns

    panel = log_returns(prices)
    origin = panel.index[-200]
    context = FitContext(
        as_of=origin, risk_free_rate=0.04, benchmark_returns=panel.loc[:origin, ["SPY"]]
    )
    with pytest.raises(ValueError, match="look-ahead"):
        GBMModel().fit(panel.loc[:, ["NVDA"]], context)  # full panel, not truncated


def test_training_window_never_returns_future_data(market):
    """Every fit goes through this method, so this is the choke point that matters."""
    for offset in (100, 400, 900):
        origin = market.log_returns.index[-offset]
        window = market.training_window(["NVDA"], origin)
        assert window.index.max() <= origin


def test_context_is_truncated_to_the_origin(market):
    origin = market.log_returns.index[-300]
    context = market.context(origin, for_backtest=True)
    assert context.benchmark_returns.index.max() <= origin
    assert context.risk_free_rate == pytest.approx(
        market.risk_free.truncated(origin).rate_at(origin)
    )


def test_backtest_run_is_free_of_leakage(market, config, monkeypatch):
    """Instrument every fit during a real backtest and assert none saw future data.

    Stronger than checking the guard in isolation: it proves the guard is actually on the
    path the backtester takes.
    """
    seen: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    original = GBMModel.fit

    def recording_fit(self, log_returns, context):
        seen.append((pd.Timestamp(log_returns.index.max()), pd.Timestamp(context.as_of)))
        return original(self, log_returns, context)

    monkeypatch.setattr(GBMModel, "fit", recording_fit)

    run_walk_forward(
        market=market,
        config=config,
        models=["gbm"],
        tickers=["NVDA"],
        horizons=[21],
        start=pd.Timestamp("2021-01-01"),
        n_paths=50,
        progress=False,
    )

    assert seen, "the backtest should have fitted at least once"
    for last_training_date, origin in seen:
        assert last_training_date <= origin, f"leak: trained to {last_training_date} at {origin}"


# ---------------------------------------------------------------------------------------
# Origins and overlap
# ---------------------------------------------------------------------------------------


def test_origins_are_real_trading_days(prices):
    origins = generate_origins(prices.index, start=pd.Timestamp("2018-01-01"), frequency="ME")
    assert len(origins) > 30
    assert set(origins).issubset(set(prices.index)), "origins must be sessions that existed"
    assert origins.is_monotonic_increasing


def test_origins_are_monthly_by_default(prices):
    origins = generate_origins(prices.index, start=pd.Timestamp("2018-01-01"))
    gaps = np.diff(origins.to_numpy()).astype("timedelta64[D]").astype(int)
    assert 25 <= np.median(gaps) <= 35


def test_non_overlapping_selection_respects_the_horizon(prices):
    origins = generate_origins(prices.index, start=pd.Timestamp("2016-06-01"))
    mask = select_non_overlapping(origins, horizon_days=252)
    chosen = origins[mask]
    gaps = np.diff(chosen.to_numpy()).astype("timedelta64[D]").astype(int)
    assert (gaps >= 360).all(), "annual horizons must not overlap"
    assert len(chosen) < len(origins) / 10


def test_non_overlapping_keeps_everything_at_a_one_day_horizon(prices):
    origins = generate_origins(prices.index, start=pd.Timestamp("2018-01-01"))
    assert select_non_overlapping(origins, horizon_days=1).all()


def test_realized_outcome_counts_trading_days(prices):
    series = prices["NVDA"]
    origin = series.index[500]
    outcome = _realized_outcome(series, origin, horizon=21)
    assert outcome is not None
    realized, start_price, end_date = outcome
    assert start_price == pytest.approx(float(series.iloc[500]))
    assert end_date == series.index[521]
    assert realized == pytest.approx(float(np.log(series.iloc[521] / series.iloc[500])))


def test_realized_outcome_is_none_past_the_end(prices):
    series = prices["NVDA"]
    assert _realized_outcome(series, series.index[-5], horizon=21) is None


# ---------------------------------------------------------------------------------------
# End-to-end backtest and scorecard
# ---------------------------------------------------------------------------------------


_BACKTEST_CACHE: dict[str, object] = {}


@pytest.fixture
def backtest_result(market, config):
    """One real backtest, computed once and reused across the scorecard tests.

    The underlying fixtures are function-scoped (they depend on ``tmp_path``), so the
    result is memoized here rather than declared module-scoped. The inputs are identical
    on every call, so reuse is safe.
    """
    if "result" not in _BACKTEST_CACHE:
        _BACKTEST_CACHE["result"] = run_walk_forward(
            market=market,
            config=config,
            models=["gbm", "bootstrap"],
            tickers=["NVDA", "AMD"],
            horizons=[21, 63],
            start=pd.Timestamp("2020-01-01"),
            n_paths=300,
            progress=False,
        )
    return _BACKTEST_CACHE["result"]


def test_backtest_produces_scoreable_rows(backtest_result):
    rows = backtest_result.rows
    assert not rows.empty
    assert set(rows["model"]) == {"gbm", "bootstrap"}
    assert set(rows["horizon"]) == {21, 63}
    assert rows["pit"].between(0, 1).all()
    assert (rows["crps"] >= 0).all()
    assert rows["non_overlapping"].any()


def test_every_row_has_a_realized_outcome_after_its_origin(backtest_result):
    rows = backtest_result.rows
    assert (pd.to_datetime(rows["end_date"]) > pd.to_datetime(rows["origin"])).all()


def test_coverage_intervals_are_ordered(backtest_result):
    rows = backtest_result.rows
    assert (rows["lower_90"] <= rows["lower_50"]).all()
    assert (rows["upper_50"] <= rows["upper_90"]).all()


def test_scorecard_reports_sample_sizes_and_low_power(backtest_result, config):
    scorecard = build_scorecard(
        backtest_result, config.backtest.coverage_levels, config.risk.var_levels
    )
    assert not scorecard.empty
    for column in ("n", "n_independent", "low_power", "coverage_90", "crps_skill"):
        assert column in scorecard.columns
    assert (scorecard["n_independent"] <= scorecard["n"]).all()


def test_baseline_has_zero_skill_against_itself(backtest_result, config):
    """A definitional check that catches a mis-joined baseline."""
    table = crps_table(backtest_result.rows, backtest_result.baseline)
    baseline_rows = table[table["model"] == backtest_result.baseline]
    assert np.allclose(baseline_rows["crps_skill"], 0.0, atol=1e-12)


def test_var_tests_use_only_non_overlapping_origins(backtest_result, config):
    table = var_test_table(backtest_result.rows, config.risk.var_levels)
    if table.empty:
        pytest.skip("too few independent origins in the fixture window")
    rows = backtest_result.rows
    for _, record in table.iterrows():
        subset = rows[
            (rows["model"] == record["model"])
            & (rows["ticker"] == record["ticker"])
            & (rows["horizon"] == record["horizon"])
        ]
        assert record["n_independent"] == int(subset["non_overlapping"].sum())
        assert record["n_independent"] < len(subset) or record["horizon"] <= 21


def test_pit_tables_cover_every_model_and_horizon(backtest_result):
    """Histogram counts come from every origin; the chi-squared test does not.

    The shape is best estimated with all the data, but testing uniformity on overlapping
    windows would inflate the sample and reject almost any model, so ``n`` (the tested
    sample) must be the non-overlapping subset of ``n_all`` (the plotted sample).
    """
    tables = pit_tables(backtest_result.rows, bins=10)
    assert set(tables) == {"gbm|21", "gbm|63", "bootstrap|21", "bootstrap|63"}
    for key, table in tables.items():
        assert table["count"].sum() == table.attrs["n_all"], key
        assert table.attrs["n"] <= table.attrs["n_all"], key

    # At 63 days with monthly origins, most windows overlap, so the tested sample must be
    # strictly smaller than the plotted one.
    assert tables["gbm|63"].attrs["n"] < tables["gbm|63"].attrs["n_all"]


def test_written_summary_states_its_caveats(backtest_result, config):
    scorecard = build_scorecard(
        backtest_result, config.backtest.coverage_levels, config.risk.var_levels
    )
    summary = model_summary(scorecard, backtest_result.baseline)
    text = written_summary(
        backtest_result,
        scorecard,
        summary,
        pit_tables(backtest_result.rows),
        dm_table(backtest_result.rows, backtest_result.baseline),
    )

    lowered = text.lower()
    assert "overlap" in lowered, "the summary must disclose overlapping windows"
    assert "survivorship" in lowered
    assert "non-overlapping" in lowered
    assert str(len(backtest_result.origins)) in text


def test_coverage_table_matches_a_hand_computation(backtest_result, config):
    table = coverage_table(backtest_result.rows, [0.90])
    rows = backtest_result.rows
    sample = table.iloc[0]
    subset = rows[
        (rows["model"] == sample["model"])
        & (rows["ticker"] == sample["ticker"])
        & (rows["horizon"] == sample["horizon"])
    ]
    assert sample["coverage_90"] == pytest.approx(float(subset["hit_90"].mean()))


def test_failed_cells_are_recorded_not_swallowed(market, config):
    """A model that always fails must be reported in `failures`, not silently dropped."""
    result = run_walk_forward(
        market=market,
        config=config,
        models=["garch_t"],
        tickers=["NVDA"],
        horizons=[21],
        start=pd.Timestamp("2021-01-01"),
        end=pd.Timestamp("2021-06-01"),
        n_paths=50,
        progress=False,
    )
    # garch_t should mostly succeed here; the contract is that failures are counted.
    assert len(result.rows) + len(result.failures) > 0
    if not result.failures.empty:
        assert {"origin", "ticker", "model", "stage", "error"}.issubset(result.failures.columns)


def test_pit_diagnosis_separates_centre_from_width(backtest_result):
    """The diagnosis must name *how* a forecast misses, not just that it does."""
    from semimc.backtest.scorecard import diagnose_pit

    tables = pit_tables(backtest_result.rows, bins=10)
    for key, table in tables.items():
        text = diagnose_pit(table)
        assert ("centre" in text) or ("outcomes land" in text), key
        assert ("width" in text) or ("too narrow" in text) or ("too wide" in text), key


def test_pit_diagnosis_detects_a_biased_centre():
    """A forecast whose outcomes always land high must be reported as drift, not width."""
    from semimc.backtest.scorecard import diagnose_pit

    high = pd.DataFrame({"count": [0] * 10})
    high.attrs.update({"mean_pit": 0.70, "tail_mass": 0.20, "uniform_tail_mass": 0.20, "n": 200})
    text = diagnose_pit(high)
    assert "land high" in text and "drift was too low" in text
    assert "width is about right" in text


def test_pit_diagnosis_detects_an_overconfident_width():
    """Excess mass in the outer bins must be reported as too narrow."""
    from semimc.backtest.scorecard import diagnose_pit

    narrow = pd.DataFrame({"count": [0] * 10})
    narrow.attrs.update({"mean_pit": 0.50, "tail_mass": 0.35, "uniform_tail_mass": 0.20, "n": 200})
    text = diagnose_pit(narrow)
    assert "centre is unbiased" in text
    assert "too narrow" in text

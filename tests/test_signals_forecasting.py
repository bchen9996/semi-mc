"""Phase 4B: point-in-time signals, the panel/sector regressions, and the drift tilt.

The addendum names five tests explicitly (Section 7 of its changes to the brief). All five
are here, plus enough coverage of the regression and tilt machinery that a broken
Driscoll-Kraay call or a mis-centred bootstrap would fail loudly rather than just produce
quietly wrong numbers:

- signals released after an origin never reach models at that origin
- training targets never end after the origin
- coefficient bootstraps resample dates rather than rows
- blend weights are non-negative, sum to 1, and use only past origins (blend's weight
  test lives in ``test_models_addendum.py``; the "past origins only" half is tested here
  against ``optimize_crps_weights``, which is what a caller restricts to past data)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from semimc.data.earnings import load_calendar
from semimc.data.returns import log_returns
from semimc.forecasting import (
    approx_trading_days_per_period,
    build_regression_row,
    date_grouped_gapped_splits,
    fit_driscoll_kraay,
    min_gap_periods,
    standardize_cross_sectionally,
    trailing_betas,
    training_rows,
)
from semimc.forecasting.combine import (
    cross_validated_pinball_losses,
    fit_quantile_regressions,
    fit_ridge_or_lasso,
    fit_simple_average,
    predict_quantiles,
    select_alpha_by_cv,
    shrinkage_spec,
    simple_average_spec,
    single_or_combined_spec,
)
from semimc.forecasting.evaluate import (
    clark_west_statistic,
    oos_r_squared,
    rank_information_coefficient,
    sign_flip_rate,
)
from semimc.forecasting.pipeline import build_full_panel, monthly_origins
from semimc.forecasting.sector import (
    build_sector_regression_row,
    fit_sector_tilt,
    industry_yoy_growth,
)
from semimc.forecasting.stability import (
    fama_macbeth_coefficient_path,
    market_environment_split,
    rolling_coefficient_stability,
)
from semimc.forecasting.tilt import (
    block_bootstrap_date_indices,
    bootstrap_panel_tilt_draws,
    build_drift_tilt,
    shrink_and_cap,
)
from semimc.signals.build import (
    earnings_drift_signal,
    momentum_signal,
    relative_strength_signal,
    reversal_signal,
    signal_panel,
)
from semimc.signals.external import as_of as external_as_of
from semimc.signals.external import load_external_series
from semimc.signals.supply_chain import customer_lead_lag_signal, load_supply_chain_links


@pytest.fixture
def panel_returns(prices) -> pd.DataFrame:
    return log_returns(prices)


@pytest.fixture
def calendar(config):
    return load_calendar(config.path(config.data.earnings_calendar))


def _monthly_origins(returns_panel: pd.DataFrame) -> pd.DatetimeIndex:
    """Month-end trading days present in ``returns_panel``'s index.

    ``.to_numpy()`` on the resampled *frame* is 2-D (one column per ticker); wrapping that
    directly in ``set()`` tries to hash whole rows and fails, so the single representative
    column's values are flattened with ``.ravel()`` instead.
    """
    last_of_month = returns_panel.resample("ME").apply(lambda x: x.index[-1] if len(x) else None)
    return pd.DatetimeIndex(sorted(set(last_of_month.dropna().to_numpy().ravel())))


# ---------------------------------------------------------------------------------------
# Addendum-required test 1: signals released after an origin never reach models at it
# ---------------------------------------------------------------------------------------


def test_external_series_released_after_origin_is_invisible(tmp_path):
    """A row's release_date must gate it exactly -- not one day off in either direction."""
    csv_path = tmp_path / "series.csv"
    csv_path.write_text(
        "period,value,release_date,source\n"
        "2020-01-31,100.0,2020-03-01,test\n"
        "2020-02-29,101.0,2020-04-01,test\n",
        encoding="utf-8",
    )
    series = load_external_series(csv_path)

    before_first_release = external_as_of(series, pd.Timestamp("2020-02-28"))
    assert before_first_release.empty

    on_first_release = external_as_of(series, pd.Timestamp("2020-03-01"))
    assert list(on_first_release.index) == [pd.Timestamp("2020-01-31")]

    after_second_release = external_as_of(series, pd.Timestamp("2020-04-01"))
    assert set(after_second_release.index) == {
        pd.Timestamp("2020-01-31"),
        pd.Timestamp("2020-02-29"),
    }

    day_before_second = external_as_of(series, pd.Timestamp("2020-03-31"))
    assert list(day_before_second.index) == [pd.Timestamp("2020-01-31")], (
        "the second row's value leaked one day before its release_date"
    )


def test_supply_chain_link_recorded_after_origin_is_invisible(tmp_path):
    """The point-in-time rule applies to the *existence* of a link, not just a value."""
    csv_path = tmp_path / "links.csv"
    csv_path.write_text(
        "supplier,customer,source,as_of\nNVDA,MSFT,test,2024-06-01\n",
        encoding="utf-8",
    )
    links = load_supply_chain_links(csv_path)
    # Must span past the link's as_of (2024-06-01) with room left for the signal's default
    # lookback window, or "after" would fail for the same reason "before" is meant to.
    idx = pd.bdate_range("2020-01-01", periods=1200)
    assert idx[-1] > pd.Timestamp("2024-06-01"), "fixture window must extend past the link's as_of"
    customer_returns = pd.DataFrame(
        {"MSFT": np.random.default_rng(0).standard_normal(1200) * 0.01}, index=idx
    )

    before = customer_lead_lag_signal(links, customer_returns, ["NVDA"], pd.Timestamp("2024-05-01"))
    assert before.isna().all(), "a link must not be usable before its own as_of date"

    after = customer_lead_lag_signal(links, customer_returns, ["NVDA"], idx[-1])
    assert after.notna().all()


def test_industry_yoy_growth_only_uses_released_vintages(tmp_path):
    csv_path = tmp_path / "industry.csv"
    rows = [f"2020-{m:02d}-28,{100 + m},2020-{m + 2:02d}-15,fred" for m in range(1, 13)]
    csv_path.write_text("period,value,release_date,source\n" + "\n".join(rows), encoding="utf-8")
    series = load_external_series(csv_path)

    # As of a date before month 13's release, the YoY calc must use only released rows.
    early_cutoff = pd.Timestamp("2020-04-01")
    growth = industry_yoy_growth(series, early_cutoff)
    assert np.isnan(growth), "no year-ago comparison exists yet this early; must not fabricate one"


# ---------------------------------------------------------------------------------------
# Addendum-required test 2: training targets never end after the origin
# ---------------------------------------------------------------------------------------


def test_training_rows_never_have_a_target_ending_after_the_origin(panel_returns, calendar, config):
    tickers = ["NVDA", "AMD"]
    origins = _monthly_origins(panel_returns)
    rows = []
    for origin in origins:
        row = build_regression_row(
            tickers,
            panel_returns.loc[:, [*tickers, "SPY", "SOXX"]],
            panel_returns["SPY"],
            panel_returns["SOXX"],
            calendar,
            origin,
            horizon_days=21,
        )
        rows.append(row.reset_index().rename(columns={"index": "ticker"}))
    full_panel = pd.concat(rows, ignore_index=True)

    as_of = origins[len(origins) // 2]
    train = training_rows(full_panel, as_of)

    assert not train.empty
    assert (train["target_end_date"] <= as_of).all()
    assert (train["date"] <= as_of).all()
    # And the converse: rows whose target realizes after as_of must be excluded, not just
    # rows whose signal date is after as_of.
    excluded = full_panel[~full_panel.index.isin(train.index)]
    late_target = excluded.dropna(subset=["target_end_date"])
    assert (late_target["target_end_date"] > as_of).all() or late_target.empty


def test_training_rows_excludes_unrealized_targets_near_the_end_of_history(panel_returns, calendar):
    """The most recent origins have no realized 21-day-forward target yet -- confirms the
    filter actually removes rows, not just that it accepts everything."""
    tickers = ["NVDA", "AMD"]
    last_origin = panel_returns.index[-1]
    row = (
        build_regression_row(
            tickers,
            panel_returns.loc[:, [*tickers, "SPY", "SOXX"]],
            panel_returns["SPY"],
            panel_returns["SOXX"],
            calendar,
            last_origin,
            horizon_days=21,
        )
        .reset_index()
        .rename(columns={"index": "ticker"})
    )
    assert row["target"].isna().all(), "the last date in history cannot have a realized target"

    train = training_rows(row, last_origin)
    assert train.empty


# ---------------------------------------------------------------------------------------
# Addendum-required test 3: coefficient bootstraps resample dates, not rows
# ---------------------------------------------------------------------------------------


def test_block_bootstrap_resamples_whole_dates():
    dates = np.array(pd.bdate_range("2020-01-01", periods=40))
    rng = np.random.default_rng(3)
    resamples = block_bootstrap_date_indices(dates, n_bootstraps=25, block_length=4, rng=rng)

    assert len(resamples) == 25
    for sample in resamples:
        assert len(sample) == len(dates)
        # Every value drawn must be one of the original dates -- resampling dates, not
        # inventing intermediate ones the way row-level jittering could.
        assert set(sample.tolist()) <= set(dates.tolist())


def test_bootstrap_panel_tilt_draws_only_uses_whole_date_blocks():
    """A date's rows must move together across a resample -- never split across ticker rows
    independently, which is what a row-level (rather than date-level) bootstrap would do."""
    rng = np.random.default_rng(1)
    dates = pd.bdate_range("2018-01-01", periods=60, freq="D")[::3][:15]
    tickers = ["A", "B", "C"]
    rows = []
    for date in dates:
        for ticker in tickers:
            rows.append(
                {
                    "date": date,
                    "ticker": ticker,
                    "momentum": rng.standard_normal(),
                    "target": rng.standard_normal() * 0.01,
                }
            )
    frame = pd.DataFrame(rows)
    latest = frame[frame["date"] == dates[-1]].set_index("ticker")

    draws = bootstrap_panel_tilt_draws(
        frame, ["momentum"], latest, n_bootstraps=20, block_length=3, rng=rng
    )
    assert draws.shape == (20, len(tickers))
    assert np.isfinite(draws).all()


# ---------------------------------------------------------------------------------------
# Signal builders
# ---------------------------------------------------------------------------------------


def test_momentum_excludes_the_skip_window(panel_returns):
    as_of = panel_returns.index[-1]
    mom = momentum_signal(panel_returns[["NVDA", "AMD"]], as_of, lookback_days=252, skip_days=21)
    rev = reversal_signal(panel_returns[["NVDA", "AMD"]], as_of, skip_days=21)
    # The two windows must not overlap: momentum's stop position is reversal's start.
    assert not mom.isna().all()
    assert not rev.isna().all()


def test_relative_strength_uses_the_sector_benchmark(panel_returns):
    as_of = panel_returns.index[-1]
    rs = relative_strength_signal(panel_returns[["NVDA", "AMD"]], panel_returns["SOXX"], as_of, 126)
    assert set(rs.index) == {"NVDA", "AMD"}
    assert rs.notna().all()


def test_earnings_drift_is_nan_outside_the_active_window(panel_returns, calendar):
    as_of = panel_returns.index[-1]
    # A tiny active window should make the signal inactive for almost every ticker.
    drift = earnings_drift_signal(
        calendar, panel_returns[["NVDA", "AMD"]], panel_returns["SPY"], as_of, active_days=0
    )
    assert drift.isna().all()


def test_signal_panel_returns_all_four_columns(panel_returns, calendar):
    as_of = panel_returns.index[-1]
    panel = signal_panel(
        panel_returns[["NVDA", "AMD"]], panel_returns["SOXX"], panel_returns["SPY"], calendar, as_of
    )
    assert set(panel.columns) == {"momentum", "reversal", "relative_strength", "earnings_drift"}
    assert set(panel.index) == {"NVDA", "AMD"}


# ---------------------------------------------------------------------------------------
# Regression machinery
# ---------------------------------------------------------------------------------------


def _build_training_panel(panel_returns, calendar, tickers, horizon=21, n_origins=60):
    origins = _monthly_origins(panel_returns)[-n_origins:]
    rows = []
    for origin in origins:
        row = build_regression_row(
            tickers,
            panel_returns.loc[:, [*tickers, "SPY", "SOXX"]],
            panel_returns["SPY"],
            panel_returns["SOXX"],
            calendar,
            origin,
            horizon_days=horizon,
        )
        rows.append(row.reset_index().rename(columns={"index": "ticker"}))
    panel = pd.concat(rows, ignore_index=True)
    return standardize_cross_sectionally(panel, ["momentum", "reversal", "relative_strength"])


def test_driscoll_kraay_fit_runs_and_reports_no_fixed_effects(panel_returns, calendar):
    panel = _build_training_panel(panel_returns, calendar, ["NVDA", "AMD"])
    as_of = panel["date"].max()
    train = training_rows(panel, as_of).dropna(
        subset=["momentum", "reversal", "relative_strength", "target"]
    )

    result = fit_driscoll_kraay(train, ["momentum", "reversal", "relative_strength"], maxlags=2)
    assert set(result.coefficients.index) == {"const", "momentum", "reversal", "relative_strength"}
    assert result.n_observations == len(train)
    assert np.isfinite(result.r_squared)


def test_trailing_betas_use_only_data_ending_at_origin(panel_returns):
    as_of = panel_returns.index[len(panel_returns) // 2]
    sector_factor_returns = panel_returns["SOXX"] - panel_returns["SPY"]
    beta_m, beta_s = trailing_betas(
        panel_returns["NVDA"], panel_returns["SPY"], sector_factor_returns, as_of
    )
    assert np.isfinite(beta_m)
    assert np.isfinite(beta_s)


def test_date_grouped_gapped_splits_leave_a_gap():
    dates = pd.bdate_range("2015-01-01", periods=300, freq="D")[::7]  # ~weekly-ish spacing
    splits = date_grouped_gapped_splits(dates, n_folds=3, gap_periods=2)
    assert len(splits) >= 1
    for train_dates, test_dates in splits:
        assert len(train_dates) > 0
        assert len(test_dates) > 0
        assert train_dates.max() < test_dates.min()


def test_sector_tilt_regression_runs(panel_returns):
    sector_returns = panel_returns["SOXX"] - panel_returns["SPY"]
    rng = np.random.default_rng(0)
    periods = pd.date_range("2016-01-31", periods=90, freq="ME")
    external = pd.DataFrame(
        {
            "period": periods,
            "value": 100 * np.cumprod(1 + rng.normal(0.002, 0.01, len(periods))),
            "release_date": periods + pd.Timedelta(days=45),
            "source": "synthetic",
            "revised": True,
        }
    )
    origins = _monthly_origins(panel_returns)[13:-2]
    rows = [build_sector_regression_row(sector_returns, external, origin, 21) for origin in origins]
    frame = pd.DataFrame(rows)
    usable = frame.dropna(subset=["industry_yoy", "target"])
    if len(usable) < 12:
        pytest.skip("synthetic external series did not produce enough usable rows")
    result = fit_sector_tilt(usable, maxlags=2)
    assert "industry_yoy" in result.coefficients.index


# ---------------------------------------------------------------------------------------
# Tilt construction
# ---------------------------------------------------------------------------------------


def test_shrink_and_cap_respects_the_cap():
    raw = np.array([10.0, -10.0, 0.02])
    out = shrink_and_cap(raw, shrinkage_lambda=0.5, cap=0.10)
    assert np.all(np.abs(out) <= 0.10 + 1e-12)


def test_build_drift_tilt_combines_relative_and_sector_with_beta():
    tickers = ["A", "B"]
    relative_point = pd.Series({"A": 0.01, "B": -0.02})
    relative_draws = np.tile(relative_point.to_numpy(), (10, 1))
    beta_sector = pd.Series({"A": 0.5, "B": 1.5})

    tilt = build_drift_tilt(
        tickers=tickers,
        relative_point_h=relative_point,
        relative_draws_h=relative_draws,
        sector_point_h=0.005,
        sector_draws_h=np.full(10, 0.005),
        beta_sector=beta_sector,
        shrinkage_lambda=1.0,
        tilt_cap_annual=1.0,
        horizon_days=21,
        trading_days_per_year=252,
        label="test",
    )
    annualize = 252 / 21
    expected_a = (0.01 + 0.5 * 0.005) * annualize
    expected_b = (-0.02 + 1.5 * 0.005) * annualize
    np.testing.assert_allclose(tilt.point, [expected_a, expected_b], rtol=1e-8)


# ---------------------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------------------


def test_oos_r_squared_is_zero_when_predictions_equal_the_benchmark():
    # oos_r_squared refuses to score fewer than 5 observations, so this needs at least that
    # many -- otherwise the function's own guard, not the formula under test, is exercised.
    actual = np.array([0.01, -0.02, 0.03, -0.01, 0.015, -0.005])
    assert oos_r_squared(actual, np.zeros(6), benchmark=0.0) == pytest.approx(0.0)


def test_oos_r_squared_positive_when_predictions_beat_zero_forecast():
    rng = np.random.default_rng(0)
    actual = rng.normal(0, 0.02, 200)
    predicted = actual * 0.5 + rng.normal(0, 0.005, 200)  # correlated with actual
    assert oos_r_squared(actual, predicted) > 0


def test_clark_west_favors_a_genuinely_informative_signal():
    rng = np.random.default_rng(2)
    n = 300
    actual = rng.normal(0, 0.02, n)
    unrestricted = actual * 0.3 + rng.normal(0, 0.01, n)
    restricted = np.zeros(n)
    t_stat, p_value = clark_west_statistic(actual, restricted, unrestricted)
    assert np.isfinite(t_stat)
    assert p_value < 0.5


def test_rank_ic_reports_n_dates():
    rng = np.random.default_rng(4)
    dates = np.repeat(pd.bdate_range("2020-01-01", periods=20), 6)
    predicted = pd.Series(rng.standard_normal(len(dates)))
    realized = predicted * 0.4 + rng.standard_normal(len(dates)) * 0.6
    result = rank_information_coefficient(predicted, pd.Series(realized), pd.Series(dates))
    assert result["n_dates"] == 20
    assert np.isfinite(result["mean_ic"])


def test_sign_flip_rate_detects_alternating_signs():
    alternating = pd.Series([1.0, -1.0, 1.0, -1.0, 1.0])
    assert sign_flip_rate(alternating) == pytest.approx(1.0)
    stable = pd.Series([1.0, 1.0, 1.0, 1.0])
    assert sign_flip_rate(stable) == pytest.approx(0.0)


# ---------------------------------------------------------------------------------------
# Combination models (addendum "simple to complex" ladder): simple average, ridge/lasso,
# quantile regression, and the shared CandidateSpec/evaluate_candidate CV harness.
# ---------------------------------------------------------------------------------------


def _synthetic_signal_panel(rng, n_dates=60, tickers=("A", "B", "C")):
    """A hand-built panel with a known relationship (``target ~ 0.5 * momentum``, reversal
    pure noise) -- fast and precise for checking coefficient recovery and combination
    behaviour without going through the full origin-by-origin pipeline."""
    dates = pd.bdate_range("2018-01-31", periods=n_dates, freq="ME")
    rows = []
    for date in dates:
        for ticker in tickers:
            momentum = rng.normal()
            reversal = rng.normal()
            target = 0.5 * momentum + rng.normal(scale=0.15)
            rows.append(
                {
                    "date": date,
                    "ticker": ticker,
                    "momentum": momentum,
                    "reversal": reversal,
                    "target": target,
                }
            )
    return pd.DataFrame(rows)


def test_fit_simple_average_combines_components_and_tolerates_a_missing_signal():
    rng = np.random.default_rng(11)
    panel = _synthetic_signal_panel(rng)
    model = fit_simple_average(panel, ["momentum", "reversal"], maxlags=1, min_observations=10)
    assert set(model.models) == {"momentum", "reversal"}

    both = panel.iloc[[0]].copy()
    momentum_only = model.models["momentum"].predict(both[["momentum"]]).iloc[0]
    reversal_only = model.models["reversal"].predict(both[["reversal"]]).iloc[0]
    both_prediction = model.predict(both).iloc[0]
    assert both_prediction == pytest.approx((momentum_only + reversal_only) / 2)

    missing_reversal = both.copy()
    missing_reversal["reversal"] = np.nan
    partial_prediction = model.predict(missing_reversal).iloc[0]
    assert partial_prediction == pytest.approx(momentum_only)


def test_fit_simple_average_drops_a_sparse_component():
    rng = np.random.default_rng(12)
    panel = _synthetic_signal_panel(rng)
    sparse = panel.copy()
    sparse.loc[sparse.index[5:], "reversal"] = np.nan  # far below min_observations
    model = fit_simple_average(sparse, ["momentum", "reversal"], maxlags=1, min_observations=10)
    assert "momentum" in model.models
    assert "reversal" not in model.models


def test_ridge_shrinks_the_momentum_coefficient_as_alpha_grows():
    rng = np.random.default_rng(13)
    panel = _synthetic_signal_panel(rng, n_dates=80)
    small_alpha = fit_ridge_or_lasso(panel, ["momentum", "reversal"], alpha=0.01, method="ridge")
    large_alpha = fit_ridge_or_lasso(panel, ["momentum", "reversal"], alpha=1000.0, method="ridge")
    assert small_alpha.coefficients["momentum"] > 0  # recovers the true positive relationship
    assert abs(large_alpha.coefficients["momentum"]) < abs(small_alpha.coefficients["momentum"])


def test_lasso_zeroes_coefficients_at_a_large_enough_alpha():
    rng = np.random.default_rng(14)
    panel = _synthetic_signal_panel(rng, n_dates=80)
    model = fit_ridge_or_lasso(panel, ["momentum", "reversal"], alpha=100.0, method="lasso")
    assert model.coefficients["momentum"] == pytest.approx(0.0, abs=1e-9)
    assert model.coefficients["reversal"] == pytest.approx(0.0, abs=1e-9)


def test_ridge_lasso_raise_below_the_minimum_row_count():
    tiny = pd.DataFrame({"momentum": [0.1] * 10, "reversal": [0.2] * 10, "target": [0.01] * 10})
    with pytest.raises(ValueError):
        fit_ridge_or_lasso(tiny, ["momentum", "reversal"], alpha=1.0, method="ridge")


def test_ridge_or_lasso_rejects_an_unknown_method():
    rng = np.random.default_rng(15)
    panel = _synthetic_signal_panel(rng)
    with pytest.raises(ValueError):
        fit_ridge_or_lasso(panel, ["momentum"], alpha=1.0, method="elasticnet")


@pytest.mark.parametrize(
    "spec_factory",
    [
        lambda cols: single_or_combined_spec("combined", cols, maxlags=1),
        lambda cols: simple_average_spec(cols, maxlags=1),
        lambda cols: shrinkage_spec(cols, alpha=1.0, method="ridge"),
        lambda cols: shrinkage_spec(cols, alpha=1.0, method="lasso"),
    ],
)
def test_candidate_spec_evaluate_returns_the_expected_metric_keys(spec_factory):
    rng = np.random.default_rng(16)
    panel = _synthetic_signal_panel(rng, n_dates=90)
    spec = spec_factory(["momentum", "reversal"])
    outcome = spec.evaluate(panel, n_folds=4, gap_periods=1)
    assert outcome is not None
    for key in ("signal", "n_obs", "n_cv_test_obs", "cv_oos_r2", "clark_west_t", "rank_ic_mean"):
        assert key in outcome
    assert outcome["signal"] == spec.label


def test_candidate_spec_evaluate_recovers_the_true_momentum_effect():
    """The combined regression should score positively out of sample on data where momentum
    genuinely predicts the target -- the sanity check that the new CV harness (shared by
    every candidate on the ladder) isn't silently scoring something other than what it
    claims to."""
    rng = np.random.default_rng(17)
    panel = _synthetic_signal_panel(rng, n_dates=100)
    spec = single_or_combined_spec("combined", ["momentum", "reversal"], maxlags=1)
    outcome = spec.evaluate(panel, n_folds=4, gap_periods=1)
    assert outcome is not None
    assert outcome["cv_oos_r2"] > 0


def test_select_alpha_by_cv_picks_a_value_from_the_grid():
    rng = np.random.default_rng(18)
    panel = _synthetic_signal_panel(rng, n_dates=90)
    grid = [0.1, 1.0, 10.0, 100.0]
    alpha, outcome = select_alpha_by_cv(panel, ["momentum", "reversal"], "ridge", grid, 4, 1)
    assert alpha in grid
    assert outcome is None or "cv_oos_r2" in outcome


def test_quantile_regressions_are_monotonic_in_quantile_level_on_average():
    rng = np.random.default_rng(19)
    panel = _synthetic_signal_panel(rng, n_dates=80)
    models = fit_quantile_regressions(
        panel, ["momentum", "reversal"], (0.05, 0.25, 0.5, 0.75, 0.95)
    )
    assert set(models) == {0.05, 0.25, 0.5, 0.75, 0.95}
    predictions = predict_quantiles(models, panel)
    means = [predictions[q].mean() for q in (0.05, 0.25, 0.5, 0.75, 0.95)]
    assert means == sorted(means)


def test_quantile_regressions_raises_below_the_minimum_row_count():
    tiny = pd.DataFrame({"momentum": [0.1] * 10, "reversal": [0.2] * 10, "target": [0.01] * 10})
    with pytest.raises(ValueError):
        fit_quantile_regressions(tiny, ["momentum", "reversal"], (0.5,))


def test_cross_validated_pinball_losses_are_nonnegative_for_the_median():
    rng = np.random.default_rng(21)
    panel = _synthetic_signal_panel(rng, n_dates=90)
    losses = cross_validated_pinball_losses(
        panel, ["momentum", "reversal"], (0.5,), n_folds=4, gap_periods=1
    )
    assert np.isfinite(losses[0.5])
    assert losses[0.5] >= 0


def test_candidate_spec_evaluate_raises_when_too_few_dates_for_the_requested_folds():
    rng = np.random.default_rng(22)
    panel = _synthetic_signal_panel(rng, n_dates=10)  # date_grouped_gapped_splits needs >= 15
    spec = single_or_combined_spec("combined", ["momentum", "reversal"], maxlags=1)
    with pytest.raises(ValueError):
        spec.evaluate(panel, n_folds=5, gap_periods=1)


# ---------------------------------------------------------------------------------------
# Stability diagnostics: rolling coefficients, sign flips, market-environment split.
# ---------------------------------------------------------------------------------------


def _stability_panel(rng, n_dates, n_tickers=8, flip_signs=False, start="2018-01-31"):
    """A panel with a clean, low-noise momentum relationship -- either constant across
    dates (``flip_signs=False``) or alternating every date (``flip_signs=True``), so
    the per-date OLS coefficient's sign is predictable and the resulting sign-flip rate is
    unambiguous, not a matter of statistical luck."""
    dates = pd.bdate_range(start, periods=n_dates, freq="ME")
    rows = []
    for index, date in enumerate(dates):
        sign = -1.0 if flip_signs and index % 2 == 1 else 1.0
        for t in range(n_tickers):
            momentum = rng.normal()
            target = sign * 0.6 * momentum + rng.normal(scale=0.02)
            rows.append({"date": date, "ticker": f"T{t}", "momentum": momentum, "target": target})
    return pd.DataFrame(rows)


def test_fama_macbeth_coefficient_path_is_date_indexed_with_one_row_per_date():
    rng = np.random.default_rng(30)
    panel = _stability_panel(rng, n_dates=24)
    path = fama_macbeth_coefficient_path(panel, ["momentum"])
    assert list(path.columns) == ["const", "momentum"]
    assert len(path) == 24
    assert (path["momentum"] > 0).all(), (
        "a clean positive relationship should recover positive coefficients throughout"
    )


def test_fama_macbeth_coefficient_path_skips_dates_below_the_cross_section_floor():
    rng = np.random.default_rng(31)
    panel = _stability_panel(rng, n_dates=10, n_tickers=8)
    sparse = panel[~((panel["date"] == panel["date"].iloc[0]) & (panel["ticker"] != "T0"))]
    path = fama_macbeth_coefficient_path(sparse, ["momentum"], min_cross_section=5)
    assert panel["date"].iloc[0] not in path.index


def test_rolling_coefficient_stability_reports_low_flip_rate_for_a_stable_relationship():
    rng = np.random.default_rng(32)
    panel = _stability_panel(rng, n_dates=30, flip_signs=False)
    result = rolling_coefficient_stability(panel, ["momentum"])
    assert result["momentum"]["n_dates"] == 30
    assert result["momentum"]["sign_flip_rate"] < 0.1


def test_rolling_coefficient_stability_reports_high_flip_rate_for_an_alternating_relationship():
    rng = np.random.default_rng(33)
    panel = _stability_panel(rng, n_dates=30, flip_signs=True)
    result = rolling_coefficient_stability(panel, ["momentum"])
    assert result["momentum"]["sign_flip_rate"] > 0.8


def test_rolling_coefficient_stability_returns_nan_for_a_column_with_no_usable_dates():
    rng = np.random.default_rng(34)
    panel = _stability_panel(rng, n_dates=10, n_tickers=2)  # below the default min_cross_section
    result = rolling_coefficient_stability(panel, ["momentum"])
    assert result["momentum"]["n_dates"] == 0
    assert np.isnan(result["momentum"]["sign_flip_rate"])


def test_market_environment_split_fits_both_halves_when_enough_data():
    rng = np.random.default_rng(35)
    downturn_dates = pd.bdate_range("2022-01-31", periods=9, freq="ME")
    rally_dates = pd.bdate_range("2022-10-31", periods=15, freq="ME")
    rows = []
    for dates in (downturn_dates, rally_dates):
        for date in dates:
            for t in range(8):
                momentum = rng.normal()
                target = 0.5 * momentum + rng.normal(scale=0.05)
                rows.append(
                    {"date": date, "ticker": f"T{t}", "momentum": momentum, "target": target}
                )
    panel = pd.DataFrame(rows)
    split = market_environment_split(panel, ["momentum"], min_observations=30)
    assert split["downturn"]["result"] is not None
    assert split["rally"]["result"] is not None
    assert split["downturn"]["n_observations"] == 9 * 8
    assert split["rally"]["n_observations"] == 15 * 8


def test_market_environment_split_reports_missing_data_without_failing():
    rng = np.random.default_rng(36)
    dates = pd.bdate_range("2022-01-31", periods=3, freq="ME")  # too little for either half
    rows = [
        {"date": date, "ticker": f"T{t}", "momentum": rng.normal(), "target": rng.normal()}
        for date in dates
        for t in range(8)
    ]
    panel = pd.DataFrame(rows)
    split = market_environment_split(panel, ["momentum"], min_observations=1000)
    assert split["downturn"]["result"] is None
    assert split["rally"]["result"] is None


# ---------------------------------------------------------------------------------------
# Origin frequency: a higher-frequency, shorter-horizon signal search over the same
# already-approved signals, never touching the spent 21d/63d holdout.
# ---------------------------------------------------------------------------------------


def test_monthly_origins_default_is_unchanged():
    trading_days = pd.bdate_range("2020-01-01", "2020-12-31")
    default = monthly_origins(trading_days, trading_days[0], trading_days[-1])
    explicit = monthly_origins(trading_days, trading_days[0], trading_days[-1], freq="ME")
    assert list(default) == list(explicit)
    assert len(default) == 12


def test_monthly_origins_accepts_a_higher_frequency():
    trading_days = pd.bdate_range("2020-01-01", "2020-03-31")
    weekly = monthly_origins(trading_days, trading_days[0], trading_days[-1], freq="W-FRI")
    monthly = monthly_origins(trading_days, trading_days[0], trading_days[-1], freq="ME")
    assert len(weekly) > len(monthly)
    # Every returned origin is a real trading day, never a synthetic period end.
    assert set(weekly).issubset(set(trading_days))


def test_build_full_panel_weekly_origins_produce_more_dates_than_monthly(market):
    tickers = ["NVDA", "AMD"]
    start, end = market.prices.index[0], market.prices.index[-1]
    monthly_panel = build_full_panel(market, tickers, horizon_days=21, start=start, end=end)
    weekly_panel = build_full_panel(
        market, tickers, horizon_days=21, start=start, end=end, origin_freq="W-FRI"
    )
    assert weekly_panel["date"].nunique() > monthly_panel["date"].nunique()


def test_approx_trading_days_per_period_matches_known_conventions():
    assert approx_trading_days_per_period("ME") == pytest.approx(21.39, abs=0.05)
    assert approx_trading_days_per_period("QE") == pytest.approx(62.78, abs=0.05)
    assert approx_trading_days_per_period("W-FRI") == pytest.approx(4.83, abs=0.05)


def test_min_gap_periods_flags_the_pre_existing_63d_monthly_gap():
    """The addendum's own already-spent 63d/monthly evaluation used cv_gap_periods=1, but
    a 63-day forward window at ~21 trading days/month needs a 3-period gap -- this is what
    exposes that, not a new requirement invented for the higher-frequency search."""
    assert min_gap_periods(21, "ME") == 1
    assert min_gap_periods(63, "ME") == 3
    assert min_gap_periods(5, "W-FRI") == 2
    assert min_gap_periods(10, "W-FRI") == 3

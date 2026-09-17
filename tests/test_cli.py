"""CLI smoke tests: every command runs end to end on fixture data, with no network.

These are deliberately shallow on numbers and strict on plumbing -- that a command exits 0,
writes the artifacts it promises, and records enough to reproduce itself. The statistics
are tested elsewhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from semimc.cli import app

FIXTURES = Path(__file__).resolve().parent / "fixtures"
runner = CliRunner()


def base_overrides(tmp_path: Path) -> list[str]:
    """``--set`` flags wiring every command to the fixtures and a temp workspace.

    Paths are written with forward slashes: override values are parsed as YAML, and a
    Windows backslash would be read as an escape.
    """
    fixtures = FIXTURES.as_posix()
    return [
        "--set",
        "data.provider=csv",
        "--set",
        f"data.csv_dir={fixtures}",
        "--set",
        f"data.cache_dir={(tmp_path / 'cache').as_posix()}",
        "--set",
        f"data.earnings_calendar={(FIXTURES / 'earnings_calendar.csv').as_posix()}",
        "--set",
        f"backtest.risk_free_series={(FIXTURES / 'risk_free_3m.csv').as_posix()}",
        "--set",
        f"reporting.runs_dir={(tmp_path / 'runs').as_posix()}",
        "--set",
        "universe.groups={fixture: [NVDA, AMD]}",
        "--set",
        "fit.min_observations=300",
        "--set",
        "fit.window_years=4",
    ]


def invoke(args: list[str], tmp_path: Path):
    result = runner.invoke(app, args + base_overrides(tmp_path))
    if result.exit_code != 0:
        raise AssertionError(
            f"`semimc {' '.join(args)}` exited {result.exit_code}\n"
            f"{result.output}\n{result.exception!r}"
        )
    return result


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A temp workspace with the fixture prices already cached."""
    invoke(["fetch"], tmp_path)
    return tmp_path


def latest_run(tmp_path: Path) -> Path:
    runs = sorted((tmp_path / "runs").iterdir())
    assert runs, "no run directory was created"
    return runs[-1]


# ---------------------------------------------------------------------------------------


def test_models_lists_every_model():
    result = runner.invoke(app, ["models"])
    assert result.exit_code == 0
    for name in (
        "gbm",
        "bootstrap",
        "garch_t",
        "earnings_jumps",
        "factor",
        "factor_regime",
        "fhs",
        "merton",
        "har",
        "regime",
        "blend",
    ):
        assert name in result.output


def test_fetch_populates_the_cache(tmp_path):
    result = invoke(["fetch"], tmp_path)
    assert "fetched" in result.output
    cached = list((tmp_path / "cache").glob("*.parquet"))
    assert {p.stem for p in cached} == {"NVDA", "AMD", "SPY", "SOXX"}


def test_fetch_is_idempotent(tmp_path):
    invoke(["fetch"], tmp_path)
    second = invoke(["fetch"], tmp_path)
    assert "cached" in second.output, "a fresh cache must not refetch"


def test_validate_writes_a_report(workspace):
    result = invoke(["validate"], workspace)
    assert "Summary statistics" in result.output
    run = latest_run(workspace)
    for name in ("validation_issues.csv", "summary_statistics.csv", "correlation_matrix.csv"):
        assert (run / name).exists(), name
    assert (run / "correlations.png").exists()


def test_simulate_single_ticker_writes_every_phase_one_artifact(workspace):
    result = invoke(
        ["simulate", "--ticker", "NVDA", "--horizon", "63", "--model", "gbm", "--paths", "800"],
        workspace,
    )
    assert "Risk table" in result.output
    assert "Convergence" in result.output

    run = latest_run(workspace)
    for name in (
        "fan_chart.png",
        "terminal_distribution.png",
        "drawdowns.png",
        "convergence.png",
        "risk_table.csv",
        "convergence.csv",
        "config_snapshot.json",
        "manifest.json",
    ):
        assert (run / name).exists(), f"missing {name}"


def test_simulate_records_enough_to_reproduce(workspace):
    invoke(
        ["simulate", "--ticker", "NVDA", "--horizon", "21", "--paths", "500", "--seed", "99"],
        workspace,
    )
    manifest = json.loads((latest_run(workspace) / "manifest.json").read_text())
    assert manifest["seed"] == 99
    assert manifest["n_paths"] == 500
    assert manifest["horizon"] == 21
    assert manifest["training_range"]["first_date"] < manifest["training_range"]["last_date"]

    # The manifest must record the drift policy actually used, and it must agree with the
    # snapshot beside it -- that pairing is what makes a run reproducible. Asserted against
    # the configured default rather than a hard-coded name, so changing the default is a
    # deliberate config edit rather than a test failure.
    from semimc.config import load_config

    expected_policy = load_config().drift.policy
    assert manifest["model"]["drift_policy"] == expected_policy

    snapshot = json.loads((latest_run(workspace) / "config_snapshot.json").read_text())
    assert snapshot["drift"]["policy"] == expected_policy


def test_simulate_is_reproducible_from_the_seed(workspace):
    import pandas as pd

    outputs = []
    for _ in range(2):
        invoke(
            ["simulate", "--ticker", "NVDA", "--horizon", "21", "--paths", "400", "--seed", "7"],
            workspace,
        )
        outputs.append(pd.read_csv(latest_run(workspace) / "risk_table.csv", index_col=0))
    pd.testing.assert_frame_equal(outputs[0], outputs[1])


def test_simulate_portfolio_reports_cvar_contributions(workspace):
    result = invoke(
        ["simulate", "--portfolio", "NVDA=0.6,AMD=0.4", "--horizon", "63", "--paths", "800"],
        workspace,
    )
    assert "Contribution to 95% CVaR" in result.output
    run = latest_run(workspace)
    assert (run / "cvar_contributions.csv").exists()


def test_simulate_rejects_both_ticker_and_portfolio(tmp_path):
    result = runner.invoke(
        app, ["simulate", "--ticker", "NVDA", "--portfolio", "NVDA=1.0"] + base_overrides(tmp_path)
    )
    assert result.exit_code != 0


def test_simulate_implied_vol_rejects_a_non_gbm_model(tmp_path):
    """--implied-vol only overrides GBM's vol_estimator; no other model has one."""
    result = runner.invoke(
        app,
        ["simulate", "--ticker", "NVDA", "--model", "garch_t", "--implied-vol"]
        + base_overrides(tmp_path),
    )
    assert result.exit_code != 0


def test_simulate_implied_vol_rejects_a_portfolio(tmp_path):
    """--implied-vol fetches one ticker's option chain; it has no portfolio meaning."""
    result = runner.invoke(
        app,
        ["simulate", "--portfolio", "NVDA=1.0", "--implied-vol"] + base_overrides(tmp_path),
    )
    assert result.exit_code != 0


@pytest.mark.parametrize(
    "model",
    [
        "gbm",
        "bootstrap",
        "garch_t",
        "fhs",
        "factor",
        "factor_regime",
        "merton",
        "har",
        "regime",
        "blend",
    ],
)
def test_simulate_runs_for_every_model(workspace, model):
    invoke(
        ["simulate", "--ticker", "NVDA", "--horizon", "21", "--model", model, "--paths", "400"],
        workspace,
    )
    assert (latest_run(workspace) / "risk_table.csv").exists()


def test_backtest_produces_a_scorecard_and_summary(workspace):
    result = invoke(
        [
            "backtest",
            "--models",
            "gbm,bootstrap",
            "--horizons",
            "21",
            "--tickers",
            "NVDA",
            "--start",
            "2021-01-01",
            "--paths",
            "200",
        ],
        workspace,
    )
    assert "Model summary" in result.output
    run = latest_run(workspace)
    for name in ("scorecard.csv", "model_summary.csv", "backtest_rows.csv", "summary.md"):
        assert (run / name).exists(), name

    summary = (run / "summary.md").read_text(encoding="utf-8")
    assert "Caveats" in summary
    assert "survivorship" in summary.lower()


def test_signals_evaluates_candidate_signals(workspace):
    result = invoke(
        [
            "signals",
            "--horizon",
            "21",
            "--set",
            "signals.min_regression_observations=15",
            "--set",
            "signals.cv_n_folds=3",
            "--set",
            "backtest.start=2016-01-01",
        ],
        workspace,
    )
    assert "cross-validated" in result.output.lower()
    run = latest_run(workspace)
    assert (run / "signal_report.csv").exists()
    assert (run / "summary.md").exists()


def test_signals_holdout_appends_to_the_research_log(workspace):
    # signals.research_log is redirected into the test workspace explicitly -- without
    # this override the command would append to the real repo's research_log.md, which is
    # exactly the bug this override exists to prevent (see CLAUDE.md's Gotchas).
    log_path = workspace / "research_log.md"
    result = invoke(
        [
            "signals",
            "--horizon",
            "21",
            "--holdout",
            "--set",
            "signals.min_regression_observations=10",
            "--set",
            "signals.cv_n_folds=2",
            "--set",
            "signals.holdout_months=12",
            "--set",
            "backtest.start=2016-01-01",
            "--set",
            f"signals.research_log={log_path.as_posix()}",
        ],
        workspace,
    )
    holdout_ran = "one-time holdout result" in result.output.lower()
    holdout_skipped = "not evaluated" in result.output.lower()
    assert holdout_ran or holdout_skipped
    if holdout_ran:
        assert log_path.exists()
        assert "holdout evaluation" in log_path.read_text(encoding="utf-8").lower()


def test_simulate_with_tilt_applies_or_gracefully_skips(workspace):
    """--tilt must never crash a run, whether or not enough data exists to fit one."""
    result = invoke(
        [
            "simulate",
            "--portfolio",
            "NVDA=0.5,AMD=0.5",
            "--horizon",
            "21",
            "--paths",
            "400",
            "--tilt",
            "--set",
            "signals.min_regression_observations=15",
            "--set",
            "backtest.start=2016-01-01",
        ],
        workspace,
    )
    assert result.exit_code == 0
    manifest = json.loads((latest_run(workspace) / "manifest.json").read_text())
    assert "drift_tilt" in manifest
    assert manifest["drift_tilt"]["applied"] in (True, False)


def test_scenario_runs_and_labels_itself_conditional(workspace):
    result = invoke(
        ["scenario", "--name", "sector_shock_25", "--horizon", "21", "--paths", "500"],
        workspace,
    )
    assert "conditional outcomes" in result.output.lower()
    assert (latest_run(workspace) / "scenario_impact.csv").exists()


def test_scenario_no_equal_weight_drops_the_portfolio_row(workspace):
    # --equal-weight had no negation for a while (typer only auto-derives --no-<flag>
    # when the option string doesn't already look like one name; it needed the explicit
    # "on/off" form in cli.py). With it missing, --no-equal-weight was "no such option"
    # and equal_weight could never be False -- the PORTFOLIO row could never be dropped.
    invoke(
        ["scenario", "--name", "sector_shock_25", "--horizon", "21", "--paths", "300"], workspace
    )
    with_portfolio = pd.read_csv(latest_run(workspace) / "scenario_impact.csv", index_col=0)
    assert "PORTFOLIO" in with_portfolio.index

    invoke(
        [
            "scenario",
            "--name",
            "sector_shock_25",
            "--horizon",
            "21",
            "--paths",
            "300",
            "--no-equal-weight",
        ],
        workspace,
    )
    without_portfolio = pd.read_csv(latest_run(workspace) / "scenario_impact.csv", index_col=0)
    assert "PORTFOLIO" not in without_portfolio.index


def test_scenario_rejects_an_unknown_name(workspace):
    result = runner.invoke(
        app, ["scenario", "--name", "not_a_scenario"] + base_overrides(workspace)
    )
    assert result.exit_code != 0


@pytest.mark.parametrize("name", ["sector_shock_25", "vol_spike_2x_21d", "export_control_china"])
def test_every_configured_scenario_runs(workspace, name):
    invoke(["scenario", "--name", name, "--horizon", "21", "--paths", "300"], workspace)
    assert (latest_run(workspace) / "scenario_impact.csv").exists()


def test_report_builds_self_contained_html(workspace):
    invoke(
        ["simulate", "--ticker", "NVDA", "--horizon", "21", "--paths", "500"],
        workspace,
    )
    invoke(["report", "--run", "latest"], workspace)

    html = (latest_run(workspace) / "report.html").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "not investment advice" in html.lower()
    assert "Limitations" in html
    assert "Survivorship bias" in html
    assert "data:image/png;base64," in html, "charts must be embedded, not linked"
    assert "http://" not in html and "https://" not in html, "the report must be self-contained"


def test_report_on_a_backtest_run(workspace):
    invoke(
        [
            "backtest",
            "--models",
            "gbm",
            "--horizons",
            "21",
            "--tickers",
            "NVDA",
            "--start",
            "2021-06-01",
            "--paths",
            "200",
        ],
        workspace,
    )
    invoke(["report", "--run", "latest"], workspace)
    html = (latest_run(workspace) / "report.html").read_text(encoding="utf-8")
    assert "Findings" in html
    assert "scorecard" in html.lower()

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


def test_signals_flags_a_gap_period_too_small_for_the_horizon_and_frequency(workspace):
    result = invoke(
        [
            "signals",
            "--horizon",
            "5",
            "--set",
            "signals.origin_frequency=W-FRI",
            "--set",
            "signals.min_regression_observations=10",
            "--set",
            "signals.cv_n_folds=2",
            "--set",
            "backtest.start=2016-01-01",
        ],
        workspace,
    )
    assert "cv_gap_periods=1 is smaller than the 2 needed" in result.output


def _holdout_args(log_path: Path, holdout_months: int, confirm: bool = False) -> list[str]:
    args = [
        "signals",
        "--horizon",
        "21",
        "--holdout",
        "--set",
        "signals.min_regression_observations=10",
        "--set",
        "signals.cv_n_folds=2",
        "--set",
        f"signals.holdout_months={holdout_months}",
        "--set",
        "backtest.start=2016-01-01",
        "--set",
        f"signals.research_log={log_path.as_posix()}",
    ]
    if confirm:
        args.append("--confirm-new-holdout-window")
    return args


def test_signals_holdout_refuses_an_identical_rerun(workspace):
    log_path = workspace / "research_log.md"
    first = runner.invoke(app, _holdout_args(log_path, 12) + base_overrides(workspace))
    assert first.exit_code == 0
    assert "one-time holdout result" in first.output.lower(), first.output

    second = runner.invoke(app, _holdout_args(log_path, 12) + base_overrides(workspace))
    assert second.exit_code != 0
    assert "already records a holdout window" in second.output
    assert "refusing to re-spend it" in second.output.lower()


def test_signals_holdout_requires_confirmation_for_a_later_but_overlapping_window(workspace):
    log_path = workspace / "research_log.md"
    first = runner.invoke(app, _holdout_args(log_path, 12) + base_overrides(workspace))
    assert first.exit_code == 0

    unconfirmed = runner.invoke(app, _holdout_args(log_path, 6) + base_overrides(workspace))
    assert unconfirmed.exit_code != 0
    assert "--confirm-new-holdout-window" in unconfirmed.output

    confirmed = runner.invoke(
        app, _holdout_args(log_path, 6, confirm=True) + base_overrides(workspace)
    )
    assert confirmed.exit_code == 0
    assert "one-time holdout result" in confirmed.output.lower()


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


# ---------------------------------------------------------------------------------------
# paper decide / execute / mark / report (Phase 5)
# ---------------------------------------------------------------------------------------


def _paper_overrides(tmp_path: Path) -> list[str]:
    return base_overrides(tmp_path) + [
        "--set",
        f"paper.ledger_path={(tmp_path / 'ledger.sqlite3').as_posix()}",
    ]


def _tradeable_paper_overrides(
    tmp_path: Path, account_number: str = "FAKE-PAPER-0001"
) -> list[str]:
    return _paper_overrides(tmp_path) + [
        "--set",
        "paper.sleeves.s0.trading_enabled=true",
        "--set",
        f"paper.registered_accounts=[{account_number}]",
    ]


@pytest.fixture
def fake_alpaca_broker(monkeypatch):
    """``paper execute``/``paper mark`` construct ``AlpacaPaperBroker.from_env()`` --
    patched here to a ``FakeBroker`` instance so the CLI wiring is exercised end to end
    with no network and no credentials, the same boundary-mocking approach
    ``tests/test_alpaca_broker.py`` uses for the SDK itself."""
    from semimc.execution.fake_broker import FakeBroker

    broker = FakeBroker(account_number="FAKE-PAPER-0001")
    monkeypatch.setattr(
        "semimc.execution.alpaca_broker.AlpacaPaperBroker.from_env",
        classmethod(lambda cls: broker),
    )
    return broker


def test_paper_decide_freezes_a_decision(workspace):
    result = runner.invoke(
        app,
        ["paper", "decide", "--sleeve", "s0", "--as-of", "2021-06-01"]
        + _paper_overrides(workspace),
    )
    assert result.exit_code == 0, result.output
    assert "Decided 's0'" in result.output


def test_paper_decide_rejects_an_unknown_sleeve(workspace):
    result = runner.invoke(
        app, ["paper", "decide", "--sleeve", "bogus"] + _paper_overrides(workspace)
    )
    assert result.exit_code != 0


def test_paper_decide_skips_a_second_call_in_the_same_period(workspace):
    overrides = _paper_overrides(workspace)
    first = runner.invoke(
        app, ["paper", "decide", "--sleeve", "s0", "--as-of", "2021-06-01"] + overrides
    )
    assert first.exit_code == 0, first.output
    second = runner.invoke(
        app, ["paper", "decide", "--sleeve", "s0", "--as-of", "2021-06-15"] + overrides
    )
    assert second.exit_code == 0, second.output
    assert "Skipped" in second.output


def test_paper_execute_declines_cleanly_when_trading_is_disabled(workspace, fake_alpaca_broker):
    overrides = _paper_overrides(workspace)
    decide = runner.invoke(
        app, ["paper", "decide", "--sleeve", "s0", "--as-of", "2021-06-01"] + overrides
    )
    assert decide.exit_code == 0, decide.output

    result = runner.invoke(
        app, ["paper", "execute", "--sleeve", "s0", "--as-of", "2021-06-01"] + overrides
    )
    assert result.exit_code == 0, result.output
    assert "Declined at pretrade" in result.output


def test_paper_execute_dry_run_reports_intended_orders(workspace, fake_alpaca_broker):
    overrides = _tradeable_paper_overrides(workspace)
    decide = runner.invoke(
        app, ["paper", "decide", "--sleeve", "s0", "--as-of", "2021-06-01"] + overrides
    )
    assert decide.exit_code == 0, decide.output

    result = runner.invoke(
        app,
        ["paper", "execute", "--sleeve", "s0", "--as-of", "2021-06-01", "--dry-run"] + overrides,
    )
    assert result.exit_code == 0, result.output
    assert "Would submit" in result.output
    assert fake_alpaca_broker.list_positions() == []  # dry-run never touches the broker


def test_paper_mark_runs_cleanly_with_no_open_orders(tmp_path, fake_alpaca_broker):
    result = runner.invoke(app, ["paper", "mark", "--sleeve", "s0"] + _paper_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Reconciliation: ok" in result.output


def test_paper_report_runs_with_no_history(tmp_path):
    result = runner.invoke(app, ["paper", "report", "--sleeve", "s0"] + _paper_overrides(tmp_path))
    assert result.exit_code == 0, result.output
    assert "No snapshot recorded yet." in result.output


def _seed_snapshots(ledger_path: Path, sleeve: str, dates, equities) -> None:
    from semimc.live.ledger import Ledger

    ledger = Ledger(ledger_path)
    for date, equity in zip(dates, equities, strict=True):
        ledger.record_snapshot(sleeve, date, equity=equity, cash=equity * 0.1, positions={})
    ledger.close()


def test_paper_evaluate_t2_caveats_a_short_overlapping_history(workspace):
    overrides = _paper_overrides(workspace)
    ledger_path = workspace / "ledger.sqlite3"
    dates = pd.date_range("2026-01-05", periods=10, freq="B")
    _seed_snapshots(ledger_path, "s0", dates, [100_000.0 * 1.001**i for i in range(10)])
    _seed_snapshots(ledger_path, "s3", dates, [100_000.0 * 1.0005**i for i in range(10)])

    result = runner.invoke(app, ["paper", "evaluate", "--thesis", "t2"] + overrides)
    assert result.exit_code == 0, result.output
    assert "CAVEAT" in result.output
    assert "overlapping days: 9" in result.output  # one lost to pct_change()


def test_paper_evaluate_t3_replays_the_shadow_sleeve(workspace):
    from semimc.live.ledger import Ledger
    from semimc.strategy.decide import Decision

    overrides = _paper_overrides(workspace)
    ledger_path = workspace / "ledger.sqlite3"
    dates = pd.date_range("2021-06-01", periods=5, freq="B")
    _seed_snapshots(ledger_path, "s3", dates, [100_000.0 * 1.0005**i for i in range(5)])

    ledger = Ledger(ledger_path)
    decision = Decision(
        as_of=dates[0], sleeve="vol_twin_shadow", weights=pd.Series({"NVDA": 0.5, "AMD": 0.5})
    )
    ledger.record_decision(decision)
    ledger.close()

    result = runner.invoke(app, ["paper", "evaluate", "--thesis", "t3"] + overrides)
    assert result.exit_code == 0, result.output
    assert "vol_twin_shadow" in result.output


def test_paper_evaluate_t5_reports_no_fills_cleanly(tmp_path):
    result = runner.invoke(
        app, ["paper", "evaluate", "--thesis", "t5"] + _paper_overrides(tmp_path)
    )
    assert result.exit_code == 0, result.output
    assert "No fills recorded yet" in result.output


def test_paper_evaluate_rejects_an_unknown_thesis(tmp_path):
    result = runner.invoke(
        app, ["paper", "evaluate", "--thesis", "t99"] + _paper_overrides(tmp_path)
    )
    assert result.exit_code != 0

"""T6's measure and verdict (`intraday/t6.py`), on made-up exits only.

`THESES.md`'s rule: each exit's P&L net of modeled costs plus the measured SOXX hedge's
P&L, in basis points of entry notional; agent mean minus placebo mean; each mean's error
clustered by trading day, the difference's the root of their sum of squares; verdicts on
2027-01-15 and 2027-04-15 at 2 standard errors. No real ledger is read here.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from statsmodels.regression.linear_model import OLS
from typer.testing import CliRunner

from semimc.cli import app
from semimc.config import PROJECT_ROOT, load_config
from semimc.intraday.ledger import IntradayLedger
from semimc.intraday.t6 import (
    FINAL_VERDICT,
    FIRST_VERDICT,
    SleeveMeasure,
    T6Result,
    clustered_mean,
    evaluate_t6,
    exit_bps,
    measure_sleeve,
    verdict,
)

SINCE = dt.date(2026, 10, 15)


def _content(net: float, hedge_pnl: float | None, *, qty: float = 10.0, entry: float = 100.0):
    content = {
        "side": "buy",
        "qty": qty,
        "avg_entry_price": entry,
        "pnl_gross": net + 1.0,
        "pnl_net": net,
    }
    if hedge_pnl is not None:
        content["hedge"] = {"beta": 1.2, "notional": 1_200.0, "pnl": hedge_pnl, "cost": 0.5}
    return content


def _exit(ledger: IntradayLedger, day: str, net: float, hedge_pnl: float | None = 0.0) -> None:
    ledger.record_reflection(pd.Timestamp(day), "NVDA", _content(net, hedge_pnl))


def test_an_exit_is_its_net_pnl_plus_the_hedges_pnl_and_not_the_hedges_cost():
    # $1,000 of entry notional; net +$5, the hedge lost $2: +$3 is 30 bps. The hedge's
    # $0.50 of modeled cost isn't charged, since it was never traded.
    assert exit_bps(_content(5.0, -2.0)) == pytest.approx(30.0)


def test_with_one_exit_a_day_the_clustered_error_is_the_ordinary_one():
    values = [10.0, -20.0, 35.0, 5.0]

    mean, stderr = clustered_mean(values, ["a", "b", "c", "d"])

    assert mean == pytest.approx(statistics.mean(values))
    assert stderr == pytest.approx(statistics.stdev(values) / math.sqrt(len(values)))


def test_exits_on_one_day_are_one_cluster():
    values = [10.0, 30.0, -20.0, 40.0, 0.0, 20.0]
    days = ["mon", "mon", "tue", "wed", "wed", "wed"]

    mean, stderr = clustered_mean(values, days)

    # Closed form: mean 80/6; residual sums per day E_mon = 40 - 2*mean, E_tue = -20 - mean,
    # E_wed = 60 - 3*mean; error = sqrt(G/(G-1) * sum E_g^2) / n with G = 3, n = 6.
    m = 80.0 / 6.0
    sums = [40.0 - 2 * m, -20.0 - m, 60.0 - 3 * m]
    assert mean == pytest.approx(m)
    assert stderr == pytest.approx(math.sqrt(3 / 2 * sum(e * e for e in sums)) / 6)
    # The same as a regression on a constant with statsmodels' cluster covariance.
    groups = np.array([0, 0, 1, 2, 2, 2])
    fit = OLS(np.array(values), np.ones(len(values))).fit(
        cov_type="cluster", cov_kwds={"groups": groups}
    )
    assert stderr == pytest.approx(float(fit.bse[0]))


def test_one_trading_day_gives_a_mean_but_no_error():
    assert clustered_mean([1.0, 2.0], ["mon", "mon"]) == (1.5, None)
    assert clustered_mean([], []) == (None, None)


def test_the_window_runs_from_stage_2_to_the_as_of_day(tmp_path):
    ledger = IntradayLedger(tmp_path / "agent.sqlite3")
    _exit(ledger, "2026-10-14", 100.0)  # Stage 1: out
    _exit(ledger, "2026-10-15", 1.0)
    _exit(ledger, "2027-01-15", 3.0)
    _exit(ledger, "2027-01-18", 100.0)  # after the verdict date: out

    measure = measure_sleeve(ledger, SINCE, FIRST_VERDICT)

    assert (measure.exits, measure.days) == (2, 2)
    assert measure.mean_bps == pytest.approx(20.0)  # 10 and 30 bps
    ledger.close()


def test_what_the_measure_cant_price_is_counted_not_measured(tmp_path):
    ledger = IntradayLedger(tmp_path / "agent.sqlite3")
    _exit(ledger, "2026-11-02", 2.0)
    _exit(ledger, "2026-11-03", 50.0, hedge_pnl=None)  # not priced yet
    # A stop that filled for a position with no proposal: no reflection is ever written.
    at = dt.datetime(2026, 11, 5, 19, 30)
    ledger.record_order(
        "recovered-QCOM-stop", "QCOM", "buy", "filled", qty=5.0, submitted_at=at, leg="stop"
    )
    ledger.record_fill("recovered-QCOM-stop", 5.0, 158.326, at)

    measure = measure_sleeve(ledger, SINCE, FIRST_VERDICT)

    assert measure.exits == 1
    assert measure.mean_bps == pytest.approx(20.0)
    assert (measure.unpriced, measure.unreflected) == (1, 1)
    ledger.close()


def test_the_statistic_is_the_agents_mean_minus_the_placebos(tmp_path):
    agent = IntradayLedger(tmp_path / "agent.sqlite3")
    placebo = IntradayLedger(tmp_path / "placebo.sqlite3")
    for day, net in [("2026-11-02", 3.0), ("2026-11-03", 1.0), ("2026-11-04", 2.0)]:
        _exit(agent, day, net)
    for day, net in [("2026-11-02", -1.0), ("2026-11-02", 0.0), ("2026-11-03", 1.0)]:
        _exit(placebo, day, net)

    result = evaluate_t6(agent, placebo, SINCE, FIRST_VERDICT)

    assert result.agent.mean_bps == pytest.approx(20.0)
    assert result.placebo.mean_bps == pytest.approx(0.0)
    assert result.difference_bps == pytest.approx(20.0)
    expected = math.hypot(result.agent.stderr_bps, result.placebo.stderr_bps)
    assert result.stderr_bps == pytest.approx(expected)
    assert result.z == pytest.approx(20.0 / expected)
    agent.close()
    placebo.close()


def _result(as_of: dt.date, z: float | None) -> T6Result:
    """A result whose difference is `z` standard errors (10 bps each), or has no error."""
    stderr = None if z is None else 10.0
    agent = SleeveMeasure(100, 40, 10.0 * (z or 0.0), stderr, 0, 0)
    placebo = SleeveMeasure(300, 40, 0.0, None if z is None else 0.0, 0, 0)
    return T6Result(SINCE, as_of, agent, placebo)


@pytest.mark.parametrize(
    ("as_of", "z", "starts"),
    [
        (FIRST_VERDICT - dt.timedelta(days=1), 5.0, "No verdict"),
        (FIRST_VERDICT, 2.0, "3-month verdict: the agent is ahead"),
        (FIRST_VERDICT, 1.99, "3-month verdict: within 2"),
        (FIRST_VERDICT, -1.99, "3-month verdict: within 2"),
        (FIRST_VERDICT, -2.0, "3-month verdict: the agent is behind"),
        (FIRST_VERDICT, None, "3-month verdict: within 2"),
        (FINAL_VERDICT, 2.0, "6-month verdict: the agent is ahead"),
        (FINAL_VERDICT, 1.99, "6-month verdict: short of 2"),
        (FINAL_VERDICT, None, "6-month verdict: short of 2"),
    ],
)
def test_the_verdict_follows_the_rule(as_of, z, starts):
    text = verdict(_result(as_of, z))

    assert text.startswith(starts)
    assert ("stops trading" in text) == (
        (as_of >= FINAL_VERDICT and (z is None or z < 2)) or (as_of >= FIRST_VERDICT and z == -2.0)
    )


def test_paper_yaml_starts_the_window_where_the_rule_says_stage_2_began():
    cfg = load_config(PROJECT_ROOT / "config" / "paper.yaml")

    assert cfg.intraday.readiness.stage_2_started == SINCE


runner = CliRunner()


def _overrides(tmp_path: Path) -> list[str]:
    settings = {
        "intraday.ledger_path": (tmp_path / "agent.sqlite3").as_posix(),
        "intraday.shadow_ledger_path": (tmp_path / "placebo.sqlite3").as_posix(),
        "intraday.reports_dir": (tmp_path / "reports").as_posix(),
        "intraday.readiness.stage_2_started": "2026-10-15",
    }
    return [arg for key, value in settings.items() for arg in ("--set", f"{key}={value}")]


def test_the_command_refuses_before_the_first_verdict_date(tmp_path):
    result = runner.invoke(app, ["intraday", "t6", "--as-of", "2027-01-14", *_overrides(tmp_path)])

    assert result.exit_code == 1
    assert "first verdict is on 2027-01-15" in result.output
    assert not (tmp_path / "reports").exists()
    assert not (tmp_path / "agent.sqlite3").exists()


def test_the_command_writes_the_verdict_on_its_date(tmp_path):
    agent = IntradayLedger(tmp_path / "agent.sqlite3")
    placebo = IntradayLedger(tmp_path / "placebo.sqlite3")
    for day, net in [("2026-11-02", 3.0), ("2026-11-03", 1.0), ("2026-11-04", 2.0)]:
        _exit(agent, day, net)
        _exit(placebo, day, net)
    agent.close()
    placebo.close()

    result = runner.invoke(app, ["intraday", "t6", "--as-of", "2027-01-15", *_overrides(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "3-month verdict: within 2 standard errors" in result.output
    text = (tmp_path / "reports" / "2027-01-15" / "t6.md").read_text(encoding="utf-8")
    assert text.startswith("# T6: 2026-10-15 to 2027-01-15")
    assert "| agent | 3 | 3 | +20.0 |" in text
    assert "Agent minus placebo: +0.0 bps per exit" in text


def test_the_command_needs_the_placebos_ledger(tmp_path):
    IntradayLedger(tmp_path / "agent.sqlite3").close()

    result = runner.invoke(app, ["intraday", "t6", "--as-of", "2027-01-15", *_overrides(tmp_path)])

    assert result.exit_code == 1
    assert "No placebo ledger" in result.output
    assert not (tmp_path / "placebo.sqlite3").exists()

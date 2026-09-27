"""`intraday/readiness.py`: the whole-window evidence report behind a rollout-stage
decision. Ledger-only, so every test builds a real `IntradayLedger` in `tmp_path`;
`ledger._now` is pinned so incidents (dated by their insert time) land on a known
weekday regardless of when the suite runs."""

from __future__ import annotations

import pandas as pd
import pytest

from semimc.config import IntradayConfig
from semimc.intraday import ledger as ledger_module
from semimc.intraday.ledger import IntradayLedger
from semimc.intraday.readiness import (
    incident_kind,
    render_stage_readiness_markdown,
    stage_readiness,
)

MON = pd.Timestamp("2026-09-21")
TUE = pd.Timestamp("2026-09-22")


@pytest.fixture
def pinned_clock(monkeypatch):
    def pin(iso: str) -> None:
        monkeypatch.setattr(ledger_module, "_now", lambda: iso)

    pin("2026-09-21T17:45:00+00:00")
    return pin


def _active_day(ledger: IntradayLedger, day: pd.Timestamp) -> None:
    ledger.record_brief(day, {"summary": "brief"})
    ledger.record_watchlist_item(day, "NVDA", "thesis")
    ledger.record_model_call(day, "research", "researcher", "sonnet", "h", 10, 10, 0.05)
    ledger.record_model_call(day, "open_batch", "trader", "haiku", "h", 10, 10, 0.01)


def _check(report, prefix: str):
    return next(c for c in report.checks if c.name.startswith(prefix))


def test_incident_kind_groups_by_failure_mode_not_by_symbol():
    assert incident_kind("AMD: no quote available") == "no quote available"
    assert incident_kind("SMCI: no quote available") == "no quote available"
    assert incident_kind("BRK.B: no quote available") == "no quote available"
    assert incident_kind("stopout:NVDA") == "stopout"
    assert incident_kind("market is closed") == "market is closed"


def test_a_clean_active_day_passes_the_schedule_checks(tmp_path, pinned_clock):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    _active_day(ledger, MON)
    (tmp_path / "reports" / str(MON.date())).mkdir(parents=True)
    (tmp_path / "reports" / str(MON.date()) / "eod.md").write_text("x", encoding="utf-8")

    report = stage_readiness(ledger, None, IntradayConfig(), tmp_path / "reports", MON, MON)

    [day] = report.days
    assert day.research_brief
    assert day.watchlist_candidates == 1
    assert day.trader_calls == 1
    assert day.spend_usd == pytest.approx(0.06)
    assert day.first_activity_utc == "17:45"
    assert day.eod_report
    assert _check(report, "Activity recorded").passed
    assert _check(report, "Research and the Trader").passed
    assert _check(report, "End-of-day report").passed
    assert _check(report, "No error-severity").passed
    assert _check(report, "Peak daily model spend").passed


def test_a_weekday_with_nothing_recorded_is_flagged_not_silently_skipped(tmp_path, pinned_clock):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    _active_day(ledger, MON)  # TUE left empty

    report = stage_readiness(ledger, None, IntradayConfig(), tmp_path / "reports", MON, TUE)

    check = _check(report, "Activity recorded")
    assert not check.passed
    assert str(TUE.date()) in check.detail
    assert "holiday or missed run" in check.detail


def test_weekends_are_not_expected_trading_days(tmp_path, pinned_clock):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    friday, monday = pd.Timestamp("2026-09-18"), pd.Timestamp("2026-09-21")
    report = stage_readiness(ledger, None, IntradayConfig(), tmp_path, friday, monday)
    assert [d.date for d in report.days] == ["2026-09-18", "2026-09-21"]


def test_a_day_where_jobs_only_declined_counts_as_active_but_fails_the_run_check(
    tmp_path, pinned_clock
):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    ledger.record_incident("error", "intraday trading is not trading_enabled")

    report = stage_readiness(ledger, None, IntradayConfig(), tmp_path, MON, MON)

    assert report.days[0].any_activity
    assert _check(report, "Activity recorded").passed
    assert not _check(report, "Research and the Trader").passed
    assert not _check(report, "No error-severity").passed


def test_rejection_reasons_split_joined_validator_failures_and_count_them(tmp_path, pinned_clock):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    ledger.record_proposal(MON, "c1", "NVDA", "open", "rejected", {}, reason="a; b")
    ledger.record_proposal(MON, "c2", "AMD", "open", "rejected", {}, reason="a")
    ledger.record_proposal(MON, "c3", "TSM", "open", "accepted", {})

    report = stage_readiness(ledger, None, IntradayConfig(), tmp_path, MON, MON)

    assert report.rejection_reasons == [("a", 2), ("b", 1)]
    assert report.days[0].proposals_accepted == 1
    assert report.days[0].proposals_rejected == 2
    assert _check(report, "The agent produced proposals").passed


def test_incidents_are_grouped_by_severity_and_kind(tmp_path, pinned_clock):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    ledger.record_incident("warning", "AMD: no quote available")
    ledger.record_incident("warning", "SMCI: no quote available")
    ledger.record_incident("error", "market is closed")

    report = stage_readiness(ledger, None, IntradayConfig(), tmp_path, MON, MON)

    assert report.incident_kinds[0] == ("warning", "no quote available", 2)
    assert ("error", "market is closed", 1) in report.incident_kinds


def test_incidents_outside_the_window_are_excluded(tmp_path, pinned_clock):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    pinned_clock("2026-09-22T17:45:00+00:00")
    ledger.record_incident("error", "market is closed")

    report = stage_readiness(ledger, None, IntradayConfig(), tmp_path, MON, MON)

    assert report.incident_kinds == []
    assert _check(report, "No error-severity").passed


def test_agent_and_placebo_results_are_reported_side_by_side(tmp_path, pinned_clock):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    placebo = IntradayLedger(tmp_path / "intraday_shadow.sqlite3")
    ledger.record_reflection(MON, "NVDA", {"pnl_gross": 12.0, "pnl_net": 10.0})
    ledger.record_reflection(MON, "AMD", {"pnl_gross": -3.0, "pnl_net": -5.0})
    ledger.record_reflection(TUE, "TSM", {"pnl_gross": 99.0, "pnl_net": 99.0})  # after as_of
    placebo.record_reflection(MON, "MU", {"pnl_gross": 1.0, "pnl_net": -1.0})

    report = stage_readiness(ledger, placebo, IntradayConfig(), tmp_path, MON, MON)

    assert report.agent.closed_trades == 2
    assert report.agent.pnl_net == pytest.approx(5.0)
    assert report.agent.win_rate == pytest.approx(0.5)
    assert report.placebo is not None
    assert report.placebo.closed_trades == 1
    assert report.placebo.win_rate == 0.0
    assert _check(report, "Placebo comparison").passed


def test_a_missing_placebo_ledger_is_unavailable_not_zero(tmp_path, pinned_clock):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    report = stage_readiness(ledger, None, IntradayConfig(), tmp_path, MON, MON)
    assert report.placebo is None
    check = _check(report, "Placebo comparison")
    assert not check.passed
    assert check.detail == "no placebo ledger"
    assert "| placebo | not available |" in render_stage_readiness_markdown(report)


def test_the_closed_trade_threshold_reuses_the_playbook_evidence_minimum(tmp_path, pinned_clock):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    for _ in range(3):
        ledger.record_reflection(MON, "NVDA", {"pnl_net": 1.0})
    config = IntradayConfig(playbook={"evidence_min_trades": 3})

    report = stage_readiness(ledger, None, config, tmp_path, MON, MON)

    assert _check(report, "At least 3 closed trades").passed


def test_spend_above_the_daily_cap_fails_its_check(tmp_path, pinned_clock):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    ledger.record_model_call(MON, "research", "researcher", "sonnet", "h", 1, 1, 2.5)
    config = IntradayConfig(budget={"max_cost_usd_per_day": 2.0})

    report = stage_readiness(ledger, None, config, tmp_path, MON, MON)

    check = _check(report, "Peak daily model spend")
    assert not check.passed
    assert "$2.50" in check.detail


def test_render_includes_every_section_and_keeps_the_day_table_rectangular(tmp_path, pinned_clock):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    _active_day(ledger, MON)  # TUE silent

    text = render_stage_readiness_markdown(
        stage_readiness(ledger, None, IntradayConfig(), tmp_path, MON, TUE)
    )

    for heading in (
        "## Evidence checks",
        "## Agent vs. placebo",
        "## Day by day",
        "## Why proposals were rejected",
        "## Incidents by kind",
        "## Model spend",
    ):
        assert heading in text
    table = [line for line in text.splitlines() if line.startswith("| 2026-")]
    assert len(table) == 2
    header = next(line for line in text.splitlines() if line.startswith("| date |"))
    assert {line.count("|") for line in table} == {header.count("|")}

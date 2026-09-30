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
    assert _check(report, "The agent proposed trades").passed


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
        "## Stage 2 clean days",
    ):
        assert heading in text
    for section in ("## Day by day", "## Stage 2 clean days"):
        body = text.split(section, 1)[1].split("\n## ", 1)[0].splitlines()
        table = [line for line in body if line.startswith("| 2026-")]
        assert len(table) == 2
        header = next(line for line in body if line.startswith("| date |"))
        assert {line.count("|") for line in table} == {header.count("|")}


# -- Stage 2 clean trading days (owner rule, 2026-09-29) -----------------------------------


def _at(day: pd.Timestamp, hhmm: str) -> str:
    """A New York wall-clock time on ``day`` as the UTC ISO string the ledger stores."""
    local = pd.Timestamp(f"{day.date()} {hhmm}").tz_localize("America/New_York")
    return local.tz_convert("UTC").isoformat()


def _clean_day(ledger, placebo, day, reports, pinned_clock, *, accepted=0, holds=0, skip=()):
    """Everything a clean day needs, timed like the external schedule: research at 08:30
    ET, the open batch at 09:33 ET, cycles every 20 min from 10:00 to 15:40 ET."""
    import datetime as dt

    if "research" not in skip:
        pinned_clock(_at(day, "08:30"))
        ledger.record_brief(day, {"text": "brief"})
    if "open_batch" not in skip:
        pinned_clock(_at(day, "09:33"))
        ledger.record_model_call(day, "open_batch", "trader", "haiku", "h", 1, 1, 0.01)
    checks = ["09:33"] + [f"{h:02d}:{m:02d}" for h in range(10, 16) for m in (0, 20, 40)]
    for hhmm in checks:
        if hhmm in skip:
            continue
        ledger.record_monitor_check(day, "cycle", dt.datetime.fromisoformat(_at(day, hhmm)))
    for n in range(accepted):
        ledger.record_proposal(day, "cycle", f"S{n}", "open", "accepted", {})
    for n in range(holds):
        ledger.record_proposal(day, "cycle", f"H{n}", "hold", "accepted", {})
    if "placebo" not in skip:
        placebo.record_session_open_equity(day, 100_000.0)
    if "eod" not in skip:
        (reports / str(day.date())).mkdir(parents=True, exist_ok=True)
        (reports / str(day.date()) / "eod.md").write_text("x", encoding="utf-8")


@pytest.fixture
def two_ledgers(tmp_path):
    return (
        IntradayLedger(tmp_path / "intraday.sqlite3"),
        IntradayLedger(tmp_path / "intraday_shadow.sqlite3"),
        tmp_path / "reports",
    )


def test_an_on_time_day_is_clean(two_ledgers, pinned_clock):
    ledger, placebo, reports = two_ledgers
    _clean_day(ledger, placebo, MON, reports, pinned_clock)
    report = stage_readiness(ledger, placebo, IntradayConfig(), reports, MON, MON)
    [day] = report.days
    assert day.clean, day.clean_failures
    assert report.clean_streak == [str(MON.date())]


@pytest.mark.parametrize(
    ("skip", "expected"),
    [
        (("research",), "no research brief"),
        (("open_batch",), "no open-batch Trader call"),
        (("placebo",), "the placebo did not run"),
        (("eod",), "no eod report filed under this date"),
        (("12:00", "12:20"), "a 60-min gap between cycle checks"),
        (tuple(f"15:{m:02d}" for m in (0, 20, 40)), "early in the afternoon"),
    ],
)
def test_each_missing_piece_fails_the_day_with_its_reason(
    two_ledgers, pinned_clock, skip, expected
):
    ledger, placebo, reports = two_ledgers
    _clean_day(ledger, placebo, MON, reports, pinned_clock, skip=skip)
    [day] = stage_readiness(ledger, placebo, IntradayConfig(), reports, MON, MON).days
    assert not day.clean
    assert any(expected in reason for reason in day.clean_failures), day.clean_failures


def test_late_research_and_a_late_open_batch_fail_the_day(two_ledgers, pinned_clock):
    ledger, placebo, reports = two_ledgers
    _clean_day(ledger, placebo, MON, reports, pinned_clock, skip=("research", "open_batch"))
    pinned_clock(_at(MON, "14:13"))  # Monday 2026-09-29's late GitHub cron, roughly
    ledger.record_brief(MON, {"text": "late"})
    ledger.record_model_call(MON, "open_batch", "trader", "haiku", "h", 1, 1, 0.01)
    [day] = stage_readiness(ledger, placebo, IntradayConfig(), reports, MON, MON).days
    assert any("after the open" in r for r in day.clean_failures)
    assert any("min after the open" in r for r in day.clean_failures)


def test_an_evening_check_outside_the_session_is_not_a_gap(two_ledgers, pinned_clock):
    import datetime as dt

    ledger, placebo, reports = two_ledgers
    _clean_day(ledger, placebo, MON, reports, pinned_clock)
    ledger.record_monitor_check(MON, "cycle", dt.datetime.fromisoformat(_at(MON, "20:25")))
    [day] = stage_readiness(ledger, placebo, IntradayConfig(), reports, MON, MON).days
    assert day.clean, day.clean_failures


def test_an_error_incident_or_overspend_fails_the_day(two_ledgers, pinned_clock):
    ledger, placebo, reports = two_ledgers
    _clean_day(ledger, placebo, MON, reports, pinned_clock)
    pinned_clock(_at(MON, "11:00"))
    ledger.record_incident("error", "malformed trader proposal")
    ledger.record_model_call(MON, "cycle", "trader", "haiku", "h", 1, 1, 5.0)
    config = IntradayConfig(budget={"max_cost_usd_per_day": 2.0})
    [day] = stage_readiness(ledger, placebo, config, reports, MON, MON).days
    assert "1 error incident(s)" in day.clean_failures
    assert any("over the $2.00 cap" in r for r in day.clean_failures)


def test_a_warning_incident_does_not_fail_the_day(two_ledgers, pinned_clock):
    ledger, placebo, reports = two_ledgers
    _clean_day(ledger, placebo, MON, reports, pinned_clock)
    pinned_clock(_at(MON, "11:00"))
    ledger.record_incident("warning", "AMD: spread 80bps exceeds max_spread_bps 50.0")
    [day] = stage_readiness(ledger, placebo, IntradayConfig(), reports, MON, MON).days
    assert day.clean, day.clean_failures


def _week(ledger, placebo, reports, pinned_clock, days, accepted=0):
    for day in days:
        _clean_day(ledger, placebo, day, reports, pinned_clock, accepted=accepted)


WEEK = list(pd.bdate_range("2026-10-05", "2026-10-09"))


def test_three_clean_days_with_accepted_proposals_meet_the_rule(two_ledgers, pinned_clock):
    ledger, placebo, reports = two_ledgers
    _week(ledger, placebo, reports, pinned_clock, WEEK[:3], accepted=1)
    report = stage_readiness(ledger, placebo, IntradayConfig(), reports, WEEK[0], WEEK[2])
    assert report.clean_streak_accepted == 3
    assert _check(report, "Stage 2 clean-day streak").passed


def test_three_clean_days_without_proposals_need_five(two_ledgers, pinned_clock):
    ledger, placebo, reports = two_ledgers
    _week(ledger, placebo, reports, pinned_clock, WEEK[:3])
    report = stage_readiness(ledger, placebo, IntradayConfig(), reports, WEEK[0], WEEK[2])
    assert not _check(report, "Stage 2 clean-day streak").passed

    _week(ledger, placebo, reports, pinned_clock, WEEK[3:])
    report = stage_readiness(ledger, placebo, IntradayConfig(), reports, WEEK[0], WEEK[4])
    assert len(report.clean_streak) == 5
    assert _check(report, "Stage 2 clean-day streak").passed


def test_a_failed_day_resets_the_streak(two_ledgers, pinned_clock):
    ledger, placebo, reports = two_ledgers
    _week(ledger, placebo, reports, pinned_clock, [WEEK[0], WEEK[2], WEEK[3]], accepted=1)
    _clean_day(ledger, placebo, WEEK[1], reports, pinned_clock, skip=("eod",))
    report = stage_readiness(ledger, placebo, IntradayConfig(), reports, WEEK[0], WEEK[3])
    assert report.clean_streak == [str(WEEK[2].date()), str(WEEK[3].date())]
    assert not _check(report, "Stage 2 clean-day streak").passed


def test_an_excluded_day_neither_counts_nor_resets(two_ledgers, pinned_clock):
    ledger, placebo, reports = two_ledgers
    # WEEK[1] left silent (say, a market holiday) and listed by the owner.
    _week(ledger, placebo, reports, pinned_clock, [WEEK[0], WEEK[2], WEEK[3]], accepted=1)
    config = IntradayConfig(readiness={"excluded_days": [WEEK[1].date()]})
    report = stage_readiness(ledger, placebo, config, reports, WEEK[0], WEEK[3])
    assert report.days[1].excluded and not report.days[1].clean
    assert len(report.clean_streak) == 3
    assert _check(report, "Stage 2 clean-day streak").passed
    assert "| excluded |" in render_stage_readiness_markdown(report)


def test_a_silent_unexcluded_day_resets_the_streak(two_ledgers, pinned_clock):
    ledger, placebo, reports = two_ledgers
    _week(ledger, placebo, reports, pinned_clock, [WEEK[0], WEEK[2]], accepted=1)
    report = stage_readiness(ledger, placebo, IntradayConfig(), reports, WEEK[0], WEEK[2])
    assert any("no activity recorded" in r for r in report.days[1].clean_failures)
    assert report.clean_streak == [str(WEEK[2].date())]


def test_holds_are_not_accepted_trade_proposals(two_ledgers, pinned_clock):
    # 2026-09-29's three "accepted proposals" were all holds; a streak of those must
    # need five clean days, like a streak with no proposals at all.
    ledger, placebo, reports = two_ledgers
    for day in WEEK[:3]:
        _clean_day(ledger, placebo, day, reports, pinned_clock, holds=2)
    report = stage_readiness(ledger, placebo, IntradayConfig(), reports, WEEK[0], WEEK[2])
    assert [d.holds for d in report.days] == [2, 2, 2]
    assert [d.proposals_accepted for d in report.days] == [0, 0, 0]
    assert report.clean_streak_accepted == 0
    assert not _check(report, "Stage 2 clean-day streak").passed
    assert not _check(report, "The agent proposed trades").passed

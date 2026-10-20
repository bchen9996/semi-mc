"""`reports/YYYY-MM-DD/stage_readiness.md` -- the evidence behind a rollout-stage decision
(S4, `BRIEF_INTRADAY.md`'s "Rollout" and "Success criteria"), added 2026-09-27.

Every other S4 report looks at one day (`eod_report.py`) or the last five trading days
(`review.py::weekly_summary`). Promoting Stage 1 (shadow) to Stage 2 (real orders, half
budget) is a judgment about the *whole* shadow period, and nothing put that period's
evidence in one place. This module does, and only that: pure aggregation over the real
ledger and the placebo's shadow ledger, no broker, no market data, no LLM call -- the
same "mechanical here, judgment elsewhere" split `review.py` draws. It never flips a
config switch or recommends one; each rollout stage stays an owner decision (the brief:
"each gated on the owner, never automatic").

The evidence checks map onto the brief's own success criteria:

- "The full schedule runs unattended ... with no missed jobs and no unexplained
  incidents" -> per-day job evidence and error-severity incidents.
- "the placebo comparison appears" / T6 -> real vs. placebo net P&L, side by side.
- A minimum trade count before any P&L comparison means anything -> reuses
  `IntradayPlaybookConfig.evidence_min_trades`, the brief's own "enough evidence"
  threshold for a playbook change. A disclosed reuse, not a new researched number: the
  brief names no separate threshold for stage promotion.
- The per-day spend cap the brief says to recalibrate from shadow logging -> observed
  spend per day against `budget.max_cost_usd_per_day`.

**Clean trading days** (owner decision 2026-09-29, `IntradayReadinessConfig`) score each
day pass/fail on what the ledger can show: research finished before the open, the
open-batch Trader call ran within a grace period of the open, cycle checks covered the
whole session with no gap over the threshold, no error incidents, an eod report filed
under that date, spend within the cap, the placebo ran, and no intraday position was
still held at the close. A trade is not required on a given day -- `no_trade` after
looking is a legitimate outcome; proposals count across the streak instead. `carry` is
checked by its effect, since it records nothing on a day it has nothing to close: a
position still open at 16:00 New York whose latest entry was an intraday trade (or can't
be traced) is one carry should have closed, rebuilt from the ledger's fills (added
2026-10-05, after a GitHub runner outage skipped carry and left a TSM intraday trade open
overnight with nothing in the clean-day check to show it). Session edges are
the regular 09:30-16:00 New York hours; a half-day session will fail the coverage check,
which the owner judges by eye (no market calendar here, same as below).

**The Stage 3 rule** (owner decision 2026-10-16, `IntradayReadinessConfig.stage_2_started`
and the `stage_3_*` fields) gets its own section once Stage 2 has a start date: the
sessions run, reconciliation against the broker at every close, the latest clean-day
streak, closed trades since Stage 2 began, each main exit path seen on the real account,
and no day at the daily hard-loss limit. The exit-path check may be waived by the owner:
90-minute time stops rarely leave anything for carry or a Trader close.

**Missed days are inferred, not known.** The ledger records activity, not the
schedule, and this module has no market calendar (no broker dependency, same as
`weekly_summary`). A weekday with no activity at all is reported as "no activity
recorded", which is either a market holiday or a missed run -- the report says so
rather than guessing which.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pandas as pd

from ..config import IntradayConfig
from .ledger import IntradayLedger
from .reflect import round_trip_count

_EXCHANGE_TZ = "America/New_York"
_SESSION_OPEN = dt.time(9, 30)
_SESSION_CLOSE = dt.time(16, 0)

# Only these change a position. `hold`/`no_trade` pass validation trivially, so counting
# them as "accepted proposals" would let a streak of holds meet the 3-day rule -- found
# 2026-09-29, when all three of the day's "accepted proposals" were holds.
_TRADE_ACTIONS = frozenset({"open", "add", "trim", "close"})

# "AMD: no quote available" and "SMCI: no quote available" are one failure mode, not two.
_SYMBOL_PREFIX = re.compile(r"^[A-Z][A-Z.\-]{0,9}: ")


def incident_kind(message: str) -> str:
    """Groups incident messages by failure mode: strips a leading ``"SYMBOL: "`` and
    collapses ``"stopout:SYMBOL"`` to ``"stopout"`` and ``"tranche_skipped:ID ..."`` to
    ``"tranche_skipped"``."""
    if message.startswith("stopout:"):
        return "stopout"
    if message.startswith("tranche_skipped:"):
        return "tranche_skipped"
    return _SYMBOL_PREFIX.sub("", message)


@dataclass(frozen=True)
class DayEvidence:
    """What the ledger shows actually happened on one weekday. ``proposals_accepted``/
    ``proposals_rejected`` count trade actions only (``_TRADE_ACTIONS``); ``holds`` counts
    the Trader's ``hold``/``no_trade`` answers."""

    date: str
    research_brief: bool
    watchlist_candidates: int
    trader_calls: int
    proposals_accepted: int
    proposals_rejected: int
    monitor_checks: int
    max_monitor_gap_minutes: float | None
    first_activity_utc: str | None
    spend_usd: float
    incidents_error: int
    incidents_warning: int
    eod_report: bool
    holds: int = 0
    placebo_ran: bool = False
    # True when the owner listed this date in `readiness.excluded_days`: it neither counts
    # toward nor resets the clean-day streak.
    excluded: bool = False
    # Why this day is not clean; empty means clean. Always empty for an excluded day.
    clean_failures: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.excluded and not self.clean_failures

    @property
    def any_activity(self) -> bool:
        # An incident counts: a job that ran and declined at pretrade still ran.
        return (
            self.research_brief
            or self.trader_calls > 0
            or self.monitor_checks > 0
            or self.proposals_accepted + self.proposals_rejected + self.holds > 0
            or self.incidents_error + self.incidents_warning > 0
        )


@dataclass(frozen=True)
class SleeveResult:
    closed_trades: int
    pnl_gross: float
    pnl_net: float
    win_rate: float | None


@dataclass(frozen=True)
class EvidenceCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class StageReadiness:
    since: pd.Timestamp
    as_of: pd.Timestamp
    days: list[DayEvidence]
    agent: SleeveResult
    placebo: SleeveResult | None
    rejection_reasons: list[tuple[str, int]]
    incident_kinds: list[tuple[str, str, int]]
    spend_cap_usd: float
    checks: list[EvidenceCheck] = field(default_factory=list)
    # The consecutive clean days ending at the most recent non-excluded day, oldest first,
    # and how many proposals were accepted across them.
    clean_streak: list[str] = field(default_factory=list)
    clean_streak_accepted: int = 0
    # Empty unless `readiness.stage_2_started` is set and within the window.
    stage_3_checks: list[EvidenceCheck] = field(default_factory=list)


def _sleeve_result(ledger: IntradayLedger, reflections: list[dict[str, Any]]) -> SleeveResult:
    """`closed_trades` counts positions closed, flat to flat, not exits (since
    2026-10-20, `reflect.py::round_trip_count`); the P&L and win rate are per exit."""
    nets = [r["content"].get("pnl_net") or 0.0 for r in reflections]
    return SleeveResult(
        closed_trades=round_trip_count(ledger, reflections),
        pnl_gross=sum(r["content"].get("pnl_gross") or 0.0 for r in reflections),
        pnl_net=sum(nets),
        win_rate=sum(1 for n in nets if n > 0) / len(nets) if nets else None,
    )


def _in_window(value: str, since: pd.Timestamp, as_of: pd.Timestamp) -> bool:
    day = pd.Timestamp(value).normalize()
    return since <= day <= as_of


def _utc(value: str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _session_edge(day: pd.Timestamp, at: dt.time) -> pd.Timestamp:
    return pd.Timestamp(dt.datetime.combine(day.date(), at)).tz_localize(_EXCHANGE_TZ)


def _clean_failures(
    ledger: IntradayLedger,
    config: IntradayConfig,
    day: pd.Timestamp,
    evidence: DayEvidence,
) -> tuple[str, ...]:
    """Every reason ``day`` is not a clean trading day (`IntradayReadinessConfig`)."""
    rules = config.readiness
    local = config.schedule.timezone
    if not evidence.any_activity:
        return ("no activity recorded (a holiday belongs in readiness.excluded_days)",)

    def at_local(stamp: pd.Timestamp) -> str:
        return stamp.tz_convert(local).strftime("%H:%M")

    open_at = _session_edge(day, _SESSION_OPEN)
    close_at = _session_edge(day, _SESSION_CLOSE)
    failures: list[str] = []

    briefs = [_utc(b["created_at"]) for b in ledger.briefs_for(day)]
    if not briefs:
        failures.append("no research brief")
    elif min(briefs) >= open_at:
        failures.append(f"research finished at {at_local(min(briefs))}, after the open")

    open_calls = [
        _utc(c["created_at"])
        for c in ledger.model_calls_for(day)
        if c["cycle"] == "open_batch" and c["role"] == "trader"
    ]
    grace = pd.Timedelta(minutes=rules.open_batch_grace_minutes)
    if not open_calls:
        failures.append("no open-batch Trader call")
    elif min(open_calls) > open_at + grace:
        failures.append(
            f"open-batch Trader call at {at_local(min(open_calls))}, more than "
            f"{rules.open_batch_grace_minutes:g} min after the open"
        )

    # Recomputed from `checked_at` rather than read from `gap_seconds`, which spans every
    # check that day -- an evening test run would otherwise count as a session gap.
    edge = pd.Timedelta(minutes=rules.session_edge_minutes)
    checks = sorted(
        stamp
        for stamp in (_utc(c["checked_at"]) for c in ledger.monitor_checks_for(day))
        if open_at <= stamp <= close_at
    )
    if not checks:
        failures.append("no cycle ran during the session")
    else:
        if checks[0] > open_at + edge:
            failures.append(f"first cycle check at {at_local(checks[0])}, late in the morning")
        if checks[-1] < close_at - edge:
            failures.append(f"last cycle check at {at_local(checks[-1])}, early in the afternoon")
        gaps = [(b - a).total_seconds() / 60.0 for a, b in zip(checks, checks[1:], strict=False)]
        if gaps and max(gaps) > rules.max_cycle_gap_minutes:
            failures.append(
                f"a {max(gaps):.0f}-min gap between cycle checks "
                f"(max {rules.max_cycle_gap_minutes:g})"
            )

    if evidence.incidents_error:
        failures.append(f"{evidence.incidents_error} error incident(s)")
    if not evidence.eod_report:
        failures.append("no eod report filed under this date")
    cap = config.budget.max_cost_usd_per_day
    if evidence.spend_usd > cap:
        failures.append(f"spend ${evidence.spend_usd:.2f} over the ${cap:.2f} cap")
    if not evidence.placebo_ran:
        failures.append("the placebo did not run")
    held = _intraday_positions_at(ledger, close_at)
    if held:
        failures.append(
            f"{', '.join(held)} still held at the close on an intraday thesis; carry should "
            "have closed it"
        )
    return tuple(failures)


def _intraday_positions_at(ledger: IntradayLedger, moment: pd.Timestamp) -> list[str]:
    """Symbols the ledger's fills show still held at ``moment`` whose latest entry's
    proposal has no multi-day or multi-week horizon: what `jobs.py::run_carry` closes at
    14:45 Central, by the same rule (an untraceable horizon is closed too)."""
    net: dict[str, float] = {}
    latest_entry: dict[str, int | None] = {}
    for fill in ledger.fills_with_orders():
        if _utc(fill["filled_at"]) > moment:
            continue
        symbol = fill["symbol"]
        signed = fill["qty"] if fill["side"] == "buy" else -fill["qty"]
        net[symbol] = net.get(symbol, 0.0) + signed
        if fill["leg"] == "entry":
            latest_entry[symbol] = fill["proposal_id"]
    held: list[str] = []
    for symbol, qty in sorted(net.items()):
        if abs(qty) < 1e-9:
            continue
        proposal_id = latest_entry.get(symbol)
        proposal = ledger.get_proposal(proposal_id) if proposal_id is not None else None
        horizon = proposal["payload"].get("horizon") if proposal is not None else None
        if horizon not in ("multi-day", "multi-week"):
            held.append(symbol)
    return held


def _day_evidence(
    ledger: IntradayLedger,
    placebo_ledger: IntradayLedger | None,
    config: IntradayConfig,
    day: pd.Timestamp,
    reports_dir: Path,
) -> DayEvidence:
    watchlist = ledger.watchlist_for(day)
    calls = ledger.model_calls_for(day)
    proposals = ledger.proposals_for(day)
    checks = ledger.monitor_checks_for(day)
    incidents = ledger.incidents_for(day)
    trades = [p for p in proposals if p["action"] in _TRADE_ACTIONS]

    gaps = [c["gap_seconds"] / 60.0 for c in checks if c["gap_seconds"] is not None]
    timestamps = [c["created_at"] for c in calls] + [i["created_at"] for i in incidents]
    first = min((pd.Timestamp(t) for t in timestamps), default=None)

    evidence = DayEvidence(
        date=str(day.date()),
        research_brief=bool(ledger.briefs_for(day)),
        watchlist_candidates=len(watchlist),
        trader_calls=sum(1 for c in calls if c["role"] == "trader"),
        proposals_accepted=sum(1 for p in trades if p["status"] == "accepted"),
        proposals_rejected=sum(1 for p in trades if p["status"] == "rejected"),
        monitor_checks=len(checks),
        max_monitor_gap_minutes=max(gaps) if gaps else None,
        first_activity_utc=first.tz_convert("UTC").strftime("%H:%M") if first is not None else None,
        spend_usd=sum(c["cost_usd"] for c in calls),
        incidents_error=sum(1 for i in incidents if i["severity"] == "error"),
        incidents_warning=sum(1 for i in incidents if i["severity"] == "warning"),
        eod_report=(reports_dir / str(day.date()) / "eod.md").exists(),
        holds=len(proposals) - len(trades),
        # The placebo sets its session-open equity on its first cycle of the day, whether
        # or not it finds a candidate to trade.
        placebo_ran=placebo_ledger is not None
        and placebo_ledger.session_open_equity(day) is not None,
    )
    if day.date() in set(config.readiness.excluded_days):
        return replace(evidence, excluded=True)
    return replace(evidence, clean_failures=_clean_failures(ledger, config, day, evidence))


def _clean_streak(days: list[DayEvidence]) -> list[DayEvidence]:
    """Consecutive clean days counting back from the latest; excluded days are skipped."""
    streak: list[DayEvidence] = []
    for day in reversed(days):
        if day.excluded:
            continue
        if not day.clean:
            break
        streak.append(day)
    return streak[::-1]


def stage_readiness(
    ledger: IntradayLedger,
    placebo_ledger: IntradayLedger | None,
    config: IntradayConfig,
    reports_dir: Path,
    since: pd.Timestamp,
    as_of: pd.Timestamp,
) -> StageReadiness:
    """Aggregates every weekday in ``[since, as_of]``. ``placebo_ledger`` may be ``None``
    (the placebo's file doesn't exist yet); the comparison is then reported as
    unavailable, not zero."""
    since = pd.Timestamp(since).normalize()
    as_of = pd.Timestamp(as_of).normalize()
    weekdays = pd.bdate_range(since, as_of)
    days = [_day_evidence(ledger, placebo_ledger, config, day, reports_dir) for day in weekdays]

    agent_reflections = [
        r for r in ledger.all_reflections() if _in_window(r["as_of"], since, as_of)
    ]
    agent = _sleeve_result(ledger, agent_reflections)
    placebo = None
    if placebo_ledger is not None:
        placebo = _sleeve_result(
            placebo_ledger,
            [r for r in placebo_ledger.all_reflections() if _in_window(r["as_of"], since, as_of)],
        )

    reasons: Counter[str] = Counter()
    incident_counts: Counter[tuple[str, str]] = Counter()
    for day in weekdays:
        for row in ledger.proposals_for(day):
            if row["status"] == "rejected" and row.get("reason"):
                # One rejection can carry several "; "-joined validator failures.
                for reason in str(row["reason"]).split("; "):
                    reasons[reason] += 1
        for row in ledger.incidents_for(day):
            incident_counts[(row["severity"], incident_kind(row["message"]))] += 1

    streak = _clean_streak(days)
    report = StageReadiness(
        since=since,
        as_of=as_of,
        days=days,
        agent=agent,
        placebo=placebo,
        rejection_reasons=reasons.most_common(),
        incident_kinds=[(sev, kind, n) for (sev, kind), n in incident_counts.most_common()],
        spend_cap_usd=config.budget.max_cost_usd_per_day,
        clean_streak=[d.date for d in streak],
        clean_streak_accepted=sum(d.proposals_accepted for d in streak),
    )
    report.checks.extend(_evidence_checks(report, config))
    report.stage_3_checks.extend(_stage_3_checks(report, ledger, config))
    return report


# The main ways a real position ends, by reflection `close_reason`: a stop (resting at the
# broker or the monitor's), a time stop, and carry or the Trader's own close or trim.
_EXIT_PATHS = (
    ("a stop", ("invalidation",)),
    ("a time stop", ("time_stop",)),
    ("carry or a Trader close", ("carry", "trader_close", "trader_trim")),
)


def _stage_3_checks(
    report: StageReadiness, ledger: IntradayLedger, config: IntradayConfig
) -> list[EvidenceCheck]:
    """The owner's Stage 2 -> 3 rule (`IntradayReadinessConfig`), over the days from
    `stage_2_started` to the report's `as_of`."""
    rules = config.readiness
    if rules.stage_2_started is None:
        return []
    start = pd.Timestamp(rules.stage_2_started)
    if start > report.as_of:
        return []
    stage_days = [d for d in report.days if pd.Timestamp(d.date) >= start]
    sessions = [d for d in stage_days if d.any_activity]

    checks = [
        EvidenceCheck(
            f"Stage 2 has run {rules.stage_3_sessions} sessions",
            len(sessions) >= rules.stage_3_sessions,
            f"{len(sessions)} since {start.date()}",
        )
    ]

    by_day: dict[str, dict[str, Any]] = {}
    for row in ledger.reconciliations():
        day = str(pd.Timestamp(row["as_of"]).date())
        if day >= str(start.date()):
            by_day[day] = row  # the day's latest
    if not by_day:
        checks.append(
            EvidenceCheck(
                "The ledger matched the account at every close", False, "no reconciliation yet"
            )
        )
    else:
        first = min(by_day)
        due = [d.date for d in sessions if d.date >= first]
        missing = [day for day in due if day not in by_day]
        mismatched = [day for day in due if day in by_day and by_day[day]["status"] != "ok"]
        problems = [f"{day} not reconciled" for day in missing] + [
            f"{day} {by_day[day]['status']}" for day in mismatched
        ]
        checks.append(
            EvidenceCheck(
                "The ledger matched the account at every close",
                not problems,
                "; ".join(problems) or f"{len(due)} day(s) ok since {first}",
            )
        )

    streak, accepted = len(report.clean_streak), report.clean_streak_accepted
    checks.append(
        EvidenceCheck(
            f"The last {rules.clean_days_required} days are clean and include accepted trades",
            streak >= rules.clean_days_required and accepted > 0,
            f"{streak} consecutive clean day(s), {accepted} accepted trade proposal(s)",
        )
    )

    reflections = [
        r for r in ledger.all_reflections() if _in_window(r["as_of"], start, report.as_of)
    ]
    # Positions closed, not exits: a position closed in two steps is one trade.
    closed = round_trip_count(ledger, reflections)
    exits = f" ({len(reflections)} exits)" if len(reflections) != closed else ""
    checks.append(
        EvidenceCheck(
            f"At least {rules.stage_3_min_closed_trades} closed trades since Stage 2 began",
            closed >= rules.stage_3_min_closed_trades,
            f"{closed} closed{exits}",
        )
    )

    reasons = Counter(r["content"].get("close_reason") for r in reflections)
    seen = {label: sum(reasons[r] for r in keys) for label, keys in _EXIT_PATHS}
    checks.append(
        EvidenceCheck(
            "Each main exit path has worked on the real account (the last may be waived)",
            all(seen.values()),
            ", ".join(f"{label}: {n}" for label, n in seen.items()),
        )
    )

    hard = config.risk.daily_loss_hard_pct
    breaches: list[str] = []
    for d in stage_days:
        day = pd.Timestamp(d.date)
        realized = sum(
            r["content"].get("pnl_gross") or 0.0
            for r in reflections
            if _in_window(r["as_of"], day, day)
        )
        equity = ledger.session_open_equity(day)
        rejected = any(
            "daily_loss_hard_pct" in str(p.get("reason") or "") for p in ledger.proposals_for(day)
        )
        if rejected or (equity and realized / equity <= -hard):
            breaches.append(d.date)
    checks.append(
        EvidenceCheck(
            f"No day reached the daily hard-loss limit ({hard:.0%})",
            not breaches,
            ", ".join(breaches) or "none",
        )
    )
    return checks


def _evidence_checks(report: StageReadiness, config: IntradayConfig) -> list[EvidenceCheck]:
    active = [d for d in report.days if d.any_activity]
    silent = [d.date for d in report.days if not d.any_activity]
    min_trades = config.playbook.evidence_min_trades

    checks = [
        EvidenceCheck(
            "Activity recorded every weekday",
            not silent,
            f"{len(active)}/{len(report.days)} weekdays"
            + (f"; none recorded on {', '.join(silent)} (holiday or missed run)" if silent else ""),
        ),
        EvidenceCheck(
            "Research and the Trader ran on every active day",
            all(d.research_brief and d.trader_calls > 0 for d in active) and bool(active),
            ", ".join(d.date for d in active if not (d.research_brief and d.trader_calls > 0))
            or "every active day",
        ),
        EvidenceCheck(
            "End-of-day report written on every active day",
            all(d.eod_report for d in active) and bool(active),
            ", ".join(d.date for d in active if not d.eod_report) or "every active day",
        ),
        EvidenceCheck(
            "No error-severity incidents",
            sum(d.incidents_error for d in report.days) == 0,
            f"{sum(d.incidents_error for d in report.days)} error incident(s)",
        ),
        EvidenceCheck(
            "The agent proposed trades",
            sum(d.proposals_accepted + d.proposals_rejected for d in report.days) > 0,
            f"{sum(d.proposals_accepted for d in report.days)} accepted, "
            f"{sum(d.proposals_rejected for d in report.days)} rejected "
            f"(plus {sum(d.holds for d in report.days)} hold/no-trade answers)",
        ),
        EvidenceCheck(
            f"At least {min_trades} closed trades (playbook evidence threshold)",
            report.agent.closed_trades >= min_trades,
            f"{report.agent.closed_trades} closed",
        ),
        EvidenceCheck(
            "Placebo comparison available",
            report.placebo is not None and report.placebo.closed_trades > 0,
            "no placebo ledger"
            if report.placebo is None
            else f"{report.placebo.closed_trades} placebo trades closed",
        ),
    ]
    rules = config.readiness
    streak, accepted = len(report.clean_streak), report.clean_streak_accepted
    checks.append(
        EvidenceCheck(
            "Stage 2 clean-day streak",
            streak >= rules.clean_days_if_sparse
            or (streak >= rules.clean_days_required and accepted > 0),
            f"{streak} consecutive clean day(s)"
            + (f" ending {report.clean_streak[-1]}" if streak else "")
            + f", {accepted} accepted trade proposal(s) in the streak; needs "
            f"{rules.clean_days_required} with accepted trade proposals (read them before "
            f"promoting) or {rules.clean_days_if_sparse} without",
        )
    )
    peak = max((d.spend_usd for d in report.days), default=0.0)
    checks.append(
        EvidenceCheck(
            "Peak daily model spend within the cap",
            peak <= report.spend_cap_usd,
            f"peak ${peak:.2f} vs ${report.spend_cap_usd:.2f} cap",
        )
    )
    return checks


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _sleeve_line(label: str, result: SleeveResult) -> str:
    win = f"{result.win_rate:.0%}" if result.win_rate is not None else "n/a"
    return (
        f"| {label} | {result.closed_trades} | {_money(result.pnl_gross)} | "
        f"{_money(result.pnl_net)} | {win} |"
    )


def render_stage_readiness_markdown(report: StageReadiness) -> str:
    lines = [
        f"# S4 stage readiness: {report.since.date()} to {report.as_of.date()}",
        "",
        "Evidence for the next rollout-stage decision. Mechanical only -- it never flips a",
        "switch or recommends one; promotion stays an owner decision (BRIEF_INTRADAY.md).",
        "",
        "## Evidence checks",
        "",
    ]
    passed = sum(1 for c in report.checks if c.passed)
    lines.append(f"{passed}/{len(report.checks)} passed.")
    lines.append("")
    for check in report.checks:
        mark = "PASS" if check.passed else "FAIL"
        lines.append(f"- **{mark}** {check.name} -- {check.detail}")

    if report.stage_3_checks:
        lines += ["", "## Stage 3 rule (owner decision 2026-10-16)", ""]
        passed_3 = sum(1 for c in report.stage_3_checks if c.passed)
        lines.append(f"{passed_3}/{len(report.stage_3_checks)} passed.")
        lines.append("")
        for check in report.stage_3_checks:
            mark = "PASS" if check.passed else "FAIL"
            lines.append(f"- **{mark}** {check.name} -- {check.detail}")

    lines += ["", "## Stage 2 clean days", ""]
    lines.append(
        "A clean day: research before the open, the open batch on time, cycles across the "
        "whole session, no error incidents, an eod report, spend within the cap, and the "
        "placebo ran. A trade is not required on any one day."
    )
    lines.append("")
    lines.append("| date | clean | why not |")
    lines.append("|---|---|---|")
    for d in report.days:
        if d.excluded:
            lines.append(f"| {d.date} | excluded | listed in readiness.excluded_days |")
        else:
            why = "; ".join(d.clean_failures) or "-"
            lines.append(f"| {d.date} | {'yes' if d.clean else 'no'} | {why} |")

    lines += ["", "## Agent vs. placebo (closed trades in the window)", ""]
    lines.append("| sleeve | closed trades | P&L gross | P&L net | win rate |")
    lines.append("|---|---|---|---|---|")
    lines.append(_sleeve_line("agent", report.agent))
    if report.placebo is not None:
        lines.append(_sleeve_line("placebo", report.placebo))
    else:
        lines.append("| placebo | not available | | | |")
    lines.append("")
    lines.append(
        "A few days of P&L is noise (the brief's first principle); read this next to the "
        "closed-trade count, not on its own."
    )

    lines += ["", "## Day by day", ""]
    lines.append(
        "| date | research | watchlist | trader calls | accepted | rejected | holds | "
        "monitor checks | longest gap (m) | first activity (UTC) | spend ($) | "
        "errors | warnings | eod report |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for d in report.days:
        if not d.any_activity:
            lines.append(f"| {d.date} | no activity recorded (holiday or missed run) |" + " |" * 12)
            continue
        gap = f"{d.max_monitor_gap_minutes:.0f}" if d.max_monitor_gap_minutes is not None else "-"
        lines.append(
            f"| {d.date} | {'yes' if d.research_brief else 'no'} | {d.watchlist_candidates} | "
            f"{d.trader_calls} | {d.proposals_accepted} | {d.proposals_rejected} | "
            f"{d.holds} | {d.monitor_checks} | {gap} | {d.first_activity_utc or '-'} | "
            f"{d.spend_usd:.2f} | {d.incidents_error} | {d.incidents_warning} | "
            f"{'yes' if d.eod_report else 'no'} |"
        )

    lines += ["", "## Why proposals were rejected", ""]
    if report.rejection_reasons:
        for reason, n in report.rejection_reasons:
            lines.append(f"- {n} x {reason}")
    else:
        lines.append("No rejections in the window.")

    lines += ["", "## Incidents by kind", ""]
    if report.incident_kinds:
        for severity, kind, n in report.incident_kinds:
            lines.append(f"- {n} x [{severity}] {kind}")
    else:
        lines.append("No incidents in the window.")

    spends = [d.spend_usd for d in report.days if d.any_activity]
    lines += ["", "## Model spend", ""]
    if spends:
        lines.append(
            f"- Total ${sum(spends):.2f} over {len(spends)} active day(s); mean "
            f"${sum(spends) / len(spends):.2f}/day, peak ${max(spends):.2f}/day"
        )
    else:
        lines.append("- No model calls in the window.")
    lines.append(
        f"- Configured cap: ${report.spend_cap_usd:.2f}/day "
        "(a placeholder; the brief says to recalibrate it from this logging)"
    )
    return "\n".join(lines) + "\n"

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

**Missed days are inferred, not known.** The ledger records activity, not the
schedule, and this module has no market calendar (no broker dependency, same as
`weekly_summary`). A weekday with no activity at all is reported as "no activity
recorded", which is either a market holiday or a missed run -- the report says so
rather than guessing which.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from ..config import IntradayConfig
from .ledger import IntradayLedger

# "AMD: no quote available" and "SMCI: no quote available" are one failure mode, not two.
_SYMBOL_PREFIX = re.compile(r"^[A-Z][A-Z.\-]{0,9}: ")


def incident_kind(message: str) -> str:
    """Groups incident messages by failure mode: strips a leading ``"SYMBOL: "`` and
    collapses ``"stopout:SYMBOL"`` to ``"stopout"``."""
    if message.startswith("stopout:"):
        return "stopout"
    return _SYMBOL_PREFIX.sub("", message)


@dataclass(frozen=True)
class DayEvidence:
    """What the ledger shows actually happened on one weekday."""

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

    @property
    def any_activity(self) -> bool:
        # An incident counts: a job that ran and declined at pretrade still ran.
        return (
            self.research_brief
            or self.trader_calls > 0
            or self.monitor_checks > 0
            or self.proposals_accepted + self.proposals_rejected > 0
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


def _sleeve_result(reflections: list[dict[str, Any]]) -> SleeveResult:
    nets = [r["content"].get("pnl_net") or 0.0 for r in reflections]
    return SleeveResult(
        closed_trades=len(reflections),
        pnl_gross=sum(r["content"].get("pnl_gross") or 0.0 for r in reflections),
        pnl_net=sum(nets),
        win_rate=sum(1 for n in nets if n > 0) / len(nets) if nets else None,
    )


def _in_window(value: str, since: pd.Timestamp, as_of: pd.Timestamp) -> bool:
    day = pd.Timestamp(value).normalize()
    return since <= day <= as_of


def _day_evidence(ledger: IntradayLedger, day: pd.Timestamp, reports_dir: Path) -> DayEvidence:
    watchlist = ledger.watchlist_for(day)
    calls = ledger.model_calls_for(day)
    proposals = ledger.proposals_for(day)
    checks = ledger.monitor_checks_for(day)
    incidents = ledger.incidents_for(day)

    gaps = [c["gap_seconds"] / 60.0 for c in checks if c["gap_seconds"] is not None]
    timestamps = [c["created_at"] for c in calls] + [i["created_at"] for i in incidents]
    first = min((pd.Timestamp(t) for t in timestamps), default=None)

    return DayEvidence(
        date=str(day.date()),
        research_brief=bool(ledger.briefs_for(day)),
        watchlist_candidates=len(watchlist),
        trader_calls=sum(1 for c in calls if c["role"] == "trader"),
        proposals_accepted=sum(1 for p in proposals if p["status"] == "accepted"),
        proposals_rejected=sum(1 for p in proposals if p["status"] == "rejected"),
        monitor_checks=len(checks),
        max_monitor_gap_minutes=max(gaps) if gaps else None,
        first_activity_utc=first.tz_convert("UTC").strftime("%H:%M") if first is not None else None,
        spend_usd=sum(c["cost_usd"] for c in calls),
        incidents_error=sum(1 for i in incidents if i["severity"] == "error"),
        incidents_warning=sum(1 for i in incidents if i["severity"] == "warning"),
        eod_report=(reports_dir / str(day.date()) / "eod.md").exists(),
    )


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
    days = [_day_evidence(ledger, day, reports_dir) for day in weekdays]

    agent_reflections = [
        r for r in ledger.all_reflections() if _in_window(r["as_of"], since, as_of)
    ]
    agent = _sleeve_result(agent_reflections)
    placebo = None
    if placebo_ledger is not None:
        placebo = _sleeve_result(
            [r for r in placebo_ledger.all_reflections() if _in_window(r["as_of"], since, as_of)]
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

    report = StageReadiness(
        since=since,
        as_of=as_of,
        days=days,
        agent=agent,
        placebo=placebo,
        rejection_reasons=reasons.most_common(),
        incident_kinds=[(sev, kind, n) for (sev, kind), n in incident_counts.most_common()],
        spend_cap_usd=config.budget.max_cost_usd_per_day,
    )
    report.checks.extend(_evidence_checks(report, config))
    return report


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
            "The agent produced proposals",
            sum(d.proposals_accepted + d.proposals_rejected for d in report.days) > 0,
            f"{sum(d.proposals_accepted for d in report.days)} accepted, "
            f"{sum(d.proposals_rejected for d in report.days)} rejected",
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
        "| date | research | watchlist | trader calls | accepted | rejected | "
        "monitor checks | longest gap (m) | first activity (UTC) | spend ($) | "
        "errors | warnings | eod report |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for d in report.days:
        if not d.any_activity:
            lines.append(f"| {d.date} | no activity recorded (holiday or missed run) |" + " |" * 11)
            continue
        gap = f"{d.max_monitor_gap_minutes:.0f}" if d.max_monitor_gap_minutes is not None else "-"
        lines.append(
            f"| {d.date} | {'yes' if d.research_brief else 'no'} | {d.watchlist_candidates} | "
            f"{d.trader_calls} | {d.proposals_accepted} | {d.proposals_rejected} | "
            f"{d.monitor_checks} | {gap} | {d.first_activity_utc or '-'} | "
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

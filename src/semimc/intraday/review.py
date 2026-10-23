"""S4's mechanical scoring, attribution, and calibration (`BRIEF_INTRADAY.md`'s Files
section names this file explicitly: "scoring, attribution, calibration"), Phase 9b.

Phase 9a already drew the "compute what's mechanical, reserve the model call for
judgment" line once, for a single trade's own reflection (P&L, invalidation-respected,
time-in-trade, `reflect.py`). This module draws the identical line again at the
whole-day level for the Reviewer's process scorecard and attribution sections: every
number here is pure computation over ledger data already on hand, so `jobs.py::
run_eod_review`'s Reviewer call narrates and grades these numbers rather than
recomputing them (`prompts/reviewer.md` says so explicitly).

:func:`day_attribution` is a disclosed simplification, not a fitted regression --
`BRIEF_INTRADAY.md` asks to "regress the day's returns on SOXX," but nothing in the
ledger retains intraday SOXX price *history* (only `latest_quote` at call time), so a
genuine per-trade regression against SOXX's return over that trade's own holding window
isn't buildable from data this project keeps today. See its own docstring for the
day-level approximation used instead -- a labeled interpretation, not a fabricated
number, the same honesty convention `eod_report.py`'s "pessimistic P&L" already uses.

:func:`weekly_summary`/:func:`render_weekly_review_markdown` (Phase 10) extend the same
"mechanical here, judgment elsewhere" split one level further: `weekly_review` has no
named agent in the brief's schedule table at all (confirmed directly before designing
it), so there is no judgment call to reserve -- these functions are the entire job, not
a precursor to one. See `intraday/playbook.py`'s module docstring for where the actual
accept/reject/retire decision lives instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from ..config import IntradayConfig
from .jobs import turnover_so_far_usd
from .ledger import IntradayLedger
from .proposals import OPEN_OR_ADD_ACTIONS

_CALIBRATION_BUCKETS = (0.0, 0.25, 0.5, 0.75, 1.0)


def confidence_calibration(
    ledger: IntradayLedger, buckets: tuple[float, ...] = _CALIBRATION_BUCKETS
) -> list[dict[str, Any]]:
    """A running calibration curve across *every* reflection ever recorded (all trading
    days, via `ledger.all_reflections()`) -- "a running calibration curve," per the
    brief, not a one-day snapshot. Stated confidence bucketed against realized win rate
    (`pnl_net > 0`). A reflection with no stated confidence is excluded, not counted as a
    0 -- an absent value is not evidence of miscalibration.
    """
    rows = [r for r in ledger.all_reflections() if r["content"].get("confidence") is not None]
    out: list[dict[str, Any]] = []
    for lo, hi in zip(buckets, buckets[1:], strict=False):
        in_bucket = [
            r
            for r in rows
            if lo <= r["content"]["confidence"] < hi
            or (hi == buckets[-1] and r["content"]["confidence"] == hi)
        ]
        if not in_bucket:
            continue
        wins = sum(1 for r in in_bucket if (r["content"].get("pnl_net") or 0.0) > 0)
        out.append(
            {"confidence_range": [lo, hi], "n": len(in_bucket), "win_rate": wins / len(in_bucket)}
        )
    return out


def process_scorecard(
    ledger: IntradayLedger, config: IntradayConfig, as_of: pd.Timestamp
) -> dict[str, Any]:
    """Every number the brief's "process scorecard" bullet names, computed mechanically:
    rejection rate and reasons, budget discipline (new positions / round trips / turnover
    against the same caps `build_trader_context`'s own `budget_remaining` reads),
    invalidation adherence, today's incidents, and the running confidence-calibration
    curve. Uses `ledger.session_open_equity(as_of)` for the turnover cap's denominator --
    no broker call, matching `reviewer.md`'s own "no broker access" and keeping this job
    as light as `eod_report.py`'s existing benchmark gathering.
    """
    todays = ledger.proposals_for(as_of)
    # Less any whose entries were canceled with nothing filled (2026-10-22).
    accepted = ledger.counted_proposals_for(as_of)
    # An add counts toward its symbol's round trips, not new positions (proposals.py).
    accepted_opens = [row for row in accepted if row["action"] == "open"]
    rejected = [row for row in todays if row["status"] == "rejected"]

    round_trips_by_symbol: dict[str, int] = {}
    for row in accepted:
        if row["action"] in OPEN_OR_ADD_ACTIONS:
            round_trips_by_symbol[row["symbol"]] = round_trips_by_symbol.get(row["symbol"], 0) + 1

    equity = ledger.session_open_equity(as_of)
    risk = config.risk
    turnover_usd = turnover_so_far_usd(ledger, as_of, equity) if equity is not None else None
    turnover_cap_usd = risk.daily_turnover_pct * equity if equity is not None else None

    reflections_today = ledger.reflections_for(as_of)
    adherence_known = [
        r for r in reflections_today if r["content"].get("invalidation_respected") is not None
    ]
    invalidation_adherence_rate = (
        sum(1 for r in adherence_known if r["content"]["invalidation_respected"])
        / len(adherence_known)
        if adherence_known
        else None
    )

    return {
        "rejection_rate": len(rejected) / len(todays) if todays else 0.0,
        "rejection_reasons": [row["reason"] for row in rejected if row.get("reason")],
        "new_positions_used": len(accepted_opens),
        "new_positions_cap": risk.new_positions_per_day,
        "round_trips_by_symbol": round_trips_by_symbol,
        "round_trips_cap_per_symbol": risk.round_trips_per_symbol_per_day,
        "turnover_usd": turnover_usd,
        "turnover_cap_usd": turnover_cap_usd,
        "invalidation_adherence_rate": invalidation_adherence_rate,
        "calibration": confidence_calibration(ledger),
        "incidents": ledger.incidents_for(as_of),
    }


def day_attribution(
    reflections_today: list[dict[str, Any]], soxx_return: float | None
) -> dict[str, float | None]:
    """A day-level sector/stock-specific split, not a fitted regression (see module
    docstring). `sector` sums each closed position's signed entry-cost-basis notional
    (`content["qty"] * content["avg_entry_price"]`, the qty *this reflection's own event*
    exited -- correct for a trim too, not just a full close) times `soxx_return` -- a
    beta-of-1 approximation. `run_eod_review` passes SOXX's open-to-close move (since
    2026-10-19; before, the whole day's): S4 holds nothing overnight, so a gap is no part
    of its sector exposure. `stock_specific` is the residual (today's total
    `pnl_net` minus `sector`), folding selection and timing into one bucket rather than
    fabricating a further split neither the ledger nor a market-data feed supports yet
    (no intraday SOXX price history is retained). `options` is always 0.0 -- options
    remain off (rollout stages 4/5, not reached).
    """
    pnl_net_total = sum(r["content"].get("pnl_net") or 0.0 for r in reflections_today)
    if soxx_return is None or not reflections_today:
        return {"sector": None, "stock_specific": None, "options": 0.0}

    sector = 0.0
    for row in reflections_today:
        content = row["content"]
        qty = content.get("qty")
        avg_entry_price = content.get("avg_entry_price")
        if qty is None or avg_entry_price is None:
            continue
        sign = 1.0 if content.get("side") == "buy" else -1.0
        sector += sign * qty * avg_entry_price * soxx_return

    return {"sector": sector, "stock_specific": pnl_net_total - sector, "options": 0.0}


_WEEKLY_WINDOW_TRADING_DAYS = 5


def weekly_summary(
    ledger: IntradayLedger, config: IntradayConfig, as_of: pd.Timestamp
) -> dict[str, Any]:
    """Phase 10's `weekly_review` job: the most recent `_WEEKLY_WINDOW_TRADING_DAYS`
    distinct trading days on record (`ledger.distinct_trading_days()`, not calendar
    Mon-Fri) -- a disclosed simplification, same reasoning `day_attribution`'s own
    module docstring already uses: a short week (a holiday) naturally pulls in one extra
    prior day instead of under-reporting, and it needs no `Broker`/market-calendar
    dependency `weekly_review` would otherwise be the only job to need. `as_of` bounds
    the window from the future (only trading days on or before it count), so a report
    generated for a specific date is reproducible regardless of when it's re-run.

    Also reports two since-inception counters (total trading days, total reflections) --
    not a window statistic, but what a human reviewing a pending proposal's own stated
    evidence ("N trades over M days") needs to eyeball it against real totals, since
    that stated evidence is freeform prose this module deliberately never parses (see
    `intraday/playbook.py`'s module docstring).
    """
    today = pd.Timestamp(as_of).normalize()
    all_days = [d for d in ledger.distinct_trading_days() if pd.Timestamp(d) <= today]
    window_days = sorted(all_days)[-_WEEKLY_WINDOW_TRADING_DAYS:]

    reflections: list[dict[str, Any]] = []
    scorecards: list[dict[str, Any]] = []
    monitor_gaps_minutes: list[float] = []
    monitor_checks_count = 0
    for day in window_days:
        reflections.extend(ledger.reflections_for(day))
        scorecards.append(process_scorecard(ledger, config, day))
        checks = ledger.monitor_checks_for(day)
        monitor_checks_count += len(checks)
        monitor_gaps_minutes.extend(
            c["gap_seconds"] / 60.0 for c in checks if c["gap_seconds"] is not None
        )

    rejection_rates = [s["rejection_rate"] for s in scorecards]
    incidents_count = sum(len(s["incidents"]) for s in scorecards)

    return {
        "window_trading_days": window_days,
        "pnl_gross": sum(r["content"].get("pnl_gross") or 0.0 for r in reflections),
        "pnl_net": sum(r["content"].get("pnl_net") or 0.0 for r in reflections),
        "avg_rejection_rate": sum(rejection_rates) / len(rejection_rates)
        if rejection_rates
        else 0.0,
        "incidents": incidents_count,
        "calibration": confidence_calibration(ledger),
        "total_trading_days": len(all_days),
        "total_reflections": sum(
            1 for r in ledger.all_reflections() if pd.Timestamp(r["as_of"]) <= today
        ),
        # Cadence telemetry (found 2026-09-23, see IntradayLedger.record_monitor_check's
        # docstring) -- how often the mechanical monitor/cycle check actually ran this
        # week, not how often the cron was configured to.
        "monitor_checks": monitor_checks_count,
        "monitor_avg_gap_minutes": sum(monitor_gaps_minutes) / len(monitor_gaps_minutes)
        if monitor_gaps_minutes
        else None,
        "monitor_max_gap_minutes": max(monitor_gaps_minutes) if monitor_gaps_minutes else None,
    }


def render_weekly_review_markdown(
    summary: dict[str, Any],
    pending: list[Path],
    due_for_review: list[dict[str, Any]],
    as_of: pd.Timestamp,
) -> str:
    """`reports/YYYY-MM-DD/weekly_review.md` -- surfaces pending playbook proposals and
    entries due for re-review; never accepts/rejects/retires anything itself (that's
    `intraday/playbook.py`'s own, separately human-invoked action -- see
    `jobs.py::run_weekly_review`'s docstring for why)."""
    lines = [f"# S4 weekly review: {pd.Timestamp(as_of).date()}", ""]

    lines.append("## This week")
    days = summary["window_trading_days"]
    lines.append(f"- Trading days covered: {', '.join(days) if days else 'none yet'}")
    lines.append(f"- P&L gross: {summary['pnl_gross']:,.2f}")
    lines.append(f"- P&L net (of modeled cost): {summary['pnl_net']:,.2f}")
    lines.append(f"- Average rejection rate: {summary['avg_rejection_rate']:.1%}")
    lines.append(f"- Incidents: {summary['incidents']}")
    if summary["calibration"]:
        lines.append("- Calibration (all-time):")
        for bucket in summary["calibration"]:
            lo, hi = bucket["confidence_range"]
            lines.append(
                f"  - [{lo:.2f}, {hi:.2f}]: {bucket['win_rate']:.1%} win rate, n={bucket['n']}"
            )

    lines.append("")
    lines.append("## Monitor cadence")
    lines.append(f"- Checks this week: {summary['monitor_checks']}")
    if summary["monitor_avg_gap_minutes"] is not None:
        lines.append(f"- Average gap between checks: {summary['monitor_avg_gap_minutes']:.1f}m")
        lines.append(f"- Longest gap between checks: {summary['monitor_max_gap_minutes']:.1f}m")
    else:
        lines.append("- Not enough checks this week to measure a gap.")

    lines.append("")
    lines.append("## Since inception")
    lines.append(f"- Trading days run: {summary['total_trading_days']}")
    lines.append(f"- Reflections recorded: {summary['total_reflections']}")

    lines.append("")
    lines.append("## Pending playbook proposals")
    if pending:
        for path in pending:
            lines.append(f"- {path.name} -- `semimc intraday playbook accept/reject` to decide")
    else:
        lines.append("None pending.")

    lines.append("")
    lines.append("## Active entries due for re-review")
    if due_for_review:
        for row in due_for_review:
            review_date = pd.Timestamp(row["review_date"]).date()
            lines.append(f"- Entry {row['id']} (review date {review_date} has passed)")
    else:
        lines.append("None due.")

    return "\n".join(lines) + "\n"

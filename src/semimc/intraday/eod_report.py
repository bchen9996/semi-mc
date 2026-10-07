"""`reports/YYYY-MM-DD/eod.md`'s reporting skeleton (S4, `BRIEF_INTRADAY.md`'s "End of
day: report and self-improvement"), Phase 9a, extended in Phase 9b.

Mirrors `live/report.py::build_report`/`render_markdown` exactly: gathers everything
through `IntradayLedger`'s own read methods (plus whatever the caller already gathered
into `DayBenchmarks`/the new optional Reviewer-output fields), no SQL above this layer,
renders plain Markdown. This module deliberately never constructs or imports a client
(`AlpacaMarketDataClient`, `live.Ledger`, the placebo's own `FakeBroker` state) itself --
the caller (`jobs.py::run_eod_review`, or the CLI directly) gathers each optional piece
and hands it in, so a report with only some of that wiring available still renders
honestly with the rest marked "not available" rather than crashing.

Phase 9a built the blotter, the day's P&L, and three of the four benchmarks (SOXX,
S0, the placebo) -- everything computable without the Reviewer's own judgment -- and left
the closing market report, process scorecard, and proposed playbook changes as an
explicit "not yet built" placeholder, the same honesty convention `paper evaluate`'s
minimum-history caveat already uses. Phase 9b fills those in (via `jobs.py::
run_eod_review`'s Reviewer call) and finishes the fourth benchmark, "do nothing"
(:func:`do_nothing_return`) -- mechanical, so it needed no model call at all, just a
caller that hadn't been built yet.

P&L gross/net are summed directly from each closed trade's own already-computed
`reflect.py::record_mechanical_reflection` fields -- no separate computation, so this can
never drift out of sync with what each individual reflection reports. "Pessimistic" P&L
has no existing convention to reuse: `BRIEF_INTRADAY.md` cites it as matching "the ledger
conventions from the paper-trading brief," but `semis_paper_trading_brief.md` was never
committed to this repo (`CLAUDE.md`'s own opening line already flags this as a dangling
citation). Absent that source, this treats "pessimistic" as net P&L under double the
modeled cost assumption (a stand-in for worse-than-expected slippage) -- a defensible,
clearly-labeled interpretation, not a guess dressed up as the real thing.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class DayBenchmarks:
    soxx_return: float | None = None
    s0_return: float | None = None
    placebo_pnl: float | None = None
    do_nothing_return: float | None = None


@dataclass(frozen=True)
class MonitorCadence:
    """How often `check_invalidation_and_time_stops` actually ran today
    (`ledger.monitor_checks_for`), not how often the cron was configured to fire --
    found 2026-09-23 that these are very different numbers on this repo. Purely
    mechanical, computed unconditionally in `build_eod_report` like `proposals`/
    `reflections`, never optional/Reviewer-supplied the way `process_scorecard` is."""

    checks: int
    avg_gap_minutes: float | None
    max_gap_minutes: float | None


@dataclass(frozen=True)
class EodReport:
    as_of: pd.Timestamp
    proposals: list[dict[str, Any]]
    reflections: list[dict[str, Any]]
    pnl_gross: float
    pnl_net: float
    pnl_pessimistic: float
    benchmarks: DayBenchmarks
    monitor_cadence: MonitorCadence
    process_scorecard: dict[str, Any] | None = None
    process_scorecard_narrative: str | None = None
    attribution: dict[str, Any] | None = None
    attribution_narrative: str | None = None
    closing_market_report: str | None = None
    playbook_proposal: str | None = None
    # Set when the Reviewer was deliberately not called (a quiet day: nothing to grade)
    # -- rendered in place of each Reviewer-only section, so the report says why a
    # section is empty instead of the "not yet built" placeholder, which would be false.
    reviewer_note: str | None = None


def s0_day_return(paper_ledger: Any, as_of: pd.Timestamp) -> float | None:
    """S0's own realized equity return for `as_of`'s calendar date, from `live.Ledger`'s
    own snapshots (reused directly, no new computation) -- the most recent snapshot dated
    before today against the most recent snapshot dated today. `None` if either side is
    missing (S0 not enabled yet, or no mark has run today)."""
    today = pd.Timestamp(as_of).normalize()
    prior: dict[str, Any] | None = None
    latest_today: dict[str, Any] | None = None
    for row in paper_ledger.snapshots_for("s0"):
        row_date = pd.Timestamp(row["as_of"]).normalize()
        if row_date < today:
            prior = row
        elif row_date == today:
            latest_today = row
    if prior is None or latest_today is None or prior["equity"] <= 0:
        return None
    return (latest_today["equity"] - prior["equity"]) / prior["equity"]


def do_nothing_return(
    ledger: Any, market_data: Any, as_of: pd.Timestamp, now: dt.datetime
) -> float | None:
    """ "Do nothing" (hold the open batch untouched all day) -- the fourth of the brief's
    benchmarks for the day, and mechanical like the other three: marks every position the
    Trader's `open_batch` cycle actually opened today (`proposals.cycle ==
    "open_batch"`, `status == "accepted"`) to market at `now`'s quote against its own
    average entry price, ignoring whatever actually happened to it afterward (any
    trim/close/carry) -- exactly what "untouched all day" means. Expressed as a percent
    of `ledger.session_open_equity(as_of)`, the same denominator S0's own day return
    uses. `None` with no open-batch entries, no session-open-equity denominator, or no
    quote reachable for any of them.
    """
    from .proposals import OPEN_OR_ADD_ACTIONS
    from .reflect import _summed_fills

    equity = ledger.session_open_equity(as_of)
    if not equity:
        return None
    entries = [
        row
        for row in ledger.proposals_for(as_of)
        if row["cycle"] == "open_batch"
        and row["status"] == "accepted"
        and row["action"] in OPEN_OR_ADD_ACTIONS
    ]
    if not entries:
        return None

    total_pnl = 0.0
    any_priced = False
    for row in entries:
        entry_orders = ledger.orders_for_proposal(row["id"], leg="entry")
        qty, notional, _, _ = _summed_fills(
            ledger, [order["client_order_id"] for order in entry_orders]
        )
        if qty <= 0:
            continue
        quote = market_data.latest_quote(row["symbol"], at=now)
        if quote is None or quote.bid_price is None or quote.ask_price is None:
            continue
        mid = (quote.bid_price + quote.ask_price) / 2.0
        avg_entry_price = notional / qty
        sign = 1.0 if row["payload"].get("side") == "buy" else -1.0
        total_pnl += sign * (mid - avg_entry_price) * qty
        any_priced = True
    if not any_priced:
        return None
    return total_pnl / equity


def build_eod_report(
    ledger: Any,
    as_of: pd.Timestamp,
    benchmarks: DayBenchmarks | None = None,
    process_scorecard: dict[str, Any] | None = None,
    process_scorecard_narrative: str | None = None,
    attribution: dict[str, Any] | None = None,
    attribution_narrative: str | None = None,
    closing_market_report: str | None = None,
    playbook_proposal: str | None = None,
    reviewer_note: str | None = None,
) -> EodReport:
    proposals = ledger.proposals_for(as_of)
    reflections = ledger.reflections_for(as_of)
    pnl_gross = sum(r["content"].get("pnl_gross") or 0.0 for r in reflections)
    pnl_net = sum(r["content"].get("pnl_net") or 0.0 for r in reflections)
    modeled_cost = pnl_gross - pnl_net
    pnl_pessimistic = pnl_gross - 2 * modeled_cost

    checks = ledger.monitor_checks_for(as_of)
    gaps_minutes = [c["gap_seconds"] / 60.0 for c in checks if c["gap_seconds"] is not None]
    monitor_cadence = MonitorCadence(
        checks=len(checks),
        avg_gap_minutes=sum(gaps_minutes) / len(gaps_minutes) if gaps_minutes else None,
        max_gap_minutes=max(gaps_minutes) if gaps_minutes else None,
    )

    return EodReport(
        as_of=pd.Timestamp(as_of),
        proposals=proposals,
        reflections=reflections,
        pnl_gross=pnl_gross,
        pnl_net=pnl_net,
        pnl_pessimistic=pnl_pessimistic,
        benchmarks=benchmarks or DayBenchmarks(),
        monitor_cadence=monitor_cadence,
        process_scorecard=process_scorecard,
        process_scorecard_narrative=process_scorecard_narrative,
        attribution=attribution,
        attribution_narrative=attribution_narrative,
        closing_market_report=closing_market_report,
        playbook_proposal=playbook_proposal,
        reviewer_note=reviewer_note,
    )


def _pct(value: float | None) -> str:
    return f"{value:+.2%}" if value is not None else "not available"


def _money(value: float | None) -> str:
    return f"{value:,.2f}" if value is not None else "not available"


def render_eod_markdown(report: EodReport) -> str:
    lines = [f"# S4 end-of-day report: {report.as_of.date()}", ""]
    no_reviewer = (
        f"No Reviewer narration today: {report.reviewer_note}." if report.reviewer_note else None
    )

    lines.append("## Blotter")
    if report.proposals:
        lines.append("Proposals today (including rejected):")
        for row in report.proposals:
            reason = f" -- {row['reason']}" if row.get("reason") else ""
            lines.append(f"- {row['symbol']} {row['action']} [{row['status']}]{reason}")
    else:
        lines.append("No proposals today.")
    lines.append("")
    if report.reflections:
        lines.append("Reflections:")
        for row in report.reflections:
            content = row["content"]
            lines.append(
                f"- {row['symbol']} ({content.get('close_reason')}): "
                f"gross={content.get('pnl_gross'):.2f}, net={content.get('pnl_net'):.2f}, "
                f"invalidation_respected={content.get('invalidation_respected')}"
            )
    else:
        lines.append("No closed positions today.")

    lines.append("")
    lines.append("## P&L")
    lines.append(f"- Gross: {report.pnl_gross:,.2f}")
    lines.append(f"- Net (of modeled cost): {report.pnl_net:,.2f}")
    lines.append(f"- Pessimistic (double modeled cost): {report.pnl_pessimistic:,.2f}")

    lines.append("")
    lines.append("## Attribution")
    if report.attribution is not None:
        attribution = report.attribution
        lines.append(f"- Sector (SOXX beta-of-1 approximation): {_money(attribution['sector'])}")
        lines.append(
            f"- Stock-specific (selection + timing, combined): "
            f"{_money(attribution['stock_specific'])}"
        )
        lines.append(f"- Options: {_money(attribution['options'])}")
        if report.attribution_narrative:
            lines.append("")
            lines.append(report.attribution_narrative)
    else:
        lines.append(
            no_reviewer or "Not yet built (Phase 9b -- needs the Reviewer's own synthesis)."
        )

    lines.append("")
    lines.append("## Benchmarks for the day")
    lines.append(f"- SOXX buy-and-hold: {_pct(report.benchmarks.soxx_return)}")
    lines.append(f"- S0 control sleeve: {_pct(report.benchmarks.s0_return)}")
    placebo = report.benchmarks.placebo_pnl
    placebo_line = f"{placebo:,.2f}" if placebo is not None else "not available"
    lines.append(f"- Placebo (T6 control): {placebo_line}")
    lines.append(
        f'- "Do nothing" (hold the open batch flat): {_pct(report.benchmarks.do_nothing_return)}'
    )

    lines.append("")
    lines.append("## Closing market report")
    if report.closing_market_report:
        lines.append(report.closing_market_report)
    else:
        lines.append(
            no_reviewer or "Not yet built (Phase 9b -- needs the Reviewer's own synthesis)."
        )

    lines.append("")
    lines.append("## Process scorecard")
    if report.process_scorecard is not None:
        scorecard = report.process_scorecard
        lines.append(f"- Rejection rate: {scorecard['rejection_rate']:.1%}")
        if scorecard["rejection_reasons"]:
            lines.append(f"  Reasons: {'; '.join(scorecard['rejection_reasons'])}")
        lines.append(
            f"- New positions: {scorecard['new_positions_used']}/{scorecard['new_positions_cap']}"
        )
        turnover_usd = scorecard.get("turnover_usd")
        turnover_cap_usd = scorecard.get("turnover_cap_usd")
        if turnover_usd is not None and turnover_cap_usd is not None:
            lines.append(f"- Turnover: {turnover_usd:,.2f} / {turnover_cap_usd:,.2f} cap")
        adherence = scorecard["invalidation_adherence_rate"]
        lines.append(
            f"- Invalidation adherence: {adherence:.1%}"
            if adherence is not None
            else "- Invalidation adherence: not available (no closes today)"
        )
        lines.append(f"- Incidents today: {len(scorecard['incidents'])}")
        if scorecard["calibration"]:
            lines.append("- Calibration (all-time):")
            for bucket in scorecard["calibration"]:
                lo, hi = bucket["confidence_range"]
                lines.append(
                    f"  - [{lo:.2f}, {hi:.2f}]: {bucket['win_rate']:.1%} win rate, n={bucket['n']}"
                )
        if report.process_scorecard_narrative:
            lines.append("")
            lines.append(report.process_scorecard_narrative)
    else:
        lines.append(
            no_reviewer
            or "Not yet built (Phase 9b -- needs the Reviewer's own reflection grading)."
        )

    lines.append("")
    lines.append("## Monitor cadence")
    cadence = report.monitor_cadence
    lines.append(f"- Checks today: {cadence.checks}")
    if cadence.avg_gap_minutes is not None:
        lines.append(f"- Average gap between checks: {cadence.avg_gap_minutes:.1f}m")
        lines.append(f"- Longest gap between checks: {cadence.max_gap_minutes:.1f}m")
    else:
        lines.append("- Not enough checks today to measure a gap.")

    lines.append("")
    lines.append("## Proposed playbook changes")
    if report.playbook_proposal is not None:
        # A real Reviewer call always sets this to a real string -- either a real
        # proposal or `run_eod_review`'s own "None proposed today." (an explicit answer,
        # not the same as "the Reviewer never ran," which is the `None` fallback below).
        lines.append(report.playbook_proposal)
    else:
        lines.append(no_reviewer or "Not yet built (Phase 9b).")

    return "\n".join(lines) + "\n"

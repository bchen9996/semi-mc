"""`reports/YYYY-MM-DD/eod.md`'s reporting skeleton (S4, `BRIEF_INTRADAY.md`'s "End of
day: report and self-improvement"), Phase 9a.

Mirrors `live/report.py::build_report`/`render_markdown` exactly: gathers everything
through `IntradayLedger`'s own read methods, no SQL above this layer, renders plain
Markdown. This phase builds the blotter, the day's P&L, and the benchmarks-for-the-day
comparison -- everything computable without the Reviewer's own judgment. The closing
market report, process scorecard, and proposed playbook changes are the Reviewer's job
(Phase 9b, batched into one daily call) and are rendered as an explicit "not yet built"
placeholder rather than a fabricated section -- the same honesty convention `paper
evaluate`'s minimum-history caveat already uses.

P&L gross/net are summed directly from each closed trade's own already-computed
`reflect.py::record_mechanical_reflection` fields -- no separate computation, so this can
never drift out of sync with what each individual reflection reports. "Pessimistic" P&L
has no existing convention to reuse: `BRIEF_INTRADAY.md` cites it as matching "the ledger
conventions from the paper-trading brief," but `semis_paper_trading_brief.md` was never
committed to this repo (`CLAUDE.md`'s own opening line already flags this as a dangling
citation). Absent that source, this treats "pessimistic" as net P&L under double the
modeled cost assumption (a stand-in for worse-than-expected slippage) -- a defensible,
clearly-labeled interpretation, not a guess dressed up as the real thing.

Benchmarks for the day (SOXX buy-and-hold, S0's own day return, the placebo's day P&L)
are each optional and independently gathered by the caller into `DayBenchmarks` -- this
module never touches `AlpacaMarketDataClient`, `live.Ledger`, or the placebo's own
`FakeBroker` state directly, so a report with only some of that wiring available still
renders honestly with the rest marked "not available" rather than crashing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class DayBenchmarks:
    soxx_return: float | None = None
    s0_return: float | None = None
    placebo_pnl: float | None = None


@dataclass(frozen=True)
class EodReport:
    as_of: pd.Timestamp
    proposals: list[dict[str, Any]]
    reflections: list[dict[str, Any]]
    pnl_gross: float
    pnl_net: float
    pnl_pessimistic: float
    benchmarks: DayBenchmarks


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


def build_eod_report(
    ledger: Any, as_of: pd.Timestamp, benchmarks: DayBenchmarks | None = None
) -> EodReport:
    proposals = ledger.proposals_for(as_of)
    reflections = ledger.reflections_for(as_of)
    pnl_gross = sum(r["content"].get("pnl_gross") or 0.0 for r in reflections)
    pnl_net = sum(r["content"].get("pnl_net") or 0.0 for r in reflections)
    modeled_cost = pnl_gross - pnl_net
    pnl_pessimistic = pnl_gross - 2 * modeled_cost
    return EodReport(
        as_of=pd.Timestamp(as_of),
        proposals=proposals,
        reflections=reflections,
        pnl_gross=pnl_gross,
        pnl_net=pnl_net,
        pnl_pessimistic=pnl_pessimistic,
        benchmarks=benchmarks or DayBenchmarks(),
    )


def _pct(value: float | None) -> str:
    return f"{value:+.2%}" if value is not None else "not available"


def render_eod_markdown(report: EodReport) -> str:
    lines = [f"# S4 end-of-day report: {report.as_of.date()}", ""]

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
    lines.append("## Benchmarks for the day")
    lines.append(f"- SOXX buy-and-hold: {_pct(report.benchmarks.soxx_return)}")
    lines.append(f"- S0 control sleeve: {_pct(report.benchmarks.s0_return)}")
    placebo = report.benchmarks.placebo_pnl
    placebo_line = f"{placebo:,.2f}" if placebo is not None else "not available"
    lines.append(f"- Placebo (T6 control): {placebo_line}")
    lines.append('- "Do nothing" (hold the open batch flat): not yet built (Phase 9b)')

    lines.append("")
    lines.append("## Closing market report")
    lines.append("Not yet built (Phase 9b -- needs the Reviewer's own synthesis).")

    lines.append("")
    lines.append("## Process scorecard")
    lines.append("Not yet built (Phase 9b -- needs the Reviewer's own reflection grading).")

    lines.append("")
    lines.append("## Proposed playbook changes")
    lines.append("Not yet built (Phase 9b).")

    return "\n".join(lines) + "\n"

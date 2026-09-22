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
"""

from __future__ import annotations

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
    accepted = [row for row in todays if row["status"] == "accepted"]
    accepted_opens = [row for row in accepted if row["action"] in OPEN_OR_ADD_ACTIONS]
    rejected = [row for row in todays if row["status"] == "rejected"]

    round_trips_by_symbol: dict[str, int] = {}
    for row in accepted_opens:
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
    exited -- correct for a trim too, not just a full close) times SOXX's whole-day
    return -- a beta-of-1 approximation. `stock_specific` is the residual (today's total
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

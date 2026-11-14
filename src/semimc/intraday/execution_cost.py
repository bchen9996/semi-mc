"""What S4 actually paid to trade (added 2026-11-14): each fill against the quote midpoint
as its order went in.

Measurement only, like `hedge.py`. A reflection's net P&L charges a modeled cost,
`risk.equity_cost_bps_per_side`, on top of fill prices that already include whatever spread
they crossed, and T6's rule keeps that modeled cost. This measures what the fills paid
against the midpoint, so the assumption can be checked: T5 (`analytics/execution_cost.py`)
does the same for the paper sleeves.

A fill's benchmark is the consolidated quote's midpoint just before its order went in. For a
stop, it is just before the fill, since a stop is placed with its entry and triggers later.
Positive is a cost: a buy filled above the midpoint, a sell below it. Each reflection gets an
`execution` block, its entry and exit fills' cost weighted by quantity, stored rather than
worked out when a report runs so the reports stay offline and reproducible.
:func:`record_execution_costs` prices whatever lacks one on every run. The agent's book only:
the placebo's fills come from the shadow broker's own fill model, so measuring them would
measure that model.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .hedge import _close_ids, reflections_since
from .ledger import IntradayLedger
from .reflect import _aware, position_entry_fills

MidpointBefore = Callable[[str, dt.datetime], float | None]


def fill_cost_bps(
    fill: dict[str, Any], order: dict[str, Any], midpoint_before: MidpointBefore
) -> float | None:
    """One fill's cost in basis points of the midpoint; `None` without a quote."""
    if order["leg"] == "stop" or not order.get("submitted_at"):
        at = fill["filled_at"]
    else:
        at = order["submitted_at"]
    mid = midpoint_before(order["symbol"], _aware(dt.datetime.fromisoformat(at)))
    if mid is None or mid <= 0:
        return None
    sign = 1.0 if order["side"] == "buy" else -1.0
    return sign * (fill["price"] - mid) / mid * 10_000


def _weighted_cost(
    ledger: IntradayLedger, fills: Sequence[dict[str, Any]], midpoint_before: MidpointBefore
) -> float | None:
    """`fills`' cost weighted by quantity, or `None` if any of them can't be priced, so a
    trade is priced whole or waits for a later run."""
    qty = 0.0
    weighted = 0.0
    for fill in fills:
        order = ledger.get_order(fill["client_order_id"])
        cost = fill_cost_bps(fill, order, midpoint_before) if order is not None else None
        if cost is None:
            return None
        qty += fill["qty"]
        weighted += fill["qty"] * cost
    return weighted / qty if qty > 0 else None


def price_execution(
    ledger: IntradayLedger, row: dict[str, Any], midpoint_before: MidpointBefore
) -> dict[str, Any] | None:
    """The `execution` block for one reflection, or `None` if it can't be priced yet."""
    close_ids = _close_ids(row)
    entries = position_entry_fills(ledger, row["symbol"], close_ids)
    exits = [fill for order_id in close_ids for fill in ledger.fills_for_order(order_id)]
    if not entries or not exits:
        return None
    entry_bps = _weighted_cost(ledger, entries, midpoint_before)
    exit_bps = _weighted_cost(ledger, exits, midpoint_before)
    if entry_bps is None or exit_bps is None:
        return None
    return {
        "entry_bps": entry_bps,
        "exit_bps": exit_bps,
        "per_side_bps": (entry_bps + exit_bps) / 2.0,
    }


def record_execution_costs(
    ledger: IntradayLedger, market_data: Any, since: dt.date | None = None
) -> int:
    """Prices every reflection from `since` on that has no `execution` block and stores it.
    Returns how many were priced."""
    rows = [
        row
        for row in reflections_since(ledger.all_reflections(), since)
        if "execution" not in row["content"]
    ]
    midpoints: dict[tuple[str, dt.datetime], float | None] = {}

    def midpoint_before(symbol: str, at: dt.datetime) -> float | None:
        if (symbol, at) not in midpoints:
            midpoints[(symbol, at)] = market_data.midpoint_before(symbol, at)
        return midpoints[(symbol, at)]

    priced = 0
    for row in rows:
        block = price_execution(ledger, row, midpoint_before)
        if block is not None:
            ledger.update_reflection(row["id"], {**row["content"], "execution": block})
            priced += 1
    return priced


@dataclass(frozen=True)
class ExecutionSummary:
    """Reflections' measured execution cost, in basis points of the midpoint."""

    exits: int = 0
    unpriced: int = 0
    entry_bps: float | None = None
    exit_bps: float | None = None
    # Per side, averaged over exits; what compares with `equity_cost_bps_per_side`.
    per_side_bps: float | None = None
    stderr_bps: float | None = None


def summarize_execution(reflections: Sequence[dict[str, Any]]) -> ExecutionSummary:
    blocks = [row["content"]["execution"] for row in reflections if row["content"].get("execution")]
    unpriced = len(reflections) - len(blocks)
    if not blocks:
        return ExecutionSummary(unpriced=unpriced)
    count = len(blocks)
    per_side = [block["per_side_bps"] for block in blocks]
    mean = sum(per_side) / count
    stderr = None
    if count > 1:
        stderr = math.sqrt(sum((value - mean) ** 2 for value in per_side) / (count - 1) / count)
    return ExecutionSummary(
        exits=count,
        unpriced=unpriced,
        entry_bps=sum(block["entry_bps"] for block in blocks) / count,
        exit_bps=sum(block["exit_bps"] for block in blocks) / count,
        per_side_bps=mean,
        stderr_bps=stderr,
    )

"""The mechanical half of a reflection (S4, `BRIEF_INTRADAY.md`'s "Reflection after each
trade"), Phase 9a.

The brief's own "Agent roles" section names exactly three model roles (Researcher,
Trader, Reviewer) -- no separate "Reflector" role exists, and Reviewer's own description
already says it "grades process." So a reflection's *qualitative* fields (category,
process grade, lesson) are the Reviewer's job, synthesized in one batched call as part of
`eod_review` (Phase 9b) -- not a new per-close call, which would also blow past the
brief's own "roughly 20 cycles a day plus two long jobs" cost shape.

:func:`record_mechanical_reflection` is everything that *doesn't* need that judgment
call: P&L gross/net of modeled cost, whether the invalidation level was respected, and
time in trade -- all computed purely from `orders`/`fills`/`proposals` rows already in
the ledger. It's called from every close-producing code path (`monitor.py`, `jobs.py`'s
`run_cycle` close/trim branches, `run_carry`) right after that path's own closing fill is
confirmed. `category`/`process_grade`/`lesson` are left `None`; Phase 9b's `eod_review`
fills them in.

A `trim` (partial exit) gets its own reflection too, scoped to just the trimmed qty --
not only a full close. The P&L math already generalizes correctly: `avg_entry_price` is
always the position's whole-entry average (constant across however many trims it takes
to close), while `pnl_gross`/`pnl_net` multiply by `exit_qty`, the qty this specific
event actually exited -- so a trim's reflection reports the realized P&L on the slice
just sold, and the remaining position gets its own, separate reflection whenever it
eventually closes.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd

from ..config import IntradayConfig
from .ledger import IntradayLedger


def _aware(value: dt.datetime) -> dt.datetime:
    """Same "assume UTC" fallback `monitor.py`/`pretrade.py` already use -- a real
    `Order`'s timestamps come back aware UTC, but a naive value must not raise."""
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def _entry_and_proposal(
    ledger: IntradayLedger, symbol: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The entry order and its originating proposal for `symbol`'s most recently opened
    position, via `latest_filled_entry_for` -- deliberately not the *closing* order's own
    `proposal_id`, which only equals the entry's for a monitor-triggered close;
    `run_cycle`'s Trader-issued close/trim attaches the new close-action proposal's own
    id instead (confirmed by reading both paths). Every tranche of one accepted proposal
    shares the same `proposal_id`, so whichever entry row is "most recent" still points
    at the right proposal even when `entry_tranches` > 1."""
    entry = ledger.latest_filled_entry_for(symbol)
    if entry is None or entry["proposal_id"] is None:
        return None
    proposal = ledger.get_proposal(entry["proposal_id"])
    if proposal is None:
        return None
    return entry, proposal


def _summed_fills(
    ledger: IntradayLedger, client_order_ids: list[str]
) -> tuple[float, float, dt.datetime | None, dt.datetime | None]:
    """Total filled qty and notional across every fill for the given orders, plus the
    earliest and latest fill times -- used both for a tranched entry (summed across every
    tranche's own order) and a single closing order (usually one fill, but summed the
    same way in case a close itself ever partial-fills)."""
    qty = 0.0
    notional = 0.0
    earliest: dt.datetime | None = None
    latest: dt.datetime | None = None
    for client_order_id in client_order_ids:
        for fill in ledger.fills_for_order(client_order_id):
            qty += fill["qty"]
            notional += fill["qty"] * fill["price"]
            filled_at = _aware(dt.datetime.fromisoformat(fill["filled_at"]))
            if earliest is None or filled_at < earliest:
                earliest = filled_at
            if latest is None or filled_at > latest:
                latest = filled_at
    return qty, notional, earliest, latest


def record_mechanical_reflection(
    symbol: str,
    close_order_id: str,
    close_reason: str,
    ledger: IntradayLedger,
    config: IntradayConfig,
    as_of: pd.Timestamp,
) -> int | None:
    """Computes and stores everything a reflection needs that doesn't require model
    judgment. `close_reason` is supplied by the caller (e.g. `"invalidation"`,
    `"time_stop"`, `"trader_close"`, `"trader_trim"`, `"carry"`) -- each call site
    already knows exactly why it's closing, so this is threaded through, not inferred
    after the fact.

    Returns `None` (recording nothing) if no traceable entry or no exit fill exists yet --
    a stray or malformed close must never crash the job that's closing a position, the
    same "never act on incomplete information" caution `execution/pretrade.py` uses
    elsewhere.
    """
    found = _entry_and_proposal(ledger, symbol)
    if found is None:
        return None
    entry, proposal = found

    entry_orders = ledger.orders_for_proposal(entry["proposal_id"], leg="entry")
    entry_qty, entry_notional, entry_filled_at, _ = _summed_fills(
        ledger, [o["client_order_id"] for o in entry_orders]
    )
    if entry_qty <= 0 or entry_filled_at is None:
        return None

    exit_qty, exit_notional, _, exit_filled_at = _summed_fills(ledger, [close_order_id])
    if exit_qty <= 0 or exit_filled_at is None:
        return None

    avg_entry_price = entry_notional / entry_qty
    avg_exit_price = exit_notional / exit_qty

    payload = proposal["payload"]
    side = entry["side"]
    sign = 1.0 if side == "buy" else -1.0
    pnl_gross = sign * (avg_exit_price - avg_entry_price) * exit_qty

    cost_bps = config.risk.equity_cost_bps_per_side
    modeled_cost = (cost_bps / 10_000) * (entry_notional + exit_notional)
    pnl_net = pnl_gross - modeled_cost

    invalidation_price = payload.get("invalidation_price")
    invalidation_respected = None
    if invalidation_price is not None:
        if close_reason == "invalidation":
            # A system-triggered stop-out closes *at or through* the invalidation level
            # by construction -- that's the definition of what fired it, so its exit
            # price is expected to be at or worse than invalidation_price. "Respected"
            # here means the system did its job and reacted, not that the fill price was
            # no worse than the level -- that would make every real stop-out score as a
            # violation, which is backwards.
            invalidation_respected = True
        else:
            # Any other close (Trader discretion, a time-stop, carry): "respected" means
            # the position was closed before ever reaching the stated level at all.
            invalidation_respected = (
                avg_exit_price >= invalidation_price
                if side == "buy"
                else avg_exit_price <= invalidation_price
            )

    time_in_trade_minutes = (exit_filled_at - entry_filled_at).total_seconds() / 60.0

    content = {
        "thesis": payload.get("thesis"),
        "confidence": payload.get("confidence"),
        "side": side,
        "invalidation_price": invalidation_price,
        "close_reason": close_reason,
        "avg_entry_price": avg_entry_price,
        "avg_exit_price": avg_exit_price,
        "pnl_gross": pnl_gross,
        "pnl_net": pnl_net,
        "invalidation_respected": invalidation_respected,
        "time_in_trade_minutes": time_in_trade_minutes,
        # Filled in by Phase 9b's eod_review, batched into its one daily Reviewer call.
        "category": None,
        "process_grade": None,
        "lesson": None,
    }
    return ledger.record_reflection(as_of, symbol, content, client_order_id=close_order_id)

"""The ``mark`` job: the polling fallback the brief requires regardless of whether a
trade-updates websocket stream is ever wired up (``docs/alpaca_notes.md``'s "still to
verify" list -- reconnect/backfill behavior is unconfirmed, so a live job cannot depend on
it alone).

Three things happen here, in order, per sleeve: refresh fill status for every order the
ledger still thinks is open, prune orders that have sat unfilled past the configured
reprice interval (never resubmits -- the next ``execute`` run rebuilds the remaining gap at
a fresh price), and reconcile the ledger's own view of positions against the broker's
actual ones. A fresh snapshot is always recorded last, regardless of what else happened.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

from ..execution.broker import Broker
from .ledger import Ledger

_OPEN_STATUSES = ("new", "accepted", "partially_filled")


@dataclass
class MarkResult:
    fills_recorded: int = 0
    orders_canceled_for_reprice: list[str] = field(default_factory=list)
    orders_not_found: list[str] = field(default_factory=list)
    reconciliation_status: str = "ok"
    discrepancies: list[dict] = field(default_factory=list)


def run_mark(
    sleeve: str,
    config,  # noqa: ANN001
    broker: Broker,
    ledger: Ledger,
    as_of: pd.Timestamp,
    now: dt.datetime,
) -> MarkResult:
    as_of = pd.Timestamp(as_of)
    result = MarkResult()
    reprice_after = dt.timedelta(minutes=config.paper.execution.reprice_interval_minutes)

    for row in ledger.open_orders(sleeve):
        client_order_id = row["client_order_id"]
        order = broker.get_order_by_client_id(client_order_id)
        if order is None:
            ledger.record_incident(
                "error", f"open order {client_order_id} not found at broker", sleeve
            )
            result.orders_not_found.append(client_order_id)
            continue

        already = sum(f["qty"] for f in ledger.fills_for_order(client_order_id))
        if order.filled_qty > already + 1e-9:
            # order.filled_avg_price is the order's cumulative average fill price, not this
            # specific incremental fill's own price -- alpaca-py (and FakeBroker, to match)
            # don't expose per-fill prices via a polled Order, only via a trade-updates
            # stream this project doesn't yet consume. Documented approximation, not an
            # oversight.
            ledger.record_fill(
                client_order_id,
                qty=order.filled_qty - already,
                price=order.filled_avg_price or 0.0,
                filled_at=order.filled_at or now,
            )
            result.fills_recorded += 1
        ledger.update_order_status(client_order_id, order)

        if (
            order.status in _OPEN_STATUSES
            and order.submitted_at is not None
            and now - order.submitted_at > reprice_after
        ):
            try:
                broker.cancel_order(order.id)
            except (ValueError, KeyError) as exc:
                # A race between this poll and the cancel attempt -- the order filled (or
                # otherwise left a cancelable state) in between. Not fatal: the fill branch
                # above already recorded whatever happened this poll, and the next poll
                # will see the corrected status either way.
                ledger.record_incident(
                    "error", f"cancel failed for {client_order_id}: {exc}", sleeve
                )
                continue
            refreshed = broker.get_order_by_client_id(client_order_id)
            if refreshed is not None:
                ledger.update_order_status(client_order_id, refreshed)
            result.orders_canceled_for_reprice.append(client_order_id)

    # Every sleeve reconciled here shares one real Alpaca paper account (s0 and s3, per
    # `docs/go_live_checklist.md` item 6), so `broker.list_positions()` is the whole
    # account's holdings, not just this sleeve's own -- compare against the combined view
    # across every sleeve, not `net_filled_qty(sleeve)` alone (see that method's docstring
    # for the 2026-09-26 incident this fixes: mark(s0) failing on every run once s3 held
    # its first real position).
    expected = ledger.net_filled_qty_all_sleeves()
    actual = {p.symbol: p.qty for p in broker.list_positions()}
    tolerance = config.paper.reconciliation.position_tolerance_shares
    symbols = set(expected) | set(actual)
    discrepancies = [
        {"symbol": symbol, "expected": expected.get(symbol, 0.0), "actual": actual.get(symbol, 0.0)}
        for symbol in sorted(symbols)
        if abs(expected.get(symbol, 0.0) - actual.get(symbol, 0.0)) > tolerance
    ]
    result.discrepancies = discrepancies
    result.reconciliation_status = "discrepancy" if discrepancies else "ok"
    ledger.record_reconciliation(sleeve, as_of, result.reconciliation_status, discrepancies)
    if discrepancies:
        ledger.record_incident(
            "error", f"position reconciliation mismatch: {discrepancies}", sleeve
        )

    sleeve_config = config.paper.sleeves.get(sleeve)
    if sleeve_config is not None and sleeve_config.trading_enabled:
        account = broker.get_account()
        positions = {p.symbol: p for p in broker.list_positions()}
        ledger.record_snapshot(sleeve, as_of, account.equity, account.cash, positions)
    else:
        # This sleeve isn't the one actually trading against the shared account (s0 while
        # s3 trades, docs/go_live_checklist.md item 6) -- broker.get_account()/
        # list_positions() would be the *other* sleeve's real equity/cash/positions, not
        # this one's. Found 2026-09-26, the same root cause net_filled_qty_all_sleeves'
        # reconciliation fix addressed, but for the snapshot this function always records
        # last regardless of reconciliation outcome: once that reconciliation fix stopped
        # crashing mark(s0) before it ever reached this line, this line would otherwise
        # start silently mislabeling s3's real activity as s0's own on every successful
        # run -- corrupting the entire s0-vs-s3 comparison this shared account exists to
        # make. Carry this sleeve's own last recorded state forward unchanged instead (it
        # never trades, so nothing about it has genuinely changed); if it has no prior
        # snapshot at all, record nothing rather than fabricate a starting figure this
        # module has no config source for.
        previous = ledger.latest_snapshot(sleeve)
        if previous is not None:
            ledger.record_snapshot(sleeve, as_of, previous["equity"], previous["cash"], {})
    return result

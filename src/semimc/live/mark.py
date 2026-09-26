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


@dataclass(frozen=True)
class _CarriedPosition:
    """Just enough to satisfy ``Ledger.record_snapshot``'s ``position.qty``/
    ``position.market_value`` access -- not a real :class:`~semimc.execution.broker.Position`
    (no ``side``/``avg_entry_price``, which the stored ``positions_json`` never carried and
    which the broker was never actually asked about for a sleeve that isn't trading here)."""

    qty: float
    market_value: float


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

    # Whether broker.get_account()/list_positions() genuinely is *this* sleeve's own state
    # depends on whether any other sleeve has ever also traded here -- not on this sleeve's
    # own current `trading_enabled` flag. A config flag answers "may this sleeve place new
    # orders," not "does the shared account's real state still belong to it": a sleeve
    # disabled at a review checkpoint (`paper.review_months`) while still holding a real,
    # market-moving position from before must keep reading the broker directly, not freeze
    # at whatever its last snapshot happened to show the moment it was disabled. `expected`
    # (this account's combined fills, computed above for reconciliation) already answers the
    # right question when compared against this sleeve's own fills: equal (within the same
    # `tolerance` reconciliation uses just above, not exact float equality -- SQLite makes
    # no row-order guarantee between these two differently-filtered queries, so summation
    # order, and therefore float rounding, could in principle differ even when the
    # underlying fills are identical) means nobody else has ever contributed anything here,
    # so the account's real state is exclusively this sleeve's, trading_enabled or not.
    own_fills = ledger.net_filled_qty(sleeve)
    is_sole_trader = set(own_fills) == set(expected) and all(
        abs(own_fills[symbol] - expected[symbol]) <= tolerance for symbol in expected
    )
    if is_sole_trader:
        account = broker.get_account()
        positions = {p.symbol: p for p in broker.list_positions()}
        ledger.record_snapshot(sleeve, as_of, account.equity, account.cash, positions)
    else:
        # Some other sleeve has also traded on this shared account (s0 while s3 trades,
        # `docs/go_live_checklist.md` item 6) -- broker.get_account()/list_positions() would
        # be a mix of both, or the *other* sleeve's real equity/cash/positions, not cleanly
        # this one's. Found 2026-09-26: the reconciliation fix above (`expected` itself)
        # stopped mark(s0) from crashing before it ever reached this line -- but this line,
        # unfixed, would then start silently mislabeling s3's real activity as s0's own on
        # every successful run, corrupting the entire s0-vs-s3 comparison this shared
        # account exists to make. Carry this sleeve's own last recorded state forward
        # *verbatim* instead -- equity, cash, and whatever positions it last held, not
        # blanked to flat (s0's own history happens to already be all-flat, but blanking
        # would be wrong in general -- see the position-carrying test for why). If this
        # sleeve has no prior snapshot at all, record nothing rather than fabricate a
        # starting figure this module has no config source for.
        previous = ledger.latest_snapshot(sleeve)
        if previous is not None:
            carried_positions = {
                symbol: _CarriedPosition(qty=p["qty"], market_value=p["market_value"])
                for symbol, p in previous["positions"].items()
            }
            ledger.record_snapshot(
                sleeve, as_of, previous["equity"], previous["cash"], carried_positions
            )
    return result

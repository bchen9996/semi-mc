"""Turning an already-*accepted* `TradeProposal` into a real order at Alpaca, with a real
stop resting at the broker rather than a polling process (S4, `BRIEF_INTRADAY.md`,
Phases 3-4).

Tranche-splitting and the placebo agent are deliberately not here -- see the Phase 4
plan's "Roadmap" section for why each waits on `jobs.py`'s loop (Phase 5) or a real
watchlist source (Phase 6).

Reuses `execution/orders.py`'s low-level helpers (`collared_limit_price`,
`sequence_orders`) rather than a second copy, and mirrors `live/execute.py::run_execute`'s
per-order fault-isolation shape exactly, so a fault in one order in a cycle's batch never
takes down the rest of it.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

import pandas as pd

from ..config import IntradayConfig
from ..execution.broker import (
    AccountBlockedError,
    Broker,
    BrokerError,
    MarketClosedError,
    Order,
    OrderRequest,
    OrderSide,
    Position,
    RateLimitError,
    WashTradeError,
)
from ..execution.orders import collared_limit_price, round_to_tick
from ..execution.pretrade import check_order_tradable
from .ledger import IntradayLedger
from .proposals import OPEN_OR_ADD_ACTIONS, TradeProposal, whole_shares
from .reflect import record_mechanical_reflection

_OPEN_STATUSES = frozenset({"new", "accepted", "partially_filled"})

# How long `cancel_resting_stops` waits for a cancel to complete before an exit is held
# back: 20 polls 0.25 s apart, 5 s in all. A starting guess, not a measured figure: no
# cancel has been timed on this account yet. Paper fills on it land 1-4 s after
# submission (`ledger/paper.sqlite3`, 2026-09-24 and 2026-10-02).
_CANCEL_SETTLE_POLLS = 20
_CANCEL_POLL_SECONDS = 0.25


def intraday_client_order_id(
    as_of: pd.Timestamp, cycle: str, symbol: str, attempt: int = 0, tranche: int = 1
) -> str:
    """Deterministic per (as_of, cycle, symbol, attempt) -- the brief's own "deterministic
    `client_order_id` per cycle, symbol, and attempt" idempotency rule. There's no
    `Decision` to hash off the way `execution/orders.py::client_order_id` has; this hashes
    the tuple itself directly.

    `tranche` (Phase 5) is appended only when it isn't `1`, so every id built before
    tranche-splitting existed -- and every existing test asserting an exact id string --
    is unaffected."""
    base = f"s4-{pd.Timestamp(as_of):%Y%m%d}-{cycle}-{symbol}-{attempt}"
    return base if tranche == 1 else f"{base}-t{tranche}"


def build_entry_order(
    proposal: TradeProposal,
    as_of: pd.Timestamp,
    cycle: str,
    price: float,
    equity: float,
    config: IntradayConfig,
    attempt: int = 0,
    tranche: int = 1,
    at_least_one_share: bool = False,
    qty: int | None = None,
) -> OrderRequest:
    """One `open`/`add` proposal, one order, with a real stop resting at the broker the
    instant it fills -- this is what removes the need for a minute-level monitor process
    between GH Actions runs.

    Raises `ValueError` for any other `proposal.action` (see the module docstring for why
    close/trim aren't built here) or if `size_pct` rounds to fewer than one whole share --
    unless `at_least_one_share`, which a later tranche passes: `jobs.py::run_cycle` only
    splits an entry into as many tranches as it has whole shares, but a price rise since
    the first tranche can round a one-share piece down to zero.

    Whole-share `qty` always, never `notional` -- this project's existing sizing
    convention (`execution/orders.py::build_orders` does the same), and a hedge pending
    real confirmation of whether an OTO stop-loss leg even accepts a fractional qty
    (unconfirmed anywhere checked -- see `docs/alpaca_notes.md`'s "still to verify" list).

    `tranche` (Phase 5) is passed straight through to `intraday_client_order_id`, to keep
    that piece's `client_order_id` distinct. `qty`, when given, is the share count to buy
    in place of `size_pct`'s own whole shares (added 2026-10-19): `jobs.py::run_cycle`
    rounds a tranched entry to whole shares once, as a whole, and splits the shares,
    since rounding each piece down on its own lost a share in 3 of Stage 2's first 6
    two-tranche entries.
    """
    if proposal.action not in OPEN_OR_ADD_ACTIONS:
        raise ValueError(f"build_entry_order only builds open/add orders, got {proposal.action!r}")

    # side/size_pct/limit_price/invalidation_price are all required for open/add by
    # TradeProposal's own model_validator, so they're never None here.
    if qty is None:
        qty = whole_shares(proposal.size_pct, equity, price)
    if qty <= 0 and at_least_one_share:
        qty = 1
    if qty <= 0:
        raise ValueError(
            f"size_pct {proposal.size_pct} is too small to buy a whole share of "
            f"{proposal.symbol} at {price}"
        )

    return OrderRequest(
        symbol=proposal.symbol,
        side=proposal.side,
        type="limit",
        time_in_force="day",
        client_order_id=intraday_client_order_id(as_of, cycle, proposal.symbol, attempt, tranche),
        qty=qty,
        # Reuses risk.equity_cost_bps_per_side as the collar -- the same modeled per-side
        # friction validate_proposal's edge-vs-cost check already uses, rather than a
        # second, separately-tuned execution parameter with no evidence behind it yet.
        limit_price=collared_limit_price(
            price, proposal.side, config.risk.equity_cost_bps_per_side
        ),
        order_class="oto",
        # A plain stop, not a stop-limit: the point of this leg is a guaranteed exit at
        # the invalidation level, and a stop-limit can fail to fill in exactly the
        # fast-moving market a stop is meant to protect against.
        #
        # round_to_tick: invalidation_price is the LLM's own free-form float and has no
        # guarantee of landing on a valid Alpaca price increment (see that function's
        # docstring) -- unrounded, this would 422 the same way collared_limit_price's
        # output did before this fix.
        stop_loss_price=round_to_tick(proposal.invalidation_price),
    )


def record_new_fill(order: Order, ledger: IntradayLedger, now: dt.datetime) -> bool:
    """Records whatever ``order`` has filled beyond what the ledger already holds for it;
    ``True`` if there was anything new. Same increment rule, and the same cumulative
    average price standing in for the increment's own price, as `live/mark.py`'s
    ``_record_new_fill``."""
    already = sum(fill["qty"] for fill in ledger.fills_for_order(order.client_order_id))
    if order.filled_qty <= already + 1e-9 or order.filled_avg_price is None:
        return False
    ledger.record_fill(
        order.client_order_id,
        qty=order.filled_qty - already,
        price=order.filled_avg_price,
        filled_at=order.filled_at or now,
    )
    return True


def cancel_resting_stops(
    symbol: str,
    broker: Broker,
    ledger: IntradayLedger,
    now: dt.datetime | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Cancels every resting protective stop leg `symbol` has -- one per tranche of a
    split entry -- waits for the broker to confirm each one is done, and returns their
    `client_order_id`s (empty if there were none).

    **Waits, because Alpaca cancels asynchronously** (added 2026-10-03, before Stage 2
    first sends real orders). A canceled order sits in ``pending_cancel`` (open, to this
    project: ``AlpacaPaperBroker`` maps it to ``"accepted"``) and keeps holding its
    shares until the cancel completes, so a close sent straight after the cancel request
    can be rejected for "insufficient qty available". Each leg is polled up to
    `_CANCEL_SETTLE_POLLS` times, `_CANCEL_POLL_SECONDS` apart, until it reports a final
    status; a leg still open after that stays open in the ledger, which is how
    :func:`submit_exit` knows to hold the exit back. A leg that turns out to have *filled*
    (the stop fired before the cancel reached it) has its fill recorded. The shadow
    broker cancels at once, so Stage 1 never waits.

    This is a correctness requirement for closing or trimming a position, not cleanup:
    only bracket/OCO/trailing-stop orders are confirmed wash-trade-exempt at Alpaca
    (Phase 3's direct quote, `docs/alpaca_notes.md`), so a plain "simple" close/trim order
    left to collide with a still-resting stop-loss leg in the same symbol would likely be
    rejected -- the brief's own "never leave an opposite-side order open in the same
    symbol; cancel before reversing." And a stop left behind a closed position is worse
    than a rejection: it sells shares that are no longer there, opening a short. Found
    2026-10-02, when this cancelled only the most recent leg and a two-tranche NVDA close
    left the first tranche's 3-share stop resting (`IntradayLedger.open_stop_legs_for`).

    Each leg's ledger row is updated to whatever the broker now reports, or "canceled" if
    the broker no longer has it -- the ledger used to keep showing a cancelled leg as
    open. Tolerates a leg already canceled or otherwise gone at the broker (`KeyError`/
    `ValueError` from `cancel_order`) the same forgiving way real Alpaca's own
    `cancel_order` already is (`docs/alpaca_notes.md`: canceling an already-canceled
    order succeeds silently) -- nothing left to cancel is not a failure. A cancel the
    broker refuses outright, or a lookup that raises, leaves the leg unconfirmed: still
    open, asked about again on the next poll.
    """
    legs = ledger.open_stop_legs_for(symbol)
    _cancel_and_settle(legs, broker, ledger, now or dt.datetime.now(dt.UTC), sleep)
    return [leg["client_order_id"] for leg in legs]


def cancel_unfilled_entries(
    broker: Broker,
    ledger: IntradayLedger,
    now: dt.datetime,
    *,
    older_than: dt.timedelta | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict]:
    """Cancels every entry still open with nothing filled, then its resting stop leg, and
    returns the ledger rows of the entries that ended canceled with nothing filled (added
    2026-10-22, owner's option A of three). An entry is a `day` limit order with its stop
    attached (OTO), which rules out IOC: one priced off a market that has already moved
    on rests at the broker for the rest of the day. MU's 09:31 CT short on 2026-10-22
    did, until MU's time-stop exit canceled it at 12:01, and could have filled hours
    after its trigger.

    `older_than` limits it to entries sent at least that long before `now`: `run_cycle`
    passes a few minutes, so it cancels what earlier jobs sent and never what a job
    re-dispatched seconds before sent. `run_carry` passes nothing and cancels them all,
    since an entry that fills after carry is held overnight on an intraday thesis.

    A partly filled entry is left alone: its shares are held, under its stop. An entry
    cancels before its stop leg does, and the leg (found through the broker's own record
    of the order) is canceled only once the entry is confirmed canceled with nothing
    filled: one that filled in the meantime keeps its stop. An entry whose lookup fails
    or whose cancel hasn't completed is asked about again next time.

    Each entry canceled with nothing filled gets an ``"entry_unfilled:{proposal_id}"``
    info incident, which the day's budgets read (`IntradayLedger.counted_proposals_for`):
    a proposal none of whose entries filled uses no new position, round trip or turnover.
    """
    cutoff = None if older_than is None else pd.Timestamp(_utc(now)) - older_than
    entries = [
        row
        for row in ledger.open_orders()
        if row["leg"] == "entry"
        and not ledger.fills_for_order(row["client_order_id"])
        and (cutoff is None or _sent_by(row, cutoff))
    ]
    if not entries:
        return []
    _cancel_and_settle(entries, broker, ledger, now, sleep)

    canceled: list[dict] = []
    legs: list[dict] = []
    for entry in entries:
        row = ledger.get_order(entry["client_order_id"]) or entry
        if row["status"] in _OPEN_STATUSES or ledger.fills_for_order(row["client_order_id"]):
            continue
        canceled.append(row)
        with contextlib.suppress(Exception):  # an unfound leg is swept as an orphan later
            order = broker.get_order_by_client_id(row["client_order_id"])
            for child in (order.legs or []) if order is not None else []:
                leg = ledger.get_order(child.client_order_id)
                if leg is not None and leg["status"] in _OPEN_STATUSES:
                    legs.append(leg)
        sent = _sent_at(row)
        age = (
            f" after {(pd.Timestamp(_utc(now)) - sent).total_seconds() / 60:.0f} min"
            if sent
            else ""
        )
        ledger.record_incident(
            "info",
            f"entry_unfilled:{row['proposal_id']} {row['symbol']}: {row['side']} "
            f"{row['qty']:g} at {row['limit_price']} canceled with nothing filled{age}",
        )
    if legs:
        _cancel_and_settle(legs, broker, ledger, now, sleep)
    return canceled


def _utc(moment: dt.datetime) -> dt.datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.UTC)


def _sent_at(row: dict) -> pd.Timestamp | None:
    if not row.get("submitted_at"):
        return None
    sent = pd.Timestamp(row["submitted_at"])
    return sent.tz_localize("UTC") if sent.tzinfo is None else sent.tz_convert("UTC")


def _sent_by(row: dict, cutoff: pd.Timestamp) -> bool:
    """Whether `row` was sent at or before `cutoff`; one with no send time counts as old."""
    sent = _sent_at(row)
    return sent is None or sent <= cutoff


def _cancel_and_settle(
    rows: list[dict],
    broker: Broker,
    ledger: IntradayLedger,
    now: dt.datetime,
    sleep: Callable[[float], None],
) -> None:
    """Cancels each ledger order row at the broker, then polls until each reports a
    final status or the wait runs out (see :func:`cancel_resting_stops`), recording each
    row's status and any fill as it goes."""
    for row in rows:
        with contextlib.suppress(KeyError, ValueError, BrokerError):
            broker.cancel_order(row["broker_order_id"])

    waiting = list(rows)
    for poll in range(_CANCEL_SETTLE_POLLS):
        if poll:
            sleep(_CANCEL_POLL_SECONDS)
        still_open = []
        for row in waiting:
            try:
                refreshed = broker.get_order_by_client_id(row["client_order_id"])
            except Exception:  # noqa: BLE001 - unconfirmed, ask again on the next poll
                still_open.append(row)
                continue
            status = refreshed.status if refreshed is not None else "canceled"
            ledger.record_order(
                row["client_order_id"],
                row["symbol"],
                row["side"],
                status=status,
                broker_order_id=row["broker_order_id"],
                leg=row["leg"],
            )
            if refreshed is not None:
                record_new_fill(refreshed, ledger, now)
            if status in _OPEN_STATUSES:
                still_open.append(row)
        waiting = still_open
        if not waiting:
            break


def build_close_order(
    position: Position,
    as_of: pd.Timestamp,
    cycle: str,
    price: float | None,
    config: IntradayConfig,
    attempt: int = 0,
) -> OrderRequest:
    """Fully exits `position`. Qty and side come from the real, currently-held
    `Position` -- never from a proposal, which needs neither `side` nor `size_pct` to
    close (the schema doesn't require either for `action="close"`).

    `order_class="simple"` -- exiting needs no new stop leg. The resting stop-loss legs
    from the original entry must be canceled first; :func:`submit_exit` does that and
    calls this with the position as re-read once they're gone.

    `price=None` builds a market order instead of a collared limit: the exit a time stop
    or carry sends on the real account when there is no current price to collar
    (owner decision 2026-10-15, after that day's midday quote outage held both positions
    past their time stops). Never for an entry, and only while the market is
    open (:func:`market_is_open`), or it would wait at Alpaca for the next open.
    """
    side: OrderSide = "sell" if position.side == "long" else "buy"
    return OrderRequest(
        symbol=position.symbol,
        side=side,
        type="limit" if price is not None else "market",
        time_in_force="day",
        client_order_id=intraday_client_order_id(as_of, cycle, position.symbol, attempt),
        qty=abs(position.qty),
        limit_price=(
            collared_limit_price(price, side, config.risk.equity_cost_bps_per_side)
            if price is not None
            else None
        ),
    )


def market_is_open(broker: Broker) -> bool:
    """The broker's own clock says the exchange is open. ``False`` if the clock can't be
    read: a market order sent while the market is closed waits at Alpaca and fills at
    the next open, at whatever price that turns out to be."""
    try:
        return bool(broker.get_clock().is_open)
    except Exception:  # noqa: BLE001 - an unreadable clock means no market order, never a crash
        return False


def build_trim_order(
    proposal: TradeProposal,
    position: Position,
    as_of: pd.Timestamp,
    cycle: str,
    price: float,
    equity: float,
    config: IntradayConfig,
    attempt: int = 0,
) -> OrderRequest:
    """Partially reduces `position`. `proposal.action` must be `"trim"` (raises
    `ValueError` otherwise). `size_pct` is percent of equity, the same unit `open`/`add`
    use (`TradeProposal`'s own model_validator now requires it for `trim`) -- capped at
    what's actually held, so a trim can never exceed the position, the same defensive cap
    `execution/orders.py::build_orders` already uses for its own sell-down case.

    Like `build_close_order`, the resting stop-loss legs must be canceled first
    (:func:`submit_exit`), and -- unlike a close -- the remainder needs a
    fresh, smaller replacement stop afterward (`build_replacement_stop_order`) so it is
    never left resting unprotected. `jobs.py::run_cycle` places that one.
    """
    if proposal.action != "trim":
        raise ValueError(f"build_trim_order only builds trim orders, got {proposal.action!r}")

    # size_pct is required for "trim" by TradeProposal's own model_validator, so it's
    # never None here.
    position_notional = abs(position.qty) * price
    desired_notional = min(proposal.size_pct * equity, position_notional)
    qty = min(math.floor(desired_notional / price), math.floor(abs(position.qty)))
    if qty <= 0:
        raise ValueError(
            f"size_pct {proposal.size_pct} is too small to trim a whole share of "
            f"{proposal.symbol} at {price}"
        )

    side: OrderSide = "sell" if position.side == "long" else "buy"
    return OrderRequest(
        symbol=proposal.symbol,
        side=side,
        type="limit",
        time_in_force="day",
        client_order_id=intraday_client_order_id(as_of, cycle, proposal.symbol, attempt),
        qty=qty,
        limit_price=collared_limit_price(price, side, config.risk.equity_cost_bps_per_side),
    )


def build_replacement_stop_order(
    symbol: str,
    side: OrderSide,
    qty: float,
    stop_price: float,
    as_of: pd.Timestamp,
    cycle: str,
    attempt: int = 0,
) -> OrderRequest:
    """A plain standalone stop order (`type="stop"`, `order_class="simple"` -- not
    `"oto"`: there is no new entry here, just re-protecting a trimmed remainder) so a
    position is never left resting unprotected between a trim and the next cycle noticing.

    `client_order_id` is suffixed the same way `FakeBroker`'s synthesized OTO leg is
    (`"...::stop"`), for the same reason: this isn't another attempt at the same order,
    it's a different kind of order for the same symbol/cycle.

    `stop_price` is rounded via `round_to_tick` -- callers pass the original entry's
    stored `invalidation_price` straight from the ledger, an LLM-set float with the same
    sub-penny risk `build_entry_order`'s own `stop_loss_price` has.
    """
    return OrderRequest(
        symbol=symbol,
        side=side,
        type="stop",
        time_in_force="day",
        client_order_id=f"{intraday_client_order_id(as_of, cycle, symbol, attempt)}::stop",
        qty=qty,
        stop_price=round_to_tick(stop_price),
    )


def submit_order_with_fault_isolation(
    request: OrderRequest,
    broker: Broker,
    ledger: IntradayLedger,
    now: dt.datetime,
    proposal_id: int | None = None,
    tranche_index: int = 1,
    tranche_total: int = 1,
    leg: str = "entry",
    exit_reason: str | None = None,
) -> Order | None:
    """Mirrors `live/execute.py::run_execute`'s per-order try/except exactly.

    `check_order_tradable` runs first, same as `run_execute`. A `TimeoutError` falls back
    to `get_order_by_client_id` (the request may have gone through even though the
    response didn't arrive -- never blind-resubmit). `RateLimitError`/`WashTradeError`/
    `AccountBlockedError`/`MarketClosedError` are caught, logged as an incident, and
    turned into a `None` return rather than raised, so one bad order never takes down the
    rest of a cycle's batch. Since 2026-10-03 so is any other `BrokerError`: Alpaca's
    "insufficient qty available" arrives as a plain one, and uncaught it would end the
    whole cycle with a position's stops already canceled. On success, records the order
    -- and, for an `"oto"` entry, its resting stop-loss leg as its own `leg="stop"` row
    (Phase 4) -- and any immediate fill, then returns the submitted `Order`.

    `tranche_index`/`tranche_total` (Phase 5) are recorded on the entry row only -- a
    resting stop leg is never itself split, so its own row always stays the ledger's
    default `1`/`1` regardless of what its parent entry's tranche is.

    `leg` (Phase 5) is the caller's own label for *this* submission -- `"entry"` (the
    default) for `build_entry_order`, `"exit"` for `build_close_order`/`build_trim_order`,
    `"stop"` for `build_replacement_stop_order`. This matters beyond bookkeeping:
    `IntradayLedger.latest_filled_entry_for`/`pending_tranche_for` both filter on
    `leg = 'entry'`, so a close/trim submitted through here without an explicit `leg`
    would otherwise masquerade as a fresh entry and corrupt both lookups.

    `exit_reason` is recorded on an exit's row (see `IntradayLedger.record_order`).
    """
    tradable_failure = check_order_tradable(request, broker)
    if tradable_failure is not None:
        ledger.record_incident("error", tradable_failure)
        return None

    try:
        submitted = broker.submit_order(request)
    except TimeoutError:
        submitted = broker.get_order_by_client_id(request.client_order_id)
        if submitted is None:
            message = f"timed out and no order found for {request.client_order_id}"
            ledger.record_incident("error", message)
            return None
    except (RateLimitError, WashTradeError, AccountBlockedError, MarketClosedError) as exc:
        ledger.record_incident("error", str(exc))
        return None
    except BrokerError as exc:  # any other rejection, e.g. "insufficient qty available"
        ledger.record_incident(
            "error", f"{request.symbol}: {request.side} order rejected by the broker: {exc}"
        )
        return None

    ledger.record_order(
        client_order_id=submitted.client_order_id,
        symbol=submitted.symbol,
        side=submitted.side,
        status=submitted.status,
        qty=submitted.qty,
        notional=submitted.notional,
        limit_price=submitted.limit_price,
        broker_order_id=submitted.id,
        submitted_at=submitted.submitted_at,
        proposal_id=proposal_id,
        tranche_index=tranche_index,
        tranche_total=tranche_total,
        leg=leg,
        exit_reason=exit_reason,
    )
    for child in submitted.legs or []:
        ledger.record_order(
            client_order_id=child.client_order_id,
            symbol=child.symbol,
            side=child.side,
            status=child.status,
            qty=child.qty,
            notional=child.notional,
            limit_price=child.limit_price,
            broker_order_id=child.id,
            submitted_at=child.submitted_at,
            proposal_id=proposal_id,
            leg="stop",
        )
    record_new_fill(submitted, ledger, now)
    return submitted


@dataclass(frozen=True)
class ExitAttempt:
    """What :func:`submit_exit` did. ``order`` is the exit that went out. Without one,
    ``failure`` says why, and a ``None`` failure means the position was already gone.
    ``position`` is what was held when the exit was built."""

    order: Order | None = None
    position: Position | None = None
    failure: str | None = None


def submit_exit(
    symbol: str,
    build_request: Callable[[Position], OrderRequest],
    broker: Broker,
    ledger: IntradayLedger,
    now: dt.datetime,
    *,
    reason: str,
    as_of: pd.Timestamp,
    cycle: str,
    stop_price: float | None,
    proposal_id: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ExitAttempt:
    """Closes or trims `symbol` without leaving it unprotected: cancel its stops, wait for
    the cancels to complete, re-read the position, send the exit, and put a stop back if
    the exit doesn't go out. Every close and trim goes through here (added 2026-10-03,
    before Stage 2 first sends real orders; the shadow broker hides all of this by
    canceling and filling instantly). `reason` is recorded on the exit as its
    `exit_reason`, for the reflection :func:`sync_open_orders` writes if the fill arrives
    later.

    1. Every order still working in the symbol is canceled, and each cancel waited out
       the way :func:`cancel_resting_stops` does: the stops, an earlier exit that never
       filled (its limit missed; it still holds the shares, so a second exit would be
       rejected), and an entry tranche still working (an opposite-side order Alpaca
       would treat as a potential wash trade). Anything still canceling after the wait
       holds the exit back to the next cycle with an error incident: sent now it would
       be rejected for "insufficient qty available", and if a stop then fired the exit
       would sell shares no longer held. No stop is re-placed here, since the old one is
       still working (it either cancels or fills).
    2. The position is re-read from the broker. A stop that fired before its cancel
       landed has already sold some or all of the shares, so the exit is built from what
       is still held, never from the caller's earlier read (closing the old quantity
       would open a short). Nothing held: a warning, and no exit.
    3. `build_request(position)` builds the exit; a `ValueError` (a trim too small for a
       whole share) counts as no exit. Then :func:`submit_order_with_fault_isolation`.
    4. If no exit went out and stops were canceled in step 1, a standalone stop for the
       whole position is re-placed at `stop_price` (the entry's invalidation price), so a
       rejected exit doesn't leave the position unguarded until the next cycle. A
       missing `stop_price`, or a stop the broker rejects too, is an error incident.
    """
    canceled = [row["client_order_id"] for row in ledger.open_stop_legs_for(symbol)]
    _cancel_and_settle(ledger.open_orders_for(symbol), broker, ledger, now, sleep)
    still_canceling = ledger.open_orders_for(symbol)
    if still_canceling:
        failure = (
            f"{symbol}: {len(still_canceling)} order(s) still canceling after "
            f"{_CANCEL_SETTLE_POLLS * _CANCEL_POLL_SECONDS:g}s; exit held back to the next cycle"
        )
        ledger.record_incident("error", failure)
        return ExitAttempt(failure=failure)

    position = next((p for p in broker.list_positions() if p.symbol == symbol), None)
    if position is None:
        ledger.record_incident(
            "warning", f"{symbol}: nothing left to exit once its stops were canceled"
        )
        return ExitAttempt()

    try:
        request = _unused_client_order_id(build_request(position), ledger)
    except ValueError as exc:
        failure = str(exc)
    else:
        submitted = submit_order_with_fault_isolation(
            request, broker, ledger, now, proposal_id=proposal_id, leg="exit", exit_reason=reason
        )
        if submitted is not None:
            return ExitAttempt(order=submitted, position=position)
        failure = f"{symbol}: exit order failed"

    if canceled:
        _restore_stop(position, stop_price, broker, ledger, now, as_of, cycle, proposal_id)
    return ExitAttempt(position=position, failure=failure)


def _unused_client_order_id(request: OrderRequest, ledger: IntradayLedger) -> OrderRequest:
    """`request` under an id the ledger hasn't used yet, suffixed ``-r1``, ``-r2``... A
    second exit in the same cycle (the first one's limit missed and it was just
    canceled) would otherwise reuse the first one's id, and the broker answers a reused
    id with the old, canceled order rather than placing a new one (`FakeBroker` returns
    it; Alpaca's duplicate-id 422 falls back to looking it up). Entries keep their fixed
    ids, which is what stops a re-run cycle from buying twice; an exit can't sell twice,
    since :func:`submit_exit` cancels every working order and re-reads the position
    first."""
    client_order_id, attempt = request.client_order_id, 0
    while ledger.get_order(client_order_id) is not None:
        attempt += 1
        client_order_id = f"{request.client_order_id}-r{attempt}"
    if client_order_id == request.client_order_id:
        return request
    return replace(request, client_order_id=client_order_id)


def _restore_stop(
    position: Position,
    stop_price: float | None,
    broker: Broker,
    ledger: IntradayLedger,
    now: dt.datetime,
    as_of: pd.Timestamp,
    cycle: str,
    proposal_id: int | None,
) -> None:
    """Step 4 of :func:`submit_exit`."""
    symbol, qty = position.symbol, abs(position.qty)
    if stop_price is None:
        ledger.record_incident(
            "error",
            f"{symbol}: no exit went out after its stops were canceled, and no invalidation "
            "price is on file to re-place one; position unprotected",
        )
        return
    restored = place_protective_stop(
        position,
        qty,
        stop_price,
        broker,
        ledger,
        now,
        as_of=as_of,
        cycle=cycle,
        proposal_id=proposal_id,
    )
    if restored is None:
        ledger.record_incident(
            "error",
            f"{symbol}: no exit went out after its stops were canceled, and the broker "
            "refused a replacement stop; position unprotected",
        )
    else:
        ledger.record_incident(
            "warning",
            f"{symbol}: no exit went out after its stops were canceled; re-placed a stop "
            f"for {qty:g} shares at {round_to_tick(stop_price):.2f}",
        )


def place_protective_stop(
    position: Position,
    qty: float,
    stop_price: float,
    broker: Broker,
    ledger: IntradayLedger,
    now: dt.datetime,
    *,
    as_of: pd.Timestamp,
    cycle: str,
    proposal_id: int | None = None,
) -> Order | None:
    """A standalone `day` stop for `qty` shares of `position` at `stop_price`, on the
    closing side, under a client order id the ledger hasn't used, recorded as a
    `leg="stop"` row. `None` if the broker refused it (already an incident)."""
    side: OrderSide = "sell" if position.side == "long" else "buy"
    request = _unused_client_order_id(
        build_replacement_stop_order(position.symbol, side, qty, stop_price, as_of, cycle),
        ledger,
    )
    return submit_order_with_fault_isolation(
        request, broker, ledger, now, proposal_id=proposal_id, leg="stop"
    )


@dataclass(frozen=True)
class OrderSync:
    """What :func:`sync_open_orders` changed, by `client_order_id`."""

    updated: list[str]
    filled: list[str]
    reflected: list[str]


def sync_open_orders(
    broker: Broker,
    ledger: IntradayLedger,
    config: IntradayConfig,
    as_of: pd.Timestamp,
    now: dt.datetime,
) -> OrderSync:
    """Brings the ledger up to date with the broker for every order it still shows open:
    the broker's current status, and any fill beyond what is already recorded. Then
    writes the reflection for any exit or stop that has filled since. Added 2026-10-03,
    a Stage 2 blocker: S4 used to record a fill only from the submit response. The
    shadow broker fills instantly, but on the real account every paper-sleeve order came
    back from `submit_order` still open and filled 1-4 s later (`docs/alpaca_notes.md`).
    Without this, every real entry would stay "accepted" in the ledger: the monitor would
    find no filled entry to enforce a time stop on, no later tranche would go out
    (`pending_tranche_for`), closes would get no reflection, and a stop that fired
    between cycles would never be recorded. The same job `live/mark.py` does for s0/s3.

    Runs first in every job that acts on positions (`run_cycle`, the `monitor` command,
    `run_carry`) and before `run_eod_review` reads the day's fills.

    - An order the broker has no record of is left as it is. S4's orders always live on
      the broker that took them; the exception is Stage 1's shadow rows once Stage 2
      switches to the real account, and the stop legs among them are cleared by the
      orphan sweep (`jobs.py::_cancel_orphaned_stops`), which marks a leg the broker
      doesn't know as canceled.
    - A lookup that raises is skipped and asked again next time; it never stops the job.
    - Reflections (`IntradayLedger.unreflected_exits`): an exit gets one under the
      `exit_reason` it was sent with. A symbol's filled stop legs get one between them,
      under ``"invalidation"``: one stop-out is one trade, however many tranches and
      adds each brought their own stop (2026-10-16: one NVDA stop-out, three legs, had
      been counted as three trades). Plus one ``"stopout:{symbol}"`` incident, which the
      cooldown rule reads (`IntradayLedger.last_stopout_times`), timed when this sync
      notices the fill, so the cooldown can only run long, never short. The reflection
      is dated `as_of`, the day of the sync that noticed the fill.
    """
    updated: list[str] = []
    filled: list[str] = []
    for row in ledger.open_orders():
        client_order_id = row["client_order_id"]
        try:
            order = broker.get_order_by_client_id(client_order_id)
        except Exception:  # noqa: BLE001 - asked again next sync; never ends the job
            continue
        if order is None:
            continue
        if record_new_fill(order, ledger, now):
            filled.append(client_order_id)
        if order.status != row["status"]:
            ledger.record_order(
                client_order_id,
                row["symbol"],
                row["side"],
                status=order.status,
                broker_order_id=order.id or row["broker_order_id"],
                leg=row["leg"],
            )
            updated.append(client_order_id)

    reflected: list[str] = []
    stops_by_symbol: dict[str, list[str]] = {}
    for row in ledger.unreflected_exits():
        if row["leg"] == "stop":
            stops_by_symbol.setdefault(row["symbol"], []).append(row["client_order_id"])
            continue
        written = record_mechanical_reflection(
            row["symbol"], row["client_order_id"], row["exit_reason"], ledger, config, as_of
        )
        if written is not None:
            reflected.append(row["client_order_id"])
    for symbol, (first, *others) in stops_by_symbol.items():
        written = record_mechanical_reflection(
            symbol, first, "invalidation", ledger, config, as_of, also_closed_by=others
        )
        if written is None:
            continue
        reflected.extend([first, *others])
        ledger.record_incident("warning", f"stopout:{symbol}")
    return OrderSync(updated=updated, filled=filled, reflected=reflected)

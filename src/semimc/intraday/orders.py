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

import pandas as pd

from ..config import IntradayConfig
from ..execution.broker import (
    AccountBlockedError,
    Broker,
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

    `tranche` (Phase 5) is passed straight through to `intraday_client_order_id` -- this
    function doesn't divide `size_pct` itself; a caller building tranche N > 1 passes an
    already-divided `proposal` (`jobs.py::run_cycle` does this via `proposal.model_copy`)
    and only uses `tranche` to keep that piece's `client_order_id` distinct.
    """
    if proposal.action not in OPEN_OR_ADD_ACTIONS:
        raise ValueError(f"build_entry_order only builds open/add orders, got {proposal.action!r}")

    # side/size_pct/limit_price/invalidation_price are all required for open/add by
    # TradeProposal's own model_validator, so they're never None here.
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


def find_and_cancel_resting_stop(symbol: str, broker: Broker, ledger: IntradayLedger) -> str | None:
    """Cancels `symbol`'s resting protective stop leg, if any, and returns its
    `client_order_id` (or `None` if there was none to cancel).

    This is a correctness requirement for closing or trimming a position, not cleanup:
    only bracket/OCO/trailing-stop orders are confirmed wash-trade-exempt at Alpaca
    (Phase 3's direct quote, `docs/alpaca_notes.md`), so a plain "simple" close/trim order
    left to collide with a still-resting stop-loss leg in the same symbol would likely be
    rejected -- the brief's own "never leave an opposite-side order open in the same
    symbol; cancel before reversing."

    Tolerates the leg already being canceled or otherwise gone at the broker (`KeyError`/
    `ValueError` from `cancel_order`) the same forgiving way real Alpaca's own
    `cancel_order` already is (`docs/alpaca_notes.md`: canceling an already-canceled
    order succeeds silently) -- nothing left to cancel is not a failure.
    """
    leg = ledger.open_stop_leg_for(symbol)
    if leg is None:
        return None
    with contextlib.suppress(KeyError, ValueError):
        broker.cancel_order(leg["broker_order_id"])
    return leg["client_order_id"]


def build_close_order(
    position: Position,
    as_of: pd.Timestamp,
    cycle: str,
    price: float,
    config: IntradayConfig,
    attempt: int = 0,
) -> OrderRequest:
    """Fully exits `position`. Qty and side come from the real, currently-held
    `Position` -- never from a proposal, which needs neither `side` nor `size_pct` to
    close (the schema doesn't require either for `action="close"`).

    `order_class="simple"` -- exiting needs no new stop leg. The resting stop-loss leg
    from the original entry must be canceled first via `find_and_cancel_resting_stop`
    (not done here -- see the module docstring on sequencing being a job's job).
    """
    side: OrderSide = "sell" if position.side == "long" else "buy"
    return OrderRequest(
        symbol=position.symbol,
        side=side,
        type="limit",
        time_in_force="day",
        client_order_id=intraday_client_order_id(as_of, cycle, position.symbol, attempt),
        qty=abs(position.qty),
        limit_price=collared_limit_price(price, side, config.risk.equity_cost_bps_per_side),
    )


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

    Like `build_close_order`, the resting stop-loss leg must be canceled first
    (`find_and_cancel_resting_stop`), and -- unlike a close -- the remainder needs a
    fresh, smaller replacement stop afterward (`build_replacement_stop_order`) so it is
    never left resting unprotected. Neither is sequenced here; see the module docstring.
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
) -> Order | None:
    """Mirrors `live/execute.py::run_execute`'s per-order try/except exactly.

    `check_order_tradable` runs first, same as `run_execute`. A `TimeoutError` falls back
    to `get_order_by_client_id` (the request may have gone through even though the
    response didn't arrive -- never blind-resubmit). `RateLimitError`/`WashTradeError`/
    `AccountBlockedError`/`MarketClosedError` are caught, logged as an incident, and
    turned into a `None` return rather than raised, so one bad order never takes down the
    rest of a cycle's batch. On success, records the order -- and, for an `"oto"` entry,
    its resting stop-loss leg as its own `leg="stop"` row (Phase 4) -- and any immediate
    fill, then returns the submitted `Order`.

    `tranche_index`/`tranche_total` (Phase 5) are recorded on the entry row only -- a
    resting stop leg is never itself split, so its own row always stays the ledger's
    default `1`/`1` regardless of what its parent entry's tranche is.

    `leg` (Phase 5) is the caller's own label for *this* submission -- `"entry"` (the
    default) for `build_entry_order`, `"exit"` for `build_close_order`/`build_trim_order`,
    `"stop"` for `build_replacement_stop_order`. This matters beyond bookkeeping:
    `IntradayLedger.latest_filled_entry_for`/`pending_tranche_for` both filter on
    `leg = 'entry'`, so a close/trim submitted through here without an explicit `leg`
    would otherwise masquerade as a fresh entry and corrupt both lookups.
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
    if submitted.filled_qty > 0 and submitted.filled_avg_price is not None:
        ledger.record_fill(
            submitted.client_order_id,
            qty=submitted.filled_qty,
            price=submitted.filled_avg_price,
            filled_at=submitted.filled_at or now,
        )
    return submitted

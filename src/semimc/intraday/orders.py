"""Turning an already-*accepted* `open`/`add` `TradeProposal` into a real order at
Alpaca, with a real stop resting at the broker (S4, `BRIEF_INTRADAY.md`, Phase 3).

`close`/`trim` order construction, tranche-splitting, and the placebo agent are
deliberately not here -- see the Phase 3 plan's "Scope" section for why each is deferred
to Phase 4 rather than half-built now.

Reuses `execution/orders.py`'s low-level helpers (`collared_limit_price`,
`sequence_orders`) rather than a second copy, and mirrors `live/execute.py::run_execute`'s
per-order fault-isolation shape exactly, so a fault in one order in a cycle's batch never
takes down the rest of it.
"""

from __future__ import annotations

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
    RateLimitError,
    WashTradeError,
)
from ..execution.orders import collared_limit_price
from ..execution.pretrade import check_order_tradable
from .ledger import IntradayLedger
from .proposals import OPEN_OR_ADD_ACTIONS, TradeProposal


def intraday_client_order_id(as_of: pd.Timestamp, cycle: str, symbol: str, attempt: int = 0) -> str:
    """Deterministic per (as_of, cycle, symbol, attempt) -- the brief's own "deterministic
    `client_order_id` per cycle, symbol, and attempt" idempotency rule. There's no
    `Decision` to hash off the way `execution/orders.py::client_order_id` has; this hashes
    the tuple itself directly."""
    return f"s4-{pd.Timestamp(as_of):%Y%m%d}-{cycle}-{symbol}-{attempt}"


def build_entry_order(
    proposal: TradeProposal,
    as_of: pd.Timestamp,
    cycle: str,
    price: float,
    equity: float,
    config: IntradayConfig,
    attempt: int = 0,
) -> OrderRequest:
    """One `open`/`add` proposal, one order, with a real stop resting at the broker the
    instant it fills -- this is what removes the need for a minute-level monitor process
    between GH Actions runs.

    Raises `ValueError` for any other `proposal.action` (see the module docstring for why
    close/trim aren't built here) or if `size_pct` rounds to fewer than one whole share.

    Whole-share `qty` always, never `notional` -- this project's existing sizing
    convention (`execution/orders.py::build_orders` does the same), and a hedge pending
    real confirmation of whether an OTO stop-loss leg even accepts a fractional qty
    (unconfirmed anywhere checked -- see `docs/alpaca_notes.md`'s "still to verify" list).
    """
    if proposal.action not in OPEN_OR_ADD_ACTIONS:
        raise ValueError(f"build_entry_order only builds open/add orders, got {proposal.action!r}")

    # side/size_pct/limit_price/invalidation_price are all required for open/add by
    # TradeProposal's own model_validator, so they're never None here.
    qty = math.floor(proposal.size_pct * equity / price)
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
        client_order_id=intraday_client_order_id(as_of, cycle, proposal.symbol, attempt),
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
        stop_loss_price=proposal.invalidation_price,
    )


def submit_order_with_fault_isolation(
    request: OrderRequest,
    broker: Broker,
    ledger: IntradayLedger,
    now: dt.datetime,
    proposal_id: int | None = None,
) -> Order | None:
    """Mirrors `live/execute.py::run_execute`'s per-order try/except exactly.

    `check_order_tradable` runs first, same as `run_execute`. A `TimeoutError` falls back
    to `get_order_by_client_id` (the request may have gone through even though the
    response didn't arrive -- never blind-resubmit). `RateLimitError`/`WashTradeError`/
    `AccountBlockedError`/`MarketClosedError` are caught, logged as an incident, and
    turned into a `None` return rather than raised, so one bad order never takes down the
    rest of a cycle's batch. On success, records the order (and any immediate fill) to the
    ledger and returns the submitted `Order`.
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
    )
    if submitted.filled_qty > 0 and submitted.filled_avg_price is not None:
        ledger.record_fill(
            submitted.client_order_id,
            qty=submitted.filled_qty,
            price=submitted.filled_avg_price,
            filled_at=submitted.filled_at or now,
        )
    return submitted

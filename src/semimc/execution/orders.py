"""Turning a :class:`~semimc.strategy.decide.Decision` into concrete broker orders.

The only place a target-weight decision becomes :class:`~semimc.execution.broker.
OrderRequest` objects -- the deterministic ``client_order_id`` convention, the minimum-
trade threshold, fractional/whole-share rounding, the collared marketable-limit price, and
sell-before-buy sequencing all live here exactly once, so a live job, a dry-run replay, and
a test never risk two slightly different reimplementations of "how big an order."

Deliberately broker-agnostic: this module never imports a :class:`~semimc.execution.
broker.Broker`, only the plain dataclasses in ``broker.py``. Whatever calls this (a later
phase's ``live/execute.py``) is responsible for supplying today's last prices and asset
fractionability -- this module only turns numbers already in hand into orders.
"""

from __future__ import annotations

import math

import pandas as pd

from ..config import Config
from ..strategy.decide import Decision
from .broker import OrderRequest, OrderSide, Position


def client_order_id(decision: Decision, symbol: str, as_of: pd.Timestamp | None = None) -> str:
    """Deterministic, well under Alpaca's 128-char limit.

    Built from the decision's own ``snapshot_hash`` rather than a random id, so re-running
    the same day's decide/execute step for the same decision reproduces the exact same id
    -- the entire point of freezing a decision before trading it. What a caller does with a
    retried submit differs by broker: ``FakeBroker`` treats a duplicate id as idempotent
    and returns the original order directly. Real Alpaca does **not** -- it rejects a
    duplicate ``client_order_id`` with HTTP 422 -- so ``AlpacaPaperBroker.submit_order``
    catches that rejection and falls back to looking the order up by this same id. Either
    way, a retried submit for the same decision resolves to the one order that decision
    produced, never a second one.

    ``as_of`` defaults to ``decision.as_of`` -- the common case, where a decision is
    decided and executed the same day. A live job that keeps executing toward the *same*
    frozen decision on later days (``live/execute.py``, once a sleeve's rebalance cadence
    is longer than a day) passes the actual day of this execute attempt instead: a stale,
    unfilled order from an earlier day may have already been canceled by ``live/mark.py``'s
    reprice pruning, and reusing that earlier day's id here would collide with it -- a real
    Alpaca duplicate-``client_order_id`` rejection whose fallback lookup would silently
    return the old, canceled order instead of submitting a fresh one for the remaining gap.
    Keying the id off the *attempt's* own day instead keeps same-day retries idempotent
    (unchanged) while giving each new day's attempt at the same decision its own id.
    """
    stamp = pd.Timestamp(as_of) if as_of is not None else decision.as_of
    return f"{decision.sleeve}-{stamp:%Y%m%d}-{symbol}-{decision.snapshot_hash[:16]}"


def target_notional(decision: Decision, equity: float) -> pd.Series:
    """Dollar target per symbol implied by the decision's weights.

    Weights need not sum to 1 -- the remainder is implicit cash, per ``Decision``'s own
    docstring, so this is simply ``weights * equity``, not a call that needs to know about
    cash itself.
    """
    return decision.weights * equity


def round_to_tick(price: float) -> float:
    """Alpaca's own minimum-pricing-increment rule: $0.01 at or above $1, $0.0001 below
    it -- anything finer is rejected outright (``invalid limit_price ... sub-penny
    increment does not fulfill minimum pricing criteria``, a real 422 this project hit on
    2026-09-23 from a plain ``price + collar_bps`` offset with no guarantee of landing on
    a valid increment). Public: also applied to ``intraday/orders.py``'s LLM-proposed
    ``invalidation_price``, which has the identical problem for the same reason -- an
    arbitrary-precision float with nothing rounding it before it reaches the broker."""
    return round(price, 2) if price >= 1.0 else round(price, 4)


def collared_limit_price(price: float, side: OrderSide, collar_bps: float) -> float:
    """A marketable limit: priced past the quote by ``collar_bps``, in the direction that
    makes it likely to fill, bounded so a fast-moving market can't fill it arbitrarily far
    from the last known price. Public: ``intraday/orders.py`` (S4, Phase 3) reuses this
    exact formula rather than a second copy."""
    offset = price * collar_bps / 10_000
    raw = price + offset if side == "buy" else price - offset
    return round_to_tick(raw)


def sequence_orders(orders: list[OrderRequest]) -> list[OrderRequest]:
    """Sells before buys, so a sell's cash is available before a buy needs it.

    This project never trades on margin (``paper.s3_max_leverage`` defaults to 1.0, no
    leverage anywhere), so submitting a buy before the sell funding it has landed would
    either fail on buying power or silently rely on margin the config doesn't intend.
    """
    return sorted(orders, key=lambda order: 0 if order.side == "sell" else 1)


def build_orders(
    decision: Decision,
    positions: dict[str, Position],
    prices: pd.Series,
    equity: float,
    config: Config,
    fractionable: dict[str, bool] | None = None,
    as_of: pd.Timestamp | None = None,
) -> list[OrderRequest]:
    """The trades needed to move from ``positions`` toward ``decision.weights``.

    A symbol whose required trade is smaller than ``config.paper.minimum_trade_pct`` of
    equity is skipped entirely -- churn on a rebalance too small to matter is worse than
    the (bounded, capped) tracking error of not making it. A symbol with no usable price is
    skipped rather than guessed at; a live job logs that as an incident, not silently here.
    Every weight this project's strategy sleeves produce is non-negative (never a short),
    so a sell is capped at the currently held long quantity as a defensive invariant, not
    because it is expected to trigger.

    ``as_of`` is forwarded to :func:`client_order_id` unchanged -- see that function's
    docstring for why a live job executing toward an older decision on a later day should
    pass today's date here rather than leaving it at ``decision.as_of``.
    """
    fractionable = fractionable or {}
    targets = target_notional(decision, equity)
    symbols = sorted(set(targets.index) | set(positions))
    orders: list[OrderRequest] = []
    for symbol in symbols:
        price = prices.get(symbol)
        if price is None or not math.isfinite(price) or price <= 0:
            continue
        current_qty = max(positions[symbol].qty, 0.0) if symbol in positions else 0.0
        delta_notional = float(targets.get(symbol, 0.0)) - current_qty * price
        if abs(delta_notional) < config.paper.minimum_trade_pct * equity:
            continue

        side: OrderSide = "buy" if delta_notional > 0 else "sell"
        qty = abs(delta_notional) / price
        # Alpaca accepts at most 9 decimal places on `qty`; a raw division can produce
        # far more (`docs/alpaca_notes.md`'s "Fractional and notional orders" -- found
        # 2026-09-23, the same defect class as collared_limit_price's own sub-penny
        # rejection). 6 decimals is comfortably inside that limit with no measurable
        # effect on notional; a non-fractionable symbol still floors to a whole share.
        qty = math.floor(qty) if not fractionable.get(symbol, True) else round(qty, 6)
        if side == "sell":
            qty = min(qty, current_qty)
        if qty <= 0:
            continue

        orders.append(
            OrderRequest(
                symbol=symbol,
                side=side,
                type="limit",
                time_in_force="day",
                client_order_id=client_order_id(decision, symbol, as_of),
                qty=qty,
                limit_price=collared_limit_price(price, side, config.paper.execution.collar_bps),
            )
        )
    return sequence_orders(orders)

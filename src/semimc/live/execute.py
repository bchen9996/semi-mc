"""The ``execute`` job: turn a frozen :class:`~semimc.strategy.decide.Decision` (already
recorded by ``live/decide.py``) into real broker orders.

Never invents a decision -- ``execute`` refuses if ``decide`` has never run for this
sleeve at all, per the brief's own "freeze before trading" ordering. It acts on the
sleeve's *most recently frozen* decision, not one dated exactly ``as_of``: a
longer-than-daily rebalance cadence means most days have no decision dated "today."

The full rebalance toward a decision happens once, on its first execute attempt (recorded
in the ledger's ``executions`` table even when it needed no orders). Every later attempt,
on a later day or a late duplicate the same day, only retries what that rebalance left
unfinished: an order that ended unfilled, or one that couldn't be placed. Price drift is
not traded; it waits for the next decision. Found 2026-10-02: this used to re-target
every holding on every run, assuming positions "already sit at target" -- so a monthly
sleeve rebalanced its drift daily, unlike the monthly backtests behind it, and GitHub's
late copy of the job bought QCOM at 13:56 Central after it dipped past the 0.5% line. Everything
about *how big an order* stays in ``execution/orders.py``; this module only supplies what
that function needs (positions, prices, equity, fractionability) and records what the
broker did.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from ..data import MarketData
from ..execution.broker import (
    AccountBlockedError,
    Broker,
    MarketClosedError,
    Order,
    OrderRequest,
    Position,
    RateLimitError,
    WashTradeError,
)
from ..execution.orders import build_orders
from ..execution.pretrade import PretradeContext, check_order_tradable, check_pretrade
from ..strategy.decide import Decision
from .ledger import Ledger
from .mark import as_paper_local


@dataclass
class ExecutionResult:
    orders: list[OrderRequest] = field(default_factory=list)
    submitted: list[Order] = field(default_factory=list)
    pretrade_failures: list[str] = field(default_factory=list)
    order_failures: list[tuple[OrderRequest, str]] = field(default_factory=list)
    skipped_reason: str | None = None


def _decision_from_row(row: dict) -> Decision:
    return Decision(
        as_of=pd.Timestamp(row["as_of"]),
        sleeve=row["sleeve"],
        weights=pd.Series(row["weights"], dtype=float),
        diagnostics=row["diagnostics"],
    )


# Order statuses that end an order without filling it. Anything else that isn't
# "filled" is still working at the broker, and gets left alone rather than duplicated.
_ENDED_UNFILLED = frozenset(
    {"canceled", "expired", "rejected", "done_for_day", "replaced", "stopped", "suspended"}
)


def unfinished_symbols(decision_orders: list[dict], attempts: list[dict]) -> set[str]:
    """Symbols a decision's rebalance still owes a trade: every order placed toward it for
    that symbol ended unfilled (canceled at the close, pruned by `live/mark.py` for a
    reprice, rejected), or an attempt couldn't place one at all (an order failure, or no
    price). A symbol with a filled order is done, and one with an order still working is
    left alone. A partly filled order that was then canceled counts as unfinished: the
    retry trades whatever gap remains."""
    statuses: dict[str, list[str]] = {}
    for row in decision_orders:
        statuses.setdefault(row["symbol"], []).append(row["status"])
    failed = {symbol for attempt in attempts for symbol in attempt["failed_symbols"]}
    unfinished: set[str] = set()
    for symbol in set(statuses) | failed:
        seen = statuses.get(symbol, [])
        if "filled" in seen or any(status not in _ENDED_UNFILLED for status in seen):
            continue
        unfinished.add(symbol)
    return unfinished


def _unpriced_symbols(
    decision: Decision, positions: dict[str, Position], prices: pd.Series
) -> list[str]:
    """Symbols `build_orders` skips for want of a usable price that it might otherwise
    have traded (a target weight, or a holding) -- recorded so the retry picks them up."""
    candidates = {s for s, w in decision.weights.items() if w > 0} | set(positions)
    unpriced = []
    for symbol in sorted(candidates):
        price = prices.get(symbol)
        if price is None or not math.isfinite(price) or price <= 0:
            unpriced.append(symbol)
    return unpriced


def run_execute(
    sleeve: str,
    config,  # noqa: ANN001
    broker: Broker,
    ledger: Ledger,
    market: MarketData,
    tickers: list[str],
    as_of: pd.Timestamp,
    now,  # noqa: ANN001 -- dt.datetime, already localized to config.paper.execution.timezone
    *,
    dry_run: bool = False,
) -> ExecutionResult:
    as_of = pd.Timestamp(as_of)
    decision_row = ledger.last_decision(sleeve)
    if decision_row is None:
        return ExecutionResult(
            skipped_reason=(
                f"no decision has ever been recorded for {sleeve!r}; "
                "run `semimc paper decide` first"
            )
        )

    failures = check_pretrade(PretradeContext(sleeve=sleeve, now=now, config=config, broker=broker))
    if failures:
        ledger.record_incident("warning", "; ".join(failures), sleeve)
        return ExecutionResult(pretrade_failures=failures)

    decision = _decision_from_row(decision_row)
    # The full rebalance happens once per decision; every later attempt only retries what
    # that rebalance left unfinished. Any order already placed toward the decision counts
    # as an attempt too: the 2026-10-01 decision traded before the executions table
    # existed. See the module docstring for why drift isn't traded.
    attempts = ledger.executions_for(sleeve, decision.snapshot_hash)
    decision_orders = ledger.orders_for_decision(sleeve, decision.snapshot_hash)
    retry_symbols: set[str] | None = None
    if attempts or decision_orders:
        retry_symbols = unfinished_symbols(decision_orders, attempts)
        if not retry_symbols:
            return ExecutionResult(
                skipped_reason=(
                    f"the {decision.as_of.date()} decision for {sleeve!r} is already "
                    "executed and nothing it ordered is left unfilled; drift waits for the "
                    "next rebalance"
                )
            )

    account = broker.get_account()
    positions = {p.symbol: p for p in broker.list_positions()}
    prices = market.spot(tickers, as_of=pd.Timestamp(now))
    fractionable = {t: broker.get_asset(t).fractionable for t in tickers}

    # as_of=as_of (today's date), not decision.as_of: this attempt may be executing toward
    # a decision frozen days or weeks ago, and a same-decision order attempt from an
    # earlier day may already have been canceled by live/mark.py's reprice pruning. Keying
    # the id off today keeps same-day retries idempotent while giving each new day's
    # attempt at the same decision its own id -- see client_order_id's own docstring.
    orders = build_orders(
        decision, positions, prices, account.equity, config, fractionable, as_of=as_of
    )
    unpriced = _unpriced_symbols(decision, positions, prices)
    if retry_symbols is not None:
        orders = [order for order in orders if order.symbol in retry_symbols]
        unpriced = [symbol for symbol in unpriced if symbol in retry_symbols]
    if dry_run:
        return ExecutionResult(orders=orders)
    for symbol in unpriced:
        ledger.record_incident(
            "warning", f"{symbol}: no price to size an order; retried next execute", sleeve
        )

    result = ExecutionResult(orders=orders)
    for request in orders:
        tradable_failure = check_order_tradable(request, broker)
        if tradable_failure is not None:
            ledger.record_incident("error", tradable_failure, sleeve)
            result.order_failures.append((request, tradable_failure))
            continue
        try:
            submitted = broker.submit_order(request)
        except TimeoutError:
            # The request may have gone through even though the response didn't arrive --
            # client_order_id is deterministic for exactly this reason (its own docstring:
            # "look it up by client_order_id, don't resubmit"). Recover by looking it up
            # rather than assuming failure.
            submitted = broker.get_order_by_client_id(request.client_order_id)
            if submitted is None:
                message = f"timed out and no order found for {request.client_order_id}"
                ledger.record_incident("error", message, sleeve)
                result.order_failures.append((request, message))
                continue
        except (RateLimitError, WashTradeError, AccountBlockedError, MarketClosedError) as exc:
            # One order's broker-level fault must not take down the rest of the batch --
            # the same reasoning check_order_tradable's failures above already follow.
            ledger.record_incident("error", str(exc), sleeve)
            result.order_failures.append((request, str(exc)))
            continue

        ledger.record_order(sleeve, decision.snapshot_hash, request, submitted)
        if submitted.filled_qty > 0 and submitted.filled_avg_price is not None:
            ledger.record_fill(
                request.client_order_id,
                qty=submitted.filled_qty,
                price=submitted.filled_avg_price,
                filled_at=submitted.filled_at or as_paper_local(now, config),
            )
        result.submitted.append(submitted)

    ledger.record_execution(
        sleeve,
        decision.snapshot_hash,
        as_of,
        mode="rebalance" if retry_symbols is None else "retry",
        orders_submitted=len(result.submitted),
        failed_symbols=[request.symbol for request, _ in result.order_failures] + unpriced,
    )
    account = broker.get_account()
    positions = {p.symbol: p for p in broker.list_positions()}
    ledger.record_snapshot(sleeve, as_of, account.equity, account.cash, positions)
    return result

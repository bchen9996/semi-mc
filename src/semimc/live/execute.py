"""The ``execute`` job: turn a frozen :class:`~semimc.strategy.decide.Decision` (already
recorded by ``live/decide.py``) into real broker orders.

Never invents a decision -- ``execute`` refuses if ``decide`` has never run for this
sleeve at all, per the brief's own "freeze before trading" ordering. It acts on the
sleeve's *most recently frozen* decision, not one dated exactly ``as_of``: a
longer-than-daily rebalance cadence means most days have no decision dated "today," and
``execute`` running daily against last month's still-current decision (most days a no-op,
since positions already sit at target) is the intended steady state, not a gap. Everything
about *how big an order* stays in ``execution/orders.py``; this module only supplies what
that function needs (positions, prices, equity, fractionability) and records what the
broker did.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..data import MarketData
from ..execution.broker import (
    AccountBlockedError,
    Broker,
    MarketClosedError,
    Order,
    OrderRequest,
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
    if dry_run:
        return ExecutionResult(orders=orders)

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

    account = broker.get_account()
    positions = {p.symbol: p for p in broker.list_positions()}
    ledger.record_snapshot(sleeve, as_of, account.equity, account.cash, positions)
    return result

"""S4's scheduled jobs (`BRIEF_INTRADAY.md`'s "Daily schedule"), Phase 5.

Five of the brief's eight named jobs, given the further split `tasks/todo.md`'s S4
addendum records: `preflight`, `preopen`, `open_batch`/`cycle` (one shared
implementation, `run_cycle`, since they are the same operation -- `open_batch` is just
the first `cycle` of the day), and `carry`. `monitor` (not in the brief's original list,
but matching its own separate `monitor.py` file) runs standalone on a tight, model-free
cron and is also the first thing `run_cycle` calls. `research`/`eod_review`/
`weekly_review` are Phase 6/9's jobs, not built here -- see the Phase 5 plan's "Context"
section for why.

Every `run_<verb>` function here is a plain function returning a result dataclass, never
raising for a routine outcome -- the same shape `live/{decide,execute,mark}.py` already
use, right down to taking the full `Config` (not just `Config.intraday`) as their own
`config` parameter does: `check_intraday_pretrade` needs the full object (it resolves
`kill_switch_path` via `Config.path()`), so every job function accepts `Config` and
narrows to `config.intraday` itself wherever a Phase 2-4 helper (`build_trader_context`,
`invoke_role`, `build_entry_order`, ...) expects the narrower type. No job trusts a
previous job's in-memory output: state passes through the ledger and the broker only, and
every job re-polls fresh broker/market-data state rather than assuming anything about a
prior run, the same "re-check fresh state" discipline `live/mark.py`'s own docstring
names explicitly.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from pydantic import ValidationError

from ..config import Config, IntradayConfig
from ..execution.broker import Broker, OrderSide, Position
from ..execution.market_data import AlpacaMarketDataClient
from .context import build_trader_context
from .ledger import IntradayLedger
from .llm import ClaudeCliClient, DailyBudgetExhausted, invoke_role
from .monitor import check_invalidation_and_time_stops
from .orders import (
    build_close_order,
    build_entry_order,
    build_replacement_stop_order,
    build_trim_order,
    find_and_cancel_resting_stop,
    submit_order_with_fault_isolation,
)
from .pretrade import IntradayPretradeContext, check_intraday_pretrade, check_spread_gate
from .proposals import OPEN_OR_ADD_ACTIONS, TradeProposal, ValidationContext, validate_proposal


def midpoint_price(quote: Any) -> float | None:
    """`None`-safe NBBO midpoint of a `Quote` -- public (not `_`-prefixed) because the
    `intraday monitor` CLI command (Phase 5) needs the identical price derivation this
    module already uses internally, rather than a second copy."""
    if quote is None or quote.bid_price is None or quote.ask_price is None:
        return None
    return (quote.bid_price + quote.ask_price) / 2.0


def quote_map(
    symbols: set[str], market_data: AlpacaMarketDataClient, now: dt.datetime
) -> dict[str, Any]:
    """Best-effort quotes for `symbols` -- a symbol with no coverage is simply absent,
    same "a single missing quote must not crash a whole report" tolerance
    `market_data.py::arrival_price` already documents."""
    quotes: dict[str, Any] = {}
    for symbol in symbols:
        quote = market_data.latest_quote(symbol, at=now)
        if quote is not None and quote.bid_price is not None and quote.ask_price is not None:
            quotes[symbol] = quote
    return quotes


def _apply_spread_gate(
    rows: list[dict[str, Any]],
    market_data: AlpacaMarketDataClient,
    config: IntradayConfig,
    ledger: IntradayLedger,
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[tuple[str, str]]]:
    """Splits `rows` (each with a `"symbol"` key) into what's currently tradable and
    what got dropped, logging a warning incident per drop. Shared by `run_preopen` and
    `run_cycle` so both apply the identical gate -- `run_preopen`'s result is never
    trusted by `run_cycle`, but the *logic* is the same either way."""
    tradable: list[dict[str, Any]] = []
    quotes: dict[str, Any] = {}
    dropped: list[tuple[str, str]] = []
    for row in rows:
        symbol = row["symbol"]
        quote = market_data.latest_quote(symbol, at=now)
        if quote is None or quote.bid_price is None or quote.ask_price is None:
            reason = f"{symbol}: no quote available"
            dropped.append((symbol, reason))
            ledger.record_incident("warning", reason)
            continue
        reason = check_spread_gate(
            symbol, quote.bid_price, quote.ask_price, quote.timestamp, now, config
        )
        if reason is not None:
            dropped.append((symbol, reason))
            ledger.record_incident("warning", reason)
            continue
        quotes[symbol] = quote
        tradable.append(row)
    return tradable, quotes, dropped


def _original_proposal_payload(ledger: IntradayLedger, symbol: str) -> dict[str, Any] | None:
    """The payload of the proposal that opened `symbol`'s currently-held position, if
    traceable -- what `run_cycle`'s trim path (the remainder's invalidation level) and
    `run_carry` (a carried position's horizon/confidence) both read."""
    entry = ledger.latest_filled_entry_for(symbol)
    if entry is None or entry["proposal_id"] is None:
        return None
    proposal_row = ledger.get_proposal(entry["proposal_id"])
    return proposal_row["payload"] if proposal_row is not None else None


def _turnover_so_far_usd(ledger: IntradayLedger, as_of: pd.Timestamp, equity: float) -> float:
    """Approximates today's turnover from proposed (not filled) notional of every
    accepted proposal so far -- the same accepted-proposals-today read
    `validate_proposal`'s own budget checks already use, so this stays consistent with
    what the validator enforces rather than a second, independently-derived figure."""
    accepted = [row for row in ledger.proposals_for(as_of) if row["status"] == "accepted"]
    total = 0.0
    for row in accepted:
        size_pct = row["payload"].get("size_pct")
        if size_pct is not None:
            total += size_pct * equity
    return total


# -- preflight --------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightResult:
    passed: bool
    failures: list[str] = field(default_factory=list)


def run_preflight(
    config: Config,
    broker: Broker,
    market_data: AlpacaMarketDataClient,
    ledger: IntradayLedger,
    now: dt.datetime,
) -> PreflightResult:
    """A canary, not a gate other jobs depend on -- every later job re-runs
    `check_intraday_pretrade` (or an order-level check) itself fresh, the same "no job
    trusts a prior job" discipline s0/s3 already use. Exists so a 06:45 GitHub Actions
    failure email catches a dead account or feed before the day's real jobs waste a
    cycle on it.

    Checks: `check_intraday_pretrade` (kill switch, trading_enabled, registered account,
    account status, market open), yesterday's reconciliation status (if any), and a
    market-data smoke test. "Playbook version loaded" (brief) is skipped -- no
    `PLAYBOOK.md` exists before Phase 9.
    """
    failures = list(
        check_intraday_pretrade(IntradayPretradeContext(now=now, config=config, broker=broker))
    )

    reconciliation = ledger.latest_reconciliation()
    if reconciliation is not None and reconciliation["status"] != "ok":
        failures.append(f"latest reconciliation status is {reconciliation['status']!r}, not 'ok'")

    try:
        market_data.latest_quote("SPY", at=now)
    except Exception as exc:
        failures.append(f"market data unreachable: {exc}")

    for failure in failures:
        ledger.record_incident("error", failure)

    return PreflightResult(passed=not failures, failures=failures)


# -- preopen ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PreopenResult:
    tradable: list[str] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)


def run_preopen(
    config: Config,
    market_data: AlpacaMarketDataClient,
    ledger: IntradayLedger,
    as_of: pd.Timestamp,
    now: dt.datetime,
) -> PreopenResult:
    """Refreshes a quote per watchlist symbol and runs `check_spread_gate`, logging a
    warning incident per drop -- read-only/logging only. `open_batch`/`cycle` re-run the
    identical gate themselves rather than trusting this result, so a missed or late
    `preopen` run never blocks trading. Headline refresh (brief) waits on Phase 6's news
    source.
    """
    watchlist = ledger.watchlist_for(as_of)
    tradable, _quotes, dropped = _apply_spread_gate(
        watchlist, market_data, config.intraday, ledger, now
    )
    return PreopenResult(tradable=[row["symbol"] for row in tradable], dropped=dropped)


# -- open_batch / cycle (shared implementation) ------------------------------------------


@dataclass
class CycleResult:
    proposals_seen: int = 0
    proposals_accepted: int = 0
    proposals_rejected: int = 0
    orders_submitted: list[str] = field(default_factory=list)
    order_failures: list[str] = field(default_factory=list)
    skipped_reason: str | None = None


def run_cycle(
    cycle: str,
    config: Config,
    broker: Broker,
    ledger: IntradayLedger,
    market_data: AlpacaMarketDataClient,
    llm_client: ClaudeCliClient,
    as_of: pd.Timestamp,
    now: dt.datetime,
) -> CycleResult:
    """One Trader cycle: monitor -> tranche continuation -> propose -> validate ->
    execute. `open_batch` and `cycle` (the CLI/workflow layer) both call this with a
    different `cycle` label -- `open_batch` is simply the first cycle of the day.

    Nothing here is a new order-construction primitive; every action reuses Phase 3-4's
    builders (`build_entry_order`/`build_close_order`/`build_trim_order`/
    `build_replacement_stop_order`) and Phase 2's context/LLM wrapper
    (`build_trader_context`/`invoke_role`). This function only sequences them.
    """
    result = CycleResult()
    intraday = config.intraday

    pretrade_failures = check_intraday_pretrade(
        IntradayPretradeContext(now=now, config=config, broker=broker)
    )
    if pretrade_failures:
        for failure in pretrade_failures:
            ledger.record_incident("error", failure)
        result.skipped_reason = "; ".join(pretrade_failures)
        return result

    account = broker.get_account()
    ledger.record_session_open_equity(as_of, account.equity)
    equity_at_session_open = ledger.session_open_equity(as_of) or account.equity

    positions = broker.list_positions()
    position_quotes = quote_map({p.symbol for p in positions}, market_data, now)
    prices = {s: midpoint_price(q) for s, q in position_quotes.items()}

    monitor_result = check_invalidation_and_time_stops(
        intraday, broker, ledger, prices, as_of, cycle, now
    )
    if monitor_result.closed:
        positions = broker.list_positions()

    # Tranche continuation: submit the next piece of any partially-tranched-in entry
    # (no new model call -- the original accepted proposal already justified the full
    # size; splitting is an execution technique, not a re-decision).
    for position in list(positions):
        pending = ledger.pending_tranche_for(position.symbol)
        if pending is None:
            continue
        proposal_row = None
        if pending["proposal_id"] is not None:
            proposal_row = ledger.get_proposal(pending["proposal_id"])
        price = prices.get(position.symbol)
        if proposal_row is None or price is None:
            continue
        try:
            original_proposal = TradeProposal(**proposal_row["payload"])
        except ValidationError:
            continue
        next_tranche = pending["tranche_index"] + 1
        per_tranche = original_proposal.model_copy(
            update={"size_pct": original_proposal.size_pct / pending["tranche_total"]}
        )
        try:
            entry_request = build_entry_order(
                per_tranche, as_of, cycle, price, account.equity, intraday, tranche=next_tranche
            )
        except ValueError as exc:
            result.order_failures.append(str(exc))
            continue
        submitted = submit_order_with_fault_isolation(
            entry_request,
            broker,
            ledger,
            now,
            proposal_id=pending["proposal_id"],
            tranche_index=next_tranche,
            tranche_total=pending["tranche_total"],
            leg="entry",
        )
        if submitted is not None:
            result.orders_submitted.append(submitted.client_order_id)
        else:
            result.order_failures.append(
                f"{position.symbol}: tranche {next_tranche} submission failed"
            )

    positions = broker.list_positions()
    positions_notional = {p.symbol: p.market_value for p in positions}

    watchlist = ledger.watchlist_for(as_of)
    tradable_watchlist, watchlist_quotes, _dropped = _apply_spread_gate(
        watchlist, market_data, intraday, ledger, now
    )
    prices.update({s: midpoint_price(q) for s, q in watchlist_quotes.items()})

    try:
        context = build_trader_context(
            as_of, cycle, intraday, ledger, tradable_watchlist, positions_notional, prices
        )
        response = invoke_role(
            "trader",
            as_of=as_of,
            cycle=cycle,
            config=intraday,
            ledger=ledger,
            client=llm_client,
            context=context,
        )
    except DailyBudgetExhausted as exc:
        result.skipped_reason = str(exc)
        return result

    turnover_so_far_usd = _turnover_so_far_usd(ledger, as_of, account.equity)
    last_stopout_at = ledger.last_stopout_times()

    raw_proposals = (
        (response.structured_output or {}).get("proposals", [])
        if isinstance(response.structured_output, dict)
        else []
    )

    for item in raw_proposals:
        result.proposals_seen += 1
        try:
            proposal = TradeProposal(**item)
        except (ValidationError, TypeError) as exc:
            ledger.record_incident("error", f"malformed trader proposal: {exc}")
            continue

        validation_context = ValidationContext(
            as_of=as_of,
            now=now,
            config=intraday,
            ledger=ledger,
            equity=account.equity,
            equity_at_session_open=equity_at_session_open,
            positions=positions_notional,
            prices=prices,
            turnover_so_far_usd=turnover_so_far_usd,
            last_stopout_at=last_stopout_at,
        )
        failures = validate_proposal(proposal, validation_context)
        status = "rejected" if failures else "accepted"
        reason = "; ".join(failures) if failures else None
        proposal_id = ledger.record_proposal(
            as_of,
            cycle,
            proposal.symbol,
            proposal.action,
            status,
            payload=proposal.model_dump(),
            reason=reason,
        )
        if failures:
            result.proposals_rejected += 1
            continue
        result.proposals_accepted += 1

        if proposal.action in OPEN_OR_ADD_ACTIONS:
            price = prices.get(proposal.symbol)
            if price is None:
                result.order_failures.append(f"{proposal.symbol}: no price to size entry")
                continue
            tranches = max(1, intraday.risk.entry_tranches)
            tranche_proposal = proposal.model_copy(
                update={"size_pct": proposal.size_pct / tranches}
            )
            try:
                entry_request = build_entry_order(
                    tranche_proposal, as_of, cycle, price, account.equity, intraday, tranche=1
                )
            except ValueError as exc:
                result.order_failures.append(str(exc))
                continue
            submitted = submit_order_with_fault_isolation(
                entry_request,
                broker,
                ledger,
                now,
                proposal_id=proposal_id,
                tranche_index=1,
                tranche_total=tranches,
                leg="entry",
            )
            if submitted is not None:
                result.orders_submitted.append(submitted.client_order_id)
            else:
                result.order_failures.append(f"{proposal.symbol}: entry order failed")

        elif proposal.action == "trim":
            position = next((p for p in positions if p.symbol == proposal.symbol), None)
            price = prices.get(proposal.symbol)
            if position is None or price is None:
                result.order_failures.append(
                    f"{proposal.symbol}: cannot trim -- no position or price"
                )
                continue
            find_and_cancel_resting_stop(proposal.symbol, broker, ledger)
            try:
                trim_request = build_trim_order(
                    proposal, position, as_of, cycle, price, account.equity, intraday
                )
            except ValueError as exc:
                result.order_failures.append(str(exc))
                continue
            submitted = submit_order_with_fault_isolation(
                trim_request, broker, ledger, now, proposal_id=proposal_id, leg="exit"
            )
            if submitted is None:
                result.order_failures.append(f"{proposal.symbol}: trim order failed")
                continue
            result.orders_submitted.append(submitted.client_order_id)

            remaining_qty = abs(position.qty) - abs(trim_request.qty)
            original_payload = _original_proposal_payload(ledger, proposal.symbol) or {}
            original_invalidation = original_payload.get("invalidation_price")
            if remaining_qty > 0 and original_invalidation is not None:
                stop_side: OrderSide = "sell" if position.side == "long" else "buy"
                replacement = build_replacement_stop_order(
                    proposal.symbol, stop_side, remaining_qty, original_invalidation, as_of, cycle
                )
                submit_order_with_fault_isolation(
                    replacement, broker, ledger, now, proposal_id=proposal_id, leg="stop"
                )
            elif remaining_qty > 0:
                ledger.record_incident(
                    "warning",
                    f"{proposal.symbol}: trimmed but no original invalidation_price found "
                    "-- remainder left without a resting stop",
                )

        elif proposal.action == "close":
            position = next((p for p in positions if p.symbol == proposal.symbol), None)
            price = prices.get(proposal.symbol)
            if position is None or price is None:
                result.order_failures.append(
                    f"{proposal.symbol}: cannot close -- no position or price"
                )
                continue
            find_and_cancel_resting_stop(proposal.symbol, broker, ledger)
            close_request = build_close_order(position, as_of, cycle, price, intraday)
            submitted = submit_order_with_fault_isolation(
                close_request, broker, ledger, now, proposal_id=proposal_id, leg="exit"
            )
            if submitted is not None:
                result.orders_submitted.append(submitted.client_order_id)
            else:
                result.order_failures.append(f"{proposal.symbol}: close order failed")

        # hold/no_trade: nothing to do.

    return result


# -- carry ------------------------------------------------------------------------------


@dataclass
class CarryResult:
    closed: list[str] = field(default_factory=list)
    carried: list[str] = field(default_factory=list)
    closed_for_cap: list[str] = field(default_factory=list)


def run_carry(
    config: Config,
    broker: Broker,
    ledger: IntradayLedger,
    market_data: AlpacaMarketDataClient,
    as_of: pd.Timestamp,
    now: dt.datetime,
) -> CarryResult:
    """At 14:45: closes every position whose opening proposal's `horizon` is
    `"intraday"` or untraceable (a safe default -- never carry something with no
    written multi-day thesis on file, per the brief's own "everything carried needs a
    written multi-day thesis"). Remaining (multi-day/multi-week) positions are carried
    unless their aggregate notional exceeds `risk.overnight_gross_cap_pct * equity`, in
    which case the lowest-confidence ones are closed one at a time (a full close, not a
    partial trim -- simpler, and this is already an off-thesis risk-cap action, not a
    sizing decision) until back under cap.
    """
    result = CarryResult()
    intraday = config.intraday
    positions = broker.list_positions()
    account = broker.get_account()

    carried_candidates: list[tuple[Position, dict[str, Any]]] = []
    for position in positions:
        payload = _original_proposal_payload(ledger, position.symbol)
        horizon = payload.get("horizon") if payload else None
        if horizon in ("multi-day", "multi-week"):
            carried_candidates.append((position, payload or {}))
            continue

        price = midpoint_price(market_data.latest_quote(position.symbol, at=now))
        if price is None:
            ledger.record_incident(
                "error", f"{position.symbol}: carry could not price a close -- left open"
            )
            result.carried.append(position.symbol)
            continue
        find_and_cancel_resting_stop(position.symbol, broker, ledger)
        close_request = build_close_order(position, as_of, "carry", price, intraday)
        submit_order_with_fault_isolation(close_request, broker, ledger, now, leg="exit")
        result.closed.append(position.symbol)

    carried_candidates.sort(key=lambda item: item[1].get("confidence") or 0.0)
    overnight_notional = sum(abs(p.market_value) for p, _ in carried_candidates)
    cap = intraday.risk.overnight_gross_cap_pct * account.equity

    for position, _payload in carried_candidates:
        if overnight_notional <= cap:
            result.carried.append(position.symbol)
            continue
        price = midpoint_price(market_data.latest_quote(position.symbol, at=now))
        if price is None:
            result.carried.append(position.symbol)
            continue
        find_and_cancel_resting_stop(position.symbol, broker, ledger)
        close_request = build_close_order(position, as_of, "carry", price, intraday)
        submit_order_with_fault_isolation(close_request, broker, ledger, now, leg="exit")
        overnight_notional -= abs(position.market_value)
        result.closed_for_cap.append(position.symbol)

    return result

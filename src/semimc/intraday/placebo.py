"""S4's placebo agent -- T6's control group (`THESES.md`, `BRIEF_INTRADAY.md`'s "Placebo
agent"), Phase 7.

*"Identical sizing, execution, and risk controls as the real S4 loop, but entries drawn at
random from the morning watchlist instead of the Trader's judgment. Runs on its own shadow
ledger every day S4 runs, never submitting real orders."* Every risk/budget/execution
primitive here is reused unmodified from Phases 3-5 (`proposals.py::validate_proposal`,
`orders.py::build_entry_order`/`submit_order_with_fault_isolation`,
`monitor.py::check_invalidation_and_time_stops`, `jobs.py::run_carry`) against a second,
placebo-only `IntradayLedger` and a `FakeBroker` reconstructed from that ledger's own
persisted state -- this is what makes the risk controls *identical*, not a parallel
reimplementation of them. The only genuinely new logic this phase adds is
`_mark_to_market` -- since promoted to `jobs.py::mark_broker_to_market` in Phase 11, once
Stage 1's own shadow mode for the *real* loop needed the identical computation (same
"second caller needs the identical computation" reasoning `turnover_so_far_usd` was
already promoted for): nothing else keeps a `FakeBroker`'s `.cash`/`.equity` correct the
way a real broker automatically would.

No LLM call, no news, no `AlpacaPaperBroker` -- confirmed in code, not just by convention,
since this module never imports either.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass

import pandas as pd

from ..config import Config
from ..execution.broker import OrderSide
from ..execution.fake_broker import FakeBroker
from ..execution.market_data import AlpacaMarketDataClient
from ..execution.pretrade import KillSwitchActive, check_kill_switch
from .jobs import (
    CarryResult,
    closed_market_note,
    current_prices,
    early_close_note_prefix,
    mark_broker_to_market,
    run_carry,
    turnover_so_far_usd,
)
from .ledger import IntradayLedger
from .monitor import check_invalidation_and_time_stops
from .orders import build_entry_order, submit_order_with_fault_isolation
from .proposals import TradeProposal, ValidationContext, stop_sized_pct, validate_proposal

_THESIS = "placebo: random draw from watchlist -- see THESES.md's T6"


def placebo_day_pnl(
    shadow_ledger: IntradayLedger,
    market_data: AlpacaMarketDataClient,
    config: Config,
    as_of: pd.Timestamp,
    now: dt.datetime,
) -> float | None:
    """The placebo's day P&L -- one of `eod_report.py`'s "benchmarks for the day" (T6's
    control group, Phase 7), added in Phase 9b: today's mark-to-market equity minus its
    own recorded session-open equity, reusing `mark_broker_to_market` (the one place
    shadow equity is ever computed) against a `FakeBroker` reconstructed from its
    persisted state -- the same reconstruction every placebo job already does, not a
    second copy. Lives here rather than in `eod_report.py`, which deliberately never
    reaches into a client's or another module's own internals directly (see that
    module's docstring); this is exactly that reach, so it belongs where
    `mark_broker_to_market`/`FakeBroker` already do. `None` if the placebo never ran
    today (no session-open equity row yet) or never ran at all (no persisted state)."""
    state = shadow_ledger.load_shadow_broker_state()
    if state is None:
        return None
    opening_equity = shadow_ledger.session_open_equity(as_of)
    if opening_equity is None:
        return None
    shadow_broker = FakeBroker.from_state(state, now=now)
    mark_broker_to_market(
        shadow_broker, shadow_ledger, market_data, config.intraday.placebo.starting_equity, now
    )
    return shadow_broker.equity - opening_equity


@dataclass(frozen=True)
class PlaceboCycleResult:
    proposed_symbol: str | None = None
    order_submitted: str | None = None
    skipped_reason: str | None = None


def run_placebo_cycle(
    cycle: str,
    config: Config,
    shadow_broker: FakeBroker,
    shadow_ledger: IntradayLedger,
    real_ledger: IntradayLedger,
    market_data: AlpacaMarketDataClient,
    as_of: pd.Timestamp,
    now: dt.datetime,
    rng: random.Random,
) -> PlaceboCycleResult:
    """One placebo cycle: the same kill switch the real loop shares (an emergency halt
    should halt the control group too, so the comparison stays clean) -> deterministic
    invalidation/time-stop enforcement (reused, folded in here at the same cadence as
    `cycle` -- fake money doesn't need `monitor`'s separate 5-minute cron, the same
    reasoning Phase 5 used for why real money does) -> mark-to-market -> one random
    watchlist symbol not already held -> a mechanical proposal, sized by the same
    `risk_per_trade_pct` every other proposal is capped by -> `validate_proposal`
    (identical risk controls) -> submit on acceptance.

    A missing watchlist, no eligible (not-already-held) candidate, and a rejected
    proposal are all routine outcomes, not failures -- exactly as routine as `hold`/
    `no_trade` are for the real Trader.
    """
    intraday = config.intraday
    try:
        check_kill_switch(config.path(intraday.kill_switch_path))
    except KillSwitchActive as exc:
        return PlaceboCycleResult(skipped_reason=str(exc))

    # Follows the real cycle that ran just before it under the same label (added
    # 2026-10-20). The shadow broker always reads the market as open, so after an early
    # close (13:00 ET on 2026-11-27 and 2026-12-24) the placebo kept trading on
    # after-hours quotes, and since `carry` waits for the last hour of a 16:00 close on
    # its fake calendar, its book stayed open past the bell. This path never touches
    # the real account; the real ledger, already read for the watchlist, says what the
    # real cycle found. (A market holiday needs nothing: no research, no watchlist.)
    real_notes = [row["message"] for row in real_ledger.incidents_for(as_of)]
    if closed_market_note(cycle) in real_notes:
        return PlaceboCycleResult(skipped_reason="market is closed (the real cycle declined)")
    if any(note.startswith(early_close_note_prefix(cycle)) for note in real_notes):
        carry = run_placebo_carry(config, shadow_broker, shadow_ledger, market_data, as_of, now)
        return PlaceboCycleResult(
            skipped_reason=(
                "early close: ran carry, as the real cycle did (closed: "
                f"{', '.join(carry.closed + carry.closed_for_cap) or 'none'})"
            )
        )

    # Mirrors `jobs.py::run_cycle`'s once-a-day open-batch guard, so the control group
    # never gets a second opening draw the real agent is denied. The placebo makes no
    # model call; an open-batch proposal on its own ledger is its record of having run.
    if cycle == "open_batch" and any(
        row["cycle"] == "open_batch" for row in shadow_ledger.proposals_for(as_of)
    ):
        return PlaceboCycleResult(
            skipped_reason=f"open_batch already ran for {pd.Timestamp(as_of).date()}"
        )

    held_symbols = {p.symbol for p in shadow_broker.list_positions()}
    prices = current_prices(held_symbols, market_data, now)
    check_invalidation_and_time_stops(
        intraday, shadow_broker, shadow_ledger, prices, as_of, cycle, now
    )

    starting_equity = intraday.placebo.starting_equity
    mark_broker_to_market(shadow_broker, shadow_ledger, market_data, starting_equity, now)
    shadow_ledger.record_session_open_equity(as_of, shadow_broker.equity)
    equity_at_session_open = shadow_ledger.session_open_equity(as_of) or shadow_broker.equity

    held_symbols = {p.symbol for p in shadow_broker.list_positions()}
    candidates = [
        row["symbol"]
        for row in real_ledger.watchlist_for(as_of)
        if row["symbol"] not in held_symbols
    ]
    if not candidates:
        return PlaceboCycleResult(skipped_reason="no eligible watchlist candidates")

    symbol = rng.choice(candidates)
    price = current_prices({symbol}, market_data, now).get(symbol)
    if price is None:
        return PlaceboCycleResult(
            proposed_symbol=symbol, skipped_reason=f"{symbol}: no quote available"
        )

    side: OrderSide = rng.choice(["buy", "sell"])
    stop_distance = intraday.placebo.stop_distance_pct
    invalidation_price = (
        price * (1 - stop_distance) if side == "buy" else price * (1 + stop_distance)
    )
    # Sized by the same rule as the real agent's entries (2026-10-22), so the two books
    # risk the same at each stop and their P&L compares like for like. A candidate is
    # never already held, so the whole single-name cap is room.
    size_pct = stop_sized_pct(price, invalidation_price, intraday.risk)

    proposal = TradeProposal(
        action="open",
        symbol=symbol,
        side=side,
        size_pct=size_pct,
        limit_price=price,
        thesis=_THESIS,
        invalidation_price=invalidation_price,
        time_stop_minutes=intraday.placebo.time_stop_minutes,
    )

    account = shadow_broker.get_account()
    positions_notional = {p.symbol: p.market_value for p in shadow_broker.list_positions()}
    validation_context = ValidationContext(
        as_of=as_of,
        now=now,
        config=intraday,
        ledger=shadow_ledger,
        equity=account.equity,
        equity_at_session_open=equity_at_session_open,
        positions=positions_notional,
        prices={**prices, symbol: price},
        turnover_so_far_usd=turnover_so_far_usd(shadow_ledger, as_of, account.equity),
        last_stopout_at=shadow_ledger.last_stopout_times(),
    )
    failures = validate_proposal(proposal, validation_context)
    status = "rejected" if failures else "accepted"
    proposal_id = shadow_ledger.record_proposal(
        as_of,
        cycle,
        symbol,
        "open",
        status,
        payload=proposal.model_dump(),
        reason="; ".join(failures) if failures else None,
    )
    if failures:
        return PlaceboCycleResult(proposed_symbol=symbol, skipped_reason="; ".join(failures))

    try:
        entry_request = build_entry_order(proposal, as_of, cycle, price, account.equity, intraday)
    except ValueError as exc:
        return PlaceboCycleResult(proposed_symbol=symbol, skipped_reason=str(exc))

    submitted = submit_order_with_fault_isolation(
        entry_request, shadow_broker, shadow_ledger, now, proposal_id=proposal_id, leg="entry"
    )
    if submitted is None:
        return PlaceboCycleResult(
            proposed_symbol=symbol, skipped_reason=f"{symbol}: submission failed"
        )
    return PlaceboCycleResult(proposed_symbol=symbol, order_submitted=submitted.client_order_id)


def run_placebo_carry(
    config: Config,
    shadow_broker: FakeBroker,
    shadow_ledger: IntradayLedger,
    market_data: AlpacaMarketDataClient,
    as_of: pd.Timestamp,
    now: dt.datetime,
    *,
    near_close_only: bool = False,
) -> CarryResult:
    """Mark-to-market, then `jobs.py::run_carry` unmodified against the shadow
    broker/ledger. Every placebo proposal leaves `horizon` unset, so `run_carry`'s own
    "no written multi-day thesis -> safe default, close it" already makes every placebo
    position intraday-only -- no placebo-specific carry rule needed. `near_close_only` is
    passed straight through (`cli.py` sets it, as for the real carry).
    """
    mark_broker_to_market(
        shadow_broker, shadow_ledger, market_data, config.intraday.placebo.starting_equity, now
    )
    return run_carry(
        config,
        shadow_broker,
        shadow_ledger,
        market_data,
        as_of,
        now,
        near_close_only=near_close_only,
    )

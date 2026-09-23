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
    mark_broker_to_market,
    midpoint_price,
    quote_map,
    run_carry,
    turnover_so_far_usd,
)
from .ledger import IntradayLedger
from .monitor import check_invalidation_and_time_stops
from .orders import build_entry_order, submit_order_with_fault_isolation
from .proposals import TradeProposal, ValidationContext, validate_proposal

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

    held_symbols = {p.symbol for p in shadow_broker.list_positions()}
    held_quotes = quote_map(held_symbols, market_data, now)
    prices = {symbol: midpoint_price(quote) for symbol, quote in held_quotes.items()}
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
    quote = market_data.latest_quote(symbol, at=now)
    price = midpoint_price(quote)
    if price is None:
        return PlaceboCycleResult(
            proposed_symbol=symbol, skipped_reason=f"{symbol}: no quote available"
        )

    side: OrderSide = rng.choice(["buy", "sell"])
    stop_distance = intraday.placebo.stop_distance_pct
    invalidation_price = (
        price * (1 - stop_distance) if side == "buy" else price * (1 + stop_distance)
    )
    # Maximally sized subject to every cap already known up front (risk-to-invalidation
    # and single-name), rather than relying on validate_proposal to reject an
    # over-concentrated size this code could have avoided proposing in the first place --
    # "no judgment" means no restraint beyond the hard caps, not "guaranteed rejected."
    size_pct = min(
        intraday.risk.risk_per_trade_pct / stop_distance,
        intraday.risk.single_name_cap_pct,
        1.0,
    )

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
) -> CarryResult:
    """Mark-to-market, then `jobs.py::run_carry` unmodified against the shadow
    broker/ledger. Every placebo proposal leaves `horizon` unset, so `run_carry`'s own
    "no written multi-day thesis -> safe default, close it" already makes every placebo
    position intraday-only -- no placebo-specific carry rule needed.
    """
    mark_broker_to_market(
        shadow_broker, shadow_ledger, market_data, config.intraday.placebo.starting_equity, now
    )
    return run_carry(config, shadow_broker, shadow_ledger, market_data, as_of, now)

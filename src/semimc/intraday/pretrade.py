"""S4's own sleeve-level pretrade gate (S4, `BRIEF_INTRADAY.md`, Phase 3).

Mirrors `execution/pretrade.py::PretradeContext`/`check_pretrade`'s exact shape, but reads
`config.intraday` instead of `config.paper` -- not generalized into one shared function,
for the same reason `IntradayConfig` is already a top-level sibling rather than nested
inside `PaperConfig`: `PaperConfig` is deliberately scoped to what s0/s3 need (its own
docstring says so), not a generalized schema. `config.intraday.registered_account` is a
single string, not a list, since S4 has exactly one account -- never
`paper.registered_accounts`, which guards a different account entirely.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..config import IntradayConfig
from ..execution.broker import Broker
from ..execution.pretrade import KillSwitchActive, check_kill_switch


@dataclass(frozen=True)
class IntradayPretradeContext:
    """Everything :func:`check_intraday_pretrade` needs, gathered once by the caller so
    every check runs against the same account/clock read rather than each check
    re-querying the broker."""

    now: dt.datetime
    config: IntradayConfig
    broker: Broker


def check_intraday_pretrade(
    context: IntradayPretradeContext, *, shadow: bool = False, check_market_open: bool = True
) -> list[str]:
    """Every gate that must clear before submitting *any* S4 order today.

    Returns every failure found, not just the first, same as `execution/pretrade.py::
    check_pretrade`. No configured start/cutoff execution window the way paper's has --
    S4's time-of-day gating is the job schedule itself (Phase 5), not a second,
    overlapping window check here.

    ``shadow`` (Phase 11, Rollout Stage 1) skips the account-identity/account-status
    checks -- `context.broker` is a `FakeBroker` with placeholder identity fields in
    shadow mode, and matching those against `registered_account` would be theater, not a
    real check (`intraday/placebo.py::run_placebo_cycle` already makes the identical
    choice, checking only the kill switch). Kill switch and `trading_enabled` are still
    checked either way -- shadow mode is still something the owner must deliberately
    turn on.

    ``check_market_open`` (Phase 11) defaults to `True` (today's exact behavior) but
    `run_research` passes `False`: research runs at 07:30 Central, before the market
    opens, so this check would otherwise *always* fail there, live or shadow -- exactly
    the kind of second, overlapping time check this function's own docstring already
    says it isn't meant to impose (timing is the job schedule's job).
    """
    failures: list[str] = []

    try:
        check_kill_switch(context.config.path(context.config.intraday.kill_switch_path))
    except KillSwitchActive as exc:
        failures.append(str(exc))

    if not context.config.intraday.trading_enabled:
        failures.append("intraday trading is not trading_enabled")

    account = context.broker.get_account()
    if not shadow:
        if account.account_number != context.config.intraday.registered_account:
            # Never interpolate the account number itself into this message -- it is
            # recorded verbatim as a ledger incident that this project deliberately
            # git-tracks, the same rule execution/pretrade.py already follows and for
            # the same reason (found the hard way once already, 2026-09-19: see that
            # module's docstring).
            failures.append(
                "account does not match intraday.registered_account -- refusing to trade "
                "against an unregistered account"
            )
        if account.status != "ACTIVE":
            failures.append(f"account status is {account.status}")

    if check_market_open and not context.broker.get_clock().is_open:
        failures.append("market is closed")

    return failures


def check_spread_gate(
    symbol: str,
    bid: float,
    ask: float,
    quote_time: dt.datetime,
    now: dt.datetime,
    config: IntradayConfig,
    check_age: bool = True,
) -> str | None:
    """`None` if `symbol`'s quote is tradable, else a rejection reason string (S4, Phase
    5) -- the brief's own "skip or defer any name whose quoted spread exceeds the
    configured limit, or whose quote is stale."

    `check_age=False` checks the spread alone, for a quote that is old on purpose: the
    15-minute-delayed consolidated quote `jobs.py::_apply_spread_gate` reads by default
    (`risk.spread_feed`), whose freshness question is answered by the price instead
    (`check_price_age`).

    A per-candidate check, not a per-sleeve one like `check_intraday_pretrade` above, so
    it's a separate function rather than folded into that one. Called by `preopen` (to
    log what's already untradable ahead of the bell) and by `run_cycle` (to actually drop
    a candidate) -- never trusted from one job to the next, same "re-check fresh state"
    discipline `live/mark.py` already uses.

    `quote_time`/`now` may each be naive or aware -- `AlpacaMarketDataClient`'s own
    `Quote.timestamp` always comes back aware UTC (confirmed against a real account,
    `execution/market_data.py`'s docstring), so a naive value on either side is assumed
    UTC rather than compared directly and raising, the same convention
    `market_data.py::arrival_price` already uses.
    """
    if bid <= 0 or ask <= 0 or ask < bid:
        return f"{symbol}: invalid quote (bid={bid}, ask={ask})"

    mid = (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 1e4
    if spread_bps > config.risk.max_spread_bps:
        return (
            f"{symbol}: spread {spread_bps:.1f}bps exceeds max_spread_bps "
            f"{config.risk.max_spread_bps}"
        )

    if check_age:
        return check_price_age(symbol, quote_time, now, config, what="quote")
    return None


def check_price_age(
    symbol: str,
    observed_at: dt.datetime,
    now: dt.datetime,
    config: IntradayConfig,
    what: str = "price",
) -> str | None:
    """`None` if a price or quote observed at `observed_at` is recent enough to trade on
    (`risk.max_quote_age_seconds`), else a rejection reason. Naive datetimes are UTC."""
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=dt.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    age_seconds = (now - observed_at).total_seconds()
    if age_seconds > config.risk.max_quote_age_seconds:
        return (
            f"{symbol}: {what} is {age_seconds:.0f}s old, exceeds max_quote_age_seconds "
            f"{config.risk.max_quote_age_seconds}"
        )

    return None

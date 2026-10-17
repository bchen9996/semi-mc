"""Deterministic invalidation/time-stop enforcement (S4, `BRIEF_INTRADAY.md`, Phase 5).

The brief calls for "a monitor process [that] checks every position every minute and
exits on its own when the level or time stop is hit" -- a persistent process this
project's GitHub-Actions-cron architecture doesn't have (s0/s3 don't run one either).
The pragmatic fit: :func:`check_invalidation_and_time_stops` is a cheap, model-free check
that can run on its own tight cron (every few minutes, no LLM cost) *and* is the first
thing `jobs.py::run_cycle` calls, so the Trader is never asked about a position that
should already be gone. Code decides this, never the model, per the brief's "the model
is asked what to do only when there's a real decision."

Every call also records its own cadence (`ledger.record_monitor_check`, found 2026-09-23):
GitHub's `schedule` event delivery for this repo turned out far less reliable than the
configured cron intervals imply, and in shadow mode there is no real resting stop order
enforcing anything between calls (`FakeBroker` never auto-triggers one) -- so how often
this function actually runs *is* the shadow book's real protection latency, not just an
operational curiosity. This doesn't fix the delay; it stops Stage 1's own reports from
silently assuming a cadence that didn't happen.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from functools import partial

import pandas as pd

from ..config import IntradayConfig
from ..execution.broker import Broker
from .ledger import IntradayLedger
from .orders import build_close_order, market_is_open, submit_exit
from .reflect import record_mechanical_reflection


@dataclass(frozen=True)
class MonitorResult:
    closed: list[str] = field(default_factory=list)
    still_open: list[str] = field(default_factory=list)
    # Breached its invalidation or time stop, but no exit went out (`submit_exit`'s
    # failure; also listed in `still_open`). `run_cycle` adds no tranche to these.
    exits_failed: list[str] = field(default_factory=list)
    # Seconds since this same trading day's previous recorded check, `None` for the day's
    # first (`ledger.record_monitor_check`) -- surfaced here so the CLI can print it
    # immediately, not just eod-review/weekly-review after the fact.
    gap_seconds: float | None = None


def _first_fill_of_proposal(ledger: IntradayLedger, entry: dict) -> dt.datetime | None:
    """The earliest fill of any entry order (every tranche) of `entry`'s proposal, aware
    UTC; `None` if none has filled. A naive time is read as UTC -- the same fallback as
    `intraday/pretrade.py::check_spread_gate`: a real Order's fill time comes back aware
    UTC (`execution/market_data.py`), but a naive one must not raise."""
    times = []
    for order in ledger.orders_for_proposal(entry["proposal_id"], leg="entry"):
        for fill in ledger.fills_for_order(order["client_order_id"]):
            filled_at = dt.datetime.fromisoformat(fill["filled_at"])
            times.append(filled_at if filled_at.tzinfo else filled_at.replace(tzinfo=dt.UTC))
    return min(times, default=None)


def check_invalidation_and_time_stops(
    config: IntradayConfig,
    broker: Broker,
    ledger: IntradayLedger,
    prices: dict[str, float],
    as_of: pd.Timestamp,
    cycle: str,
    now: dt.datetime,
    *,
    market_exit_without_price: bool = False,
) -> MonitorResult:
    """For every held `Position`, recovers the proposal that opened it (the most recent
    filled entry order for that symbol, via `ledger.latest_filled_entry_for`, then that
    order's own `proposal_id` via `ledger.get_proposal`) and closes it
    (`orders.py::submit_exit`, which cancels the stops, waits for the cancels, and
    re-places a stop if the close doesn't go out) if the current price has crossed the recorded
    `invalidation_price`, or `time_stop_minutes` has elapsed since that proposal's first
    fill, across all its tranches (:func:`_first_fill_of_proposal`).

    **A later tranche doesn't restart the clock** (owner decision 2026-10-16, option A
    of three). It used to count from the latest entry order's fill, and an entry goes in
    as tranches a cycle apart, so a 90-minute time stop ran about 120: on 2026-10-15
    NVDA's first tranche filled at 10:01 CT and its second at 10:30, and the time stop
    came due at 12:01 rather than 11:31. Tranching is an execution technique, not a new
    decision. An `add` is one, with its own time stop, so it still restarts the clock.

    A position with no traceable entry/proposal is left alone and logged as an incident
    rather than guessed at -- the same "never act on incomplete information" caution
    `execution/pretrade.py` uses elsewhere.

    With no current price, only a time stop can come due (an invalidation needs a price
    to cross, and the stop resting at the broker covers that level meanwhile). By
    default that position is left open with an error incident. With
    `market_exit_without_price`, which only the real account's callers set, it is closed
    with a market order instead, provided the broker's clock says the market is open
    (`orders.py::build_close_order`). Added 2026-10-15, after that day's outage, the
    first of Stage 2: every quote went stale and then missing from about 11:55 to 13:00
    CT, and both positions stayed open 30 to 60 minutes past their time stops. A shadow
    or placebo `FakeBroker` has no market to fill a market order against, so it keeps
    the default.

    A confirmed close also records a mechanical reflection (`reflect.py`, Phase 9a) tagged
    `"invalidation"` or `"time_stop"` -- the qualitative fields (category, process grade)
    stay `None` until Phase 9b's `eod_review` batches them in.

    An invalidation-triggered close (never a time-stop close, which isn't necessarily a
    loss) also records an incident message prefixed ``"stopout:{symbol}"`` -- the brief's
    own cooldown rule ("after a stop-out in a name, that name is untradable for a set
    period... to break revenge-trading patterns") is about hitting the invalidation
    level specifically, and `IntradayLedger.last_stopout_times` (Phase 5) reads this
    prefix back out to feed `ValidationContext.last_stopout_at`.
    """
    gap_seconds = ledger.record_monitor_check(as_of, cycle, now)

    closed: list[str] = []
    still_open: list[str] = []
    exits_failed: list[str] = []

    for position in broker.list_positions():
        symbol = position.symbol
        entry = ledger.latest_filled_entry_for(symbol)
        proposal = None
        if entry is not None and entry["proposal_id"] is not None:
            proposal = ledger.get_proposal(entry["proposal_id"])
        if entry is None or proposal is None:
            still_open.append(symbol)
            continue

        payload = proposal["payload"]
        invalidation_price = payload.get("invalidation_price")
        time_stop_minutes = payload.get("time_stop_minutes")
        price = prices.get(symbol)

        stopped_out = False
        timed_out = False
        if invalidation_price is not None and price is not None:
            long_stopped_out = position.side == "long" and price <= invalidation_price
            short_stopped_out = position.side == "short" and price >= invalidation_price
            stopped_out = long_stopped_out or short_stopped_out

        if not stopped_out and time_stop_minutes is not None:
            filled_at = _first_fill_of_proposal(ledger, entry)
            if filled_at is not None:
                aware_now = now if now.tzinfo is not None else now.replace(tzinfo=dt.UTC)
                elapsed_minutes = (aware_now - filled_at).total_seconds() / 60.0
                if elapsed_minutes >= time_stop_minutes:
                    timed_out = True

        if not stopped_out and not timed_out:
            still_open.append(symbol)
            continue

        if price is None:
            if not (market_exit_without_price and market_is_open(broker)):
                ledger.record_incident(
                    "error",
                    f"{symbol}: invalidation/time-stop breached but no current price to "
                    "close at -- left open, needs attention",
                )
                still_open.append(symbol)
                continue
            ledger.record_incident(
                "warning",
                f"{symbol}: time stop due with no current price -- closing with a market order",
            )

        attempt = submit_exit(
            symbol,
            partial(build_close_order, as_of=as_of, cycle=cycle, price=price, config=config),
            broker,
            ledger,
            now,
            reason="invalidation" if stopped_out else "time_stop",
            as_of=as_of,
            cycle=cycle,
            stop_price=invalidation_price,
            proposal_id=entry["proposal_id"],
        )
        if stopped_out:
            ledger.record_incident("warning", f"stopout:{symbol}")
        if attempt.order is not None:
            record_mechanical_reflection(
                symbol,
                attempt.order.client_order_id,
                "invalidation" if stopped_out else "time_stop",
                ledger,
                config,
                as_of,
            )
        if attempt.failure is not None:
            exits_failed.append(symbol)
            still_open.append(symbol)
        else:
            closed.append(symbol)

    return MonitorResult(
        closed=closed, still_open=still_open, exits_failed=exits_failed, gap_seconds=gap_seconds
    )

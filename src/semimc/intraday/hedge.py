"""What a sector hedge would have done for each closed trade (added 2026-10-31).

Measurement only: nothing here places an order. Most of S4's P&L is the sector's direction,
so a good week says little about whether its picks are any good. Each reflection gets a
`hedge` block: what a beta-sized position in `intraday.hedge.instrument`, opposite the
trade, would have made from each entry fill to the exit, and what it would have cost at the
modeled rate. What's left is the trade with the sector taken out, comparable between the
agent and the placebo in basis points of notional, whatever their sizes.

Betas come from `MarketData.training_window`, ending the day before the entry, so a trade
is never hedged with a beta that knows how the trade turned out. Hedge prices come from
one-minute bars. The block is stored rather than worked out when a report runs, so the
reports stay offline and reproducible, and :func:`record_trade_hedges` prices whatever
lacks one on every run, so a day that fails is picked up by the next.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..config import IntradayConfig
from ..data.returns import estimate_beta
from .ledger import IntradayLedger
from .reflect import _aware, _summed_fills, position_entry_fills

# A bar older than this says the feed had a gap, so the trade waits for a later run rather
# than being priced off a stale print. The real session's gaps on this feed are seconds.
_MAX_BAR_AGE = dt.timedelta(minutes=15)


def reflections_since(
    rows: Sequence[dict[str, Any]], since: dt.date | None
) -> list[dict[str, Any]]:
    return [r for r in rows if since is None or pd.Timestamp(r["as_of"]).date() >= since]


def _close_ids(row: dict[str, Any]) -> list[str]:
    """The orders a reflection's exit was made of. Before 2026-10-16 it named only the one,
    in its own column: leaving those out would drop six of the placebo's first 36 exits."""
    ids = row["content"].get("client_order_ids")
    if ids:
        return list(ids)
    return [row["client_order_id"]] if row.get("client_order_id") else []


def trailing_beta(
    market: Any, symbol: str, day: pd.Timestamp, config: IntradayConfig
) -> float | None:
    """`symbol`'s beta to the hedge instrument from daily returns up to the day before
    `day`. `None` without enough history, e.g. a symbol outside the price cache."""
    hedge = config.hedge
    try:
        window = market.training_window(
            [symbol, hedge.instrument],
            pd.Timestamp(day) - pd.Timedelta(days=1),
            window_days=hedge.beta_window_days,
            min_observations=hedge.beta_min_observations,
        )
    except (KeyError, ValueError):
        return None
    beta = estimate_beta(window[symbol], window[hedge.instrument])
    return beta if math.isfinite(beta) else None


def _price_at(prices: pd.Series, moment: dt.datetime) -> float | None:
    """The open of the bar `moment` falls in. `prices` is sorted by aware bar start."""
    position = int(prices.index.searchsorted(pd.Timestamp(moment), side="right")) - 1
    if position < 0 or pd.Timestamp(moment) - prices.index[position] > _MAX_BAR_AGE:
        return None
    return float(prices.iloc[position])


def _new_york_day(moment: dt.datetime) -> pd.Timestamp:
    return pd.Timestamp(moment).tz_convert("America/New_York").normalize().tz_localize(None)


def price_trade_hedge(
    ledger: IntradayLedger,
    row: dict[str, Any],
    prices: pd.Series,
    beta_for: Callable[[str, pd.Timestamp], float | None],
    cost_bps_per_side: float,
) -> dict[str, Any] | None:
    """The `hedge` block for one reflection, or `None` if it can't be priced yet. Each entry
    fill is hedged from its own time, so a second tranche isn't hedged for time it wasn't
    held. An exit of part of a position (a trim, or one stop of several) takes its share of
    the entries, the same share `trade_figures` prices it with."""
    content = row["content"]
    close_ids = _close_ids(row)
    entries = position_entry_fills(ledger, row["symbol"], close_ids)
    exit_at = _summed_fills(ledger, close_ids)[3]
    entry_qty = sum(fill["qty"] for fill in entries)
    if entry_qty <= 0 or exit_at is None:
        return None

    first_fill = min(_aware(dt.datetime.fromisoformat(fill["filled_at"])) for fill in entries)
    beta = beta_for(row["symbol"], _new_york_day(first_fill))
    exit_price = _price_at(prices, exit_at)
    if beta is None or exit_price is None:
        return None

    sign = 1.0 if content["side"] == "buy" else -1.0
    share = content["qty"] / entry_qty
    notional = 0.0
    pnl = 0.0
    for fill in entries:
        entry_price = _price_at(prices, _aware(dt.datetime.fromisoformat(fill["filled_at"])))
        if entry_price is None:
            return None
        hedged = beta * share * fill["qty"] * fill["price"]
        notional += hedged
        pnl -= sign * hedged * (exit_price / entry_price - 1.0)
    return {
        "beta": beta,
        "notional": notional,
        "pnl": pnl,
        "cost": 2.0 * cost_bps_per_side / 10_000 * notional,
    }


def record_trade_hedges(
    ledger: IntradayLedger,
    market_data: Any,
    market: Any,
    config: IntradayConfig,
    since: dt.date | None = None,
) -> int:
    """Prices every reflection from `since` on that has no `hedge` block and stores it.
    Returns how many were priced."""
    rows = [
        row
        for row in reflections_since(ledger.all_reflections(), since)
        if "hedge" not in row["content"]
    ]
    moments = []
    for row in rows:
        close_ids = _close_ids(row)
        moments += [
            _aware(dt.datetime.fromisoformat(fill["filled_at"]))
            for fill in position_entry_fills(ledger, row["symbol"], close_ids)
        ]
        exit_at = _summed_fills(ledger, close_ids)[3]
        if exit_at is not None:
            moments.append(exit_at)
    if not moments:
        return 0

    instrument = config.hedge.instrument
    bars = market_data.bars(
        [instrument],
        min(moments) - dt.timedelta(minutes=1),
        max(moments) + dt.timedelta(minutes=1),
        minutes=1,
    ).get(instrument, [])
    if not bars:
        return 0
    prices = pd.Series(
        [float(bar.open) for bar in bars], index=[pd.Timestamp(bar.timestamp) for bar in bars]
    ).sort_index()

    betas: dict[tuple[str, pd.Timestamp], float | None] = {}

    def beta_for(symbol: str, day: pd.Timestamp) -> float | None:
        if (symbol, day) not in betas:
            betas[(symbol, day)] = trailing_beta(market, symbol, day, config)
        return betas[(symbol, day)]

    priced = 0
    for row in rows:
        hedge = price_trade_hedge(
            ledger, row, prices, beta_for, config.risk.equity_cost_bps_per_side
        )
        if hedge is not None:
            ledger.update_reflection(row["id"], {**row["content"], "hedge": hedge})
            priced += 1
    return priced


@dataclass(frozen=True)
class HedgedSummary:
    """A set of reflections with the sector taken out. Dollars for the record, but only
    `mean_bps` compares across the agent and the placebo, whose sizes differ."""

    exits: int = 0
    unpriced: int = 0
    gross: float = 0.0
    net: float = 0.0
    hedge_pnl: float = 0.0
    hedge_cost: float = 0.0
    # Before costs, per exit, in basis points of the exit's notional.
    mean_bps: float | None = None
    stderr_bps: float | None = None

    @property
    def hedged_gross(self) -> float:
        return self.gross + self.hedge_pnl

    @property
    def hedged_net(self) -> float:
        return self.net + self.hedge_pnl - self.hedge_cost


def summarize_hedged(reflections: Sequence[dict[str, Any]]) -> HedgedSummary:
    """Dollar figures cover the priced exits only, so as-traded and hedged compare like for
    like; `unpriced` says how many were left out."""
    priced = [row["content"] for row in reflections if row["content"].get("hedge")]
    bps = [
        10_000 * (c["pnl_gross"] + c["hedge"]["pnl"]) / (c["qty"] * c["avg_entry_price"])
        for c in priced
    ]
    mean = sum(bps) / len(bps) if bps else None
    stderr = None
    if len(bps) > 1:
        variance = sum((value - mean) ** 2 for value in bps) / (len(bps) - 1)
        stderr = math.sqrt(variance / len(bps))
    return HedgedSummary(
        exits=len(priced),
        unpriced=len(reflections) - len(priced),
        gross=sum(c["pnl_gross"] for c in priced),
        net=sum(c["pnl_net"] for c in priced),
        hedge_pnl=sum(c["hedge"]["pnl"] for c in priced),
        hedge_cost=sum(c["hedge"]["cost"] for c in priced),
        mean_bps=mean,
        stderr_bps=stderr,
    )

"""The trade proposal schema and its risk/budget validator (S4, `BRIEF_INTRADAY.md`'s
"Trade proposal schema" and "Risk limits and trading budget" sections).

:class:`TradeProposal` is a pydantic model, not a plain dataclass like every other
in-process type in this project (`Decision`, `OrderRequest`, `Order`, `Position`) --
those are built by trusted internal code, while a proposal is untrusted external input (a
Trader LLM's JSON response) that must be schema-validated before anything downstream
trusts it, the same reason `config.py`'s schema is pydantic rather than a dataclass.

:func:`validate_proposal` mirrors `execution/pretrade.py::check_pretrade`'s shape:
everything it needs is gathered once by the caller into :class:`ValidationContext`, and it
returns every failure found (not just the first) as a plain list of strings.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import IntradayConfig
from .ledger import IntradayLedger

OPEN_OR_ADD_ACTIONS = ("open", "add")


class TradeProposal(BaseModel):
    """One action the Trader proposes for one symbol. `instrument="option"` is accepted
    at the schema level (so a future options phase needs no migration) but is rejected by
    :func:`validate_proposal` while `config.intraday.options.enabled` is `False` -- every
    stage through Phase 7."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["open", "add", "trim", "close", "hold", "no_trade"]
    instrument: Literal["equity", "option"] = "equity"
    symbol: str
    side: Literal["buy", "sell"] | None = None
    # Percent of equity, not shares -- for open/add, the position's own size; for trim,
    # how much of equity's worth to sell off (capped at what's actually held, see
    # intraday/orders.py::build_trim_order). Not used by close (the whole position exits).
    size_pct: float | None = Field(None, gt=0, le=1)
    limit_price: float | None = Field(None, gt=0)  # brief: marketable limit orders only
    thesis: str | None = Field(None, max_length=300)
    evidence: list[str] = Field(default_factory=list)
    invalidation_price: float | None = Field(None, gt=0)
    time_stop_minutes: int | None = Field(None, gt=0)
    expected_move_pct: float | None = None
    expected_edge_bps: float | None = None
    confidence: float | None = Field(None, ge=0, le=1)
    horizon: Literal["intraday", "multi-day", "multi-week"] | None = None
    risk_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _required_fields_per_action(self) -> TradeProposal:
        if self.action in OPEN_OR_ADD_ACTIONS:
            required = (
                "side",
                "size_pct",
                "limit_price",
                "thesis",
                "invalidation_price",
                "time_stop_minutes",
            )
            missing = [field for field in required if getattr(self, field) is None]
            if missing:
                raise ValueError(f"action={self.action!r} requires {missing}")
        elif self.action == "trim" and self.size_pct is None:
            # Same unit as open/add (percent of equity, not shares) -- Phase 4 (S4,
            # BRIEF_INTRADAY.md) resolved this as the one meaning for the field across
            # every action that uses it, rather than a second, silently-different unit
            # for trim alone.
            raise ValueError("action='trim' requires ['size_pct']")
        return self


def trader_output_json_schema() -> dict:
    """The schema passed to `claude -p --json-schema` for the Trader role (`intraday/
    llm.py::invoke_role`). Generated from :class:`TradeProposal` itself, not hand-
    maintained, so the CLI-side constraint and the pydantic schema can never drift apart.

    This only constrains *type* shape (JSON Schema has no way to express
    `TradeProposal`'s `model_validator` -- "open/add requires these six fields"), so it's
    a coarse pre-filter, not a replacement for constructing a real `TradeProposal` (and
    then `validate_proposal`) out of each item afterward.
    """
    return {
        "type": "object",
        "properties": {"proposals": {"type": "array", "items": TradeProposal.model_json_schema()}},
        "required": ["proposals"],
    }


@dataclass(frozen=True)
class ValidationContext:
    """Everything :func:`validate_proposal` needs, gathered once by the caller so every
    check runs against the same account/ledger read rather than each check re-querying.

    None of these fields come from a real broker call yet -- Phase 3 builds order/fill
    handling; until then, callers (today, only tests) supply fixture values directly.
    """

    as_of: pd.Timestamp
    now: dt.datetime
    config: IntradayConfig
    ledger: IntradayLedger
    equity: float
    equity_at_session_open: float
    positions: dict[str, float]  # symbol -> current notional (signed: short is negative)
    prices: dict[str, float]  # symbol -> current price
    turnover_so_far_usd: float
    last_stopout_at: dict[str, dt.datetime]


def _accepted_proposals(context: ValidationContext) -> list[dict]:
    return [
        row for row in context.ledger.proposals_for(context.as_of) if row["status"] == "accepted"
    ]


def validate_proposal(proposal: TradeProposal, context: ValidationContext) -> list[str]:
    """Every risk/budget failure found for this proposal, or `[]` if it's accepted.

    `hold`/`no_trade` are always accepted -- "first-class answers, never penalized" per
    the brief. `close`/`trim` skip every risk-increasing check below since they reduce
    exposure -- "when the budget is spent, the agent can still close, trim, and hold."
    """
    if proposal.action in ("hold", "no_trade"):
        return []

    failures: list[str] = []

    if proposal.instrument == "option" and not context.config.options.enabled:
        failures.append("options are not enabled")

    if proposal.action not in OPEN_OR_ADD_ACTIONS:
        return failures

    risk = context.config.risk

    if proposal.expected_edge_bps is not None:
        round_trip_cost_bps = 2 * risk.equity_cost_bps_per_side
        min_edge_bps = risk.min_edge_multiple * round_trip_cost_bps
        if proposal.expected_edge_bps < min_edge_bps:
            failures.append(
                f"expected_edge_bps {proposal.expected_edge_bps:.1f} is below "
                f"{risk.min_edge_multiple}x the modeled round-trip cost ({min_edge_bps:.1f} bps)"
            )

    accepted = _accepted_proposals(context)
    accepted_opens = [row for row in accepted if row["action"] in OPEN_OR_ADD_ACTIONS]
    if len(accepted_opens) >= risk.new_positions_per_day:
        failures.append(
            f"new_positions_per_day budget ({risk.new_positions_per_day}) already spent today"
        )
    same_symbol_round_trips = sum(
        1
        for row in accepted
        if row["symbol"] == proposal.symbol and row["action"] in OPEN_OR_ADD_ACTIONS
    )
    if same_symbol_round_trips >= risk.round_trips_per_symbol_per_day:
        failures.append(
            f"round_trips_per_symbol_per_day budget ({risk.round_trips_per_symbol_per_day}) "
            f"already spent today for {proposal.symbol}"
        )

    # size_pct/limit_price/invalidation_price are all required for open/add by the
    # schema's own model_validator, so they're never None here.
    proposed_notional = proposal.size_pct * context.equity
    risk_to_invalidation_pct = proposal.size_pct * (
        abs(proposal.limit_price - proposal.invalidation_price) / proposal.limit_price
    )
    if risk_to_invalidation_pct > risk.risk_per_trade_pct:
        failures.append(
            f"risk to invalidation ({risk_to_invalidation_pct:.4f}) exceeds "
            f"risk_per_trade_pct ({risk.risk_per_trade_pct:.4f})"
        )

    existing_notional = context.positions.get(proposal.symbol, 0.0)
    signed_notional = proposed_notional if proposal.side == "buy" else -proposed_notional
    resulting_notional = existing_notional + signed_notional
    if abs(resulting_notional) / context.equity > risk.single_name_cap_pct:
        failures.append(
            f"resulting position in {proposal.symbol} exceeds single_name_cap_pct "
            f"({risk.single_name_cap_pct:.4f})"
        )

    gross = sum(abs(v) for v in context.positions.values()) + abs(signed_notional)
    if gross / context.equity > risk.gross_cap_pct:
        failures.append(f"gross exposure would exceed gross_cap_pct ({risk.gross_cap_pct:.4f})")
    net_long = sum(context.positions.values()) + signed_notional
    if net_long / context.equity > risk.net_long_cap_pct:
        failures.append(
            f"net long exposure would exceed net_long_cap_pct ({risk.net_long_cap_pct:.4f})"
        )

    daily_pnl_pct = (
        context.equity - context.equity_at_session_open
    ) / context.equity_at_session_open
    if daily_pnl_pct <= -risk.daily_loss_soft_pct:
        failures.append(f"daily_loss_soft_pct ({risk.daily_loss_soft_pct:.4f}) already breached")
    if daily_pnl_pct <= -risk.daily_loss_hard_pct:
        failures.append(f"daily_loss_hard_pct ({risk.daily_loss_hard_pct:.4f}) already breached")

    stopout_at = context.last_stopout_at.get(proposal.symbol)
    if stopout_at is not None:
        cooldown = dt.timedelta(minutes=risk.cooldown_minutes_after_stopout)
        if context.now - stopout_at < cooldown:
            failures.append(f"{proposal.symbol} is in its post-stop-out cooldown")

    projected_turnover = (context.turnover_so_far_usd + proposed_notional) / context.equity
    if projected_turnover > risk.daily_turnover_pct:
        failures.append(f"daily_turnover_pct ({risk.daily_turnover_pct:.4f}) would be exceeded")

    return failures

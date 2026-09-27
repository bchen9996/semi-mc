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
from typing import TYPE_CHECKING, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import IntradayConfig
from .ledger import IntradayLedger

if TYPE_CHECKING:
    # Deferred: intraday/options.py imports TradeProposal from here for its own type
    # hints (also TYPE_CHECKING-only), so a module-level import in either direction would
    # be circular. validate_proposal below imports options.py's check functions locally,
    # inside the function body, once both modules have already finished loading.
    from .options import OptionPricing, OptionQuote

OPEN_OR_ADD_ACTIONS = ("open", "add")


class TradeProposal(BaseModel):
    """One action the Trader proposes for one symbol. `instrument="option"` is accepted
    at the schema level (so a future options phase needed no migration) and, since Phase
    12, actually reaches :func:`validate_proposal`'s full options gate suite once
    `config.intraday.options.shadow_pricing` or `.enabled` is `True` -- still rejected
    outright while both stay `False` (the equities-only rollout stages)."""

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
    # Options only (BRIEF_INTRADAY.md's trade proposal schema: "for options the expiry,
    # strike, right, and structure"). `structure` has no short-premium value at all --
    # "never a naked short option" is enforced by the schema itself, not by a validator
    # that could be bypassed.
    expiry: dt.date | None = None
    strike: float | None = Field(None, gt=0)
    right: Literal["call", "put"] | None = None
    structure: Literal["long_call", "long_put", "debit_vertical"] | None = None

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

    @model_validator(mode="after")
    def _option_fields_match_instrument(self) -> TradeProposal:
        option_fields = ("expiry", "strike", "right", "structure")
        set_fields = [field for field in option_fields if getattr(self, field) is not None]
        if self.instrument == "option":
            missing = [field for field in option_fields if field not in set_fields]
            if missing:
                raise ValueError(f"instrument='option' requires {missing}")
        elif set_fields:
            raise ValueError(
                f"instrument={self.instrument!r} must not set {set_fields} (options-only fields)"
            )
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
    # Options only (Phase 12). All `None`/empty until intraday/jobs.py actually fetches a
    # quote and prices it (Phase 12b) -- an instrument="option" proposal validated before
    # then fails cleanly ("no options pricing available"), rather than crashing or
    # silently passing ungated. `option_quote`/`option_pricing` are this one proposal's
    # own contract only, not a per-symbol dict -- unlike equities, no two option proposals
    # in the same cycle share a contract (different strike/expiry/right), so there is
    # nothing to key a dict by ahead of time the way `prices` is keyed by symbol.
    option_quote: OptionQuote | None = None
    option_pricing: OptionPricing | None = None
    option_forecast_vol: float | None = None
    spot_price: float | None = None  # underlying's own spot -- needed to convert this
    # proposal's premium budget into an implied contract count for the delta/theta caps
    open_option_premium_usd: float = 0.0  # every currently-open option position's cost basis
    open_option_delta_pct: float = 0.0  # portfolio delta so far, as a fraction of equity
    open_option_theta_pct_today: float = 0.0  # today's cumulative theta decay, same unit
    next_earnings_date: dict[str, str] | None = (
        None  # symbol -> ISO date, jobs.py::_upcoming_earnings shape
    )


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

    if proposal.instrument == "option":
        options_cfg = context.config.options
        if not (options_cfg.shadow_pricing or options_cfg.enabled):
            failures.append("options are not enabled")
        elif proposal.action in OPEN_OR_ADD_ACTIONS:
            # Deferred import: intraday/options.py type-hints against this module's
            # TradeProposal/ValidationContext under TYPE_CHECKING only, so a module-level
            # import here would be circular -- see this file's own TYPE_CHECKING block.
            from .options import validate_option_proposal

            failures.extend(validate_option_proposal(proposal, context))

    if proposal.action not in OPEN_OR_ADD_ACTIONS:
        return failures

    risk = context.config.risk

    if proposal.expected_edge_bps is not None:
        if proposal.instrument == "option":
            # "half the spread plus 5 bps for options" (Defaults table) parallels "8 bps
            # per side for equities" -- both are *per-side* figures, doubled below for a
            # round trip the same way risk.equity_cost_bps_per_side already is. Needs the
            # quote's own spread, unlike the equity cost model, so this only runs once a
            # quote is actually available (context.option_quote is None before Phase 12b
            # wires the real fetch in; the "no options pricing available" failure above
            # already covers that case).
            options_cfg = context.config.options
            cost_bps_per_side = (
                (
                    context.option_quote.spread_pct_of_mid
                    * 1e4
                    * options_cfg.cost_model_spread_fraction
                )
                + options_cfg.cost_model_extra_bps
                if context.option_quote is not None
                else None
            )
            round_trip_cost_bps = 2 * cost_bps_per_side if cost_bps_per_side is not None else None
        else:
            round_trip_cost_bps = 2 * risk.equity_cost_bps_per_side
        if round_trip_cost_bps is not None:
            min_edge_bps = risk.min_edge_multiple * round_trip_cost_bps
            if proposal.expected_edge_bps < min_edge_bps:
                failures.append(
                    f"expected_edge_bps {proposal.expected_edge_bps:.1f} is below "
                    f"{risk.min_edge_multiple}x the modeled round-trip cost "
                    f"({min_edge_bps:.1f} bps)"
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
        # `stopout_at` is the incident log's aware-UTC `created_at`; the real `now` is
        # naive UTC (`cli.py::_intraday_now`). Naive-minus-aware raises, so normalize a
        # naive value on either side to UTC -- the same convention `check_spread_gate`
        # and the monitor's time-stop already use.
        now = context.now if context.now.tzinfo else context.now.replace(tzinfo=dt.UTC)
        if stopout_at.tzinfo is None:
            stopout_at = stopout_at.replace(tzinfo=dt.UTC)
        if now - stopout_at < cooldown:
            failures.append(f"{proposal.symbol} is in its post-stop-out cooldown")

    projected_turnover = (context.turnover_so_far_usd + proposed_notional) / context.equity
    if projected_turnover > risk.daily_turnover_pct:
        failures.append(f"daily_turnover_pct ({risk.daily_turnover_pct:.4f}) would be exceeded")

    return failures

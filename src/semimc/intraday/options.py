"""S4's options gates, liquidity filters, greeks, and IV-vs-forecast-vol check
(BRIEF_INTRADAY.md's "## Options" section) -- the module its own file tree names.

Rollout Stage 4 ("options in shadow -- proposals priced and logged but not traded") is
this module's own reason to exist: `intraday/proposals.py::validate_proposal` calls
:func:`validate_option_proposal` (below) once `config.intraday.options.shadow_pricing` or
`.enabled` is `True`, and `intraday/jobs.py::run_cycle` records whatever it finds without
ever reaching order submission while `enabled` stays `False`. Stage 5 (real order
submission) is deliberately out of scope here -- no OCC symbol construction, no
`broker.py`/`orders.py` changes; this module only prices and gates a proposal that has
already named its own contract (`TradeProposal.expiry`/`strike`/`right`/`structure`).

Same architectural shape as `pretrade.py`/`budget.py`: a frozen context object gathered
once by the caller, pure functions, `list[str]`/`str | None` returns, never raises for a
routine finding, and every placeholder numeric default disclosed in `config.py`'s own
docstring rather than here.

`TradeProposal`/`ValidationContext`/`IntradayOptionsConfig` are only imported under
`TYPE_CHECKING` -- `proposals.py`'s `ValidationContext` in turn type-hints its
`option_quote`/`option_pricing` fields against this module, so a module-level import in
either direction would be circular. Nothing here needs those classes at runtime, only for
type hints on functions that receive already-constructed instances.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import pandas as pd

from ..data.options import black_scholes_greeks, implied_volatility_from_price

if TYPE_CHECKING:
    from ..config import IntradayOptionsConfig
    from .proposals import TradeProposal, ValidationContext

_CONTRACT_MULTIPLIER = 100  # standard US equity option -- this project has no mini/exotic
# contract handling anywhere, so this isn't a validation-time simplification, it's the
# only contract size this codebase knows about.


@dataclass(frozen=True)
class OptionQuote:
    """One contract's market quote, exactly as `execution/market_data.py`'s options fetch
    (Phase 12b) returns it -- bid/ask plus the liquidity figures the brief's filters need.
    """

    bid: float
    ask: float
    open_interest: int
    volume: int
    # Which Alpaca plan actually answered -- Stage 4's own stated purpose is judging
    # whether *this* feed produces sane pricing, so which one served each quote is part
    # of the record, not an implementation detail.
    feed: Literal["indicative", "opra"]

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct_of_mid(self) -> float:
        return (self.ask - self.bid) / self.mid if self.mid > 0 else float("inf")


@dataclass(frozen=True)
class OptionPricing:
    """One proposed contract's own computed pricing -- :func:`price_contract`'s output."""

    mid_price: float
    implied_vol: float
    delta: float
    gamma: float
    vega: float
    theta: float


def price_contract(
    proposal: TradeProposal,
    quote: OptionQuote,
    spot: float,
    rate: float,
    as_of: pd.Timestamp,
) -> OptionPricing:
    """Recompute a quoted contract's implied vol and greeks from its own bid/ask
    midpoint -- never trust a feed's reported IV, the same discipline `data/options.py`'s
    own module docstring already established (a stale or illiquid quote reprices to a
    garbage number through anyone's Black-Scholes, feed-reported or not).

    Dividend yield is left at the `data.options` primitives' own 0.0 default -- this
    project's universe (semiconductor large-caps) pays negligible dividends relative to
    the short-dated contracts the brief's DTE bounds actually allow.
    """
    time_to_expiry = (pd.Timestamp(proposal.expiry) - pd.Timestamp(as_of)).days / 365.25
    # proposal.right is required (and therefore never None) whenever instrument="option",
    # enforced by TradeProposal's own model_validator -- same "schema already guarantees
    # this" reasoning proposals.py's own docstring uses for limit_price/invalidation_price.
    implied_vol = implied_volatility_from_price(
        quote.mid, spot, proposal.strike, time_to_expiry, rate, proposal.right
    )
    if not math.isfinite(implied_vol):
        # A crossed or stale quote (data.options.implied_volatility_from_price's own NaN
        # convention) -- greeks computed off a nonsense vol would be a specific-looking
        # but meaningless number, worse than an honest NaN a caller can filter on.
        nan = float("nan")
        return OptionPricing(quote.mid, implied_vol, nan, nan, nan, nan)
    greeks = black_scholes_greeks(
        spot, proposal.strike, time_to_expiry, rate, implied_vol, proposal.right
    )
    return OptionPricing(
        mid_price=quote.mid,
        implied_vol=implied_vol,
        delta=greeks["delta"],
        gamma=greeks["gamma"],
        vega=greeks["vega"],
        theta=greeks["theta"],
    )


def check_liquidity(quote: OptionQuote, config: IntradayOptionsConfig) -> str | None:
    """Brief: "minimum open interest and volume, and a maximum spread as a percent of
    mid. Reject anything outside them.\""""
    if quote.open_interest < config.min_open_interest:
        return (
            f"open interest {quote.open_interest} is below min_open_interest "
            f"({config.min_open_interest})"
        )
    if quote.volume < config.min_volume:
        return f"volume {quote.volume} is below min_volume ({config.min_volume})"
    if quote.spread_pct_of_mid > config.max_spread_pct_of_mid:
        return (
            f"spread ({quote.spread_pct_of_mid:.4f} of mid) exceeds max_spread_pct_of_mid "
            f"({config.max_spread_pct_of_mid:.4f})"
        )
    return None


def check_dte(
    proposal: TradeProposal, as_of: pd.Timestamp, config: IntradayOptionsConfig
) -> str | None:
    """Brief: "minimum 7 days to expiry, maximum 60. No 0DTE.\""""
    dte = (pd.Timestamp(proposal.expiry) - pd.Timestamp(as_of)).days
    if dte < config.min_dte:
        return f"{dte} days to expiry is below min_dte ({config.min_dte})"
    if dte > config.max_dte:
        return f"{dte} days to expiry exceeds max_dte ({config.max_dte})"
    return None


def should_close_for_dte(
    expiry: pd.Timestamp, as_of: pd.Timestamp, config: IntradayOptionsConfig
) -> bool:
    """An open option position's own *exit* rule, not an entry gate -- "close any position
    at 2 days to expiry so assignment and pin risk never come up." Not called anywhere yet
    (Phase 12): there is never a real option position to close until Stage 5 holds one.
    Exposed here for `monitor.py`/`jobs.py` to call once one exists.
    """
    dte = (pd.Timestamp(expiry) - pd.Timestamp(as_of)).days
    return dte <= config.close_at_dte


def check_premium_at_risk(
    proposal: TradeProposal,
    equity: float,
    open_option_premium_usd: float,
    config: IntradayOptionsConfig,
) -> str | None:
    """Brief: "premium at risk per position and in total" as a percent of equity.

    `size_pct` is this proposal's own share of equity, the same unit every other action
    uses (`TradeProposal`'s own docstring) -- for a long, defined-risk option it is spent
    entirely on premium (never margin, never leverage), so `size_pct` alone already *is*
    the per-position premium-at-risk fraction; no separate contract-count math is needed
    for this specific check.
    """
    if proposal.size_pct > config.premium_at_risk_pct_per_position:
        return (
            f"size_pct ({proposal.size_pct:.4f}) exceeds premium_at_risk_pct_per_position "
            f"({config.premium_at_risk_pct_per_position:.4f})"
        )
    position_premium = proposal.size_pct * equity
    total_after_pct = (open_option_premium_usd + position_premium) / equity
    if total_after_pct > config.premium_at_risk_pct_total:
        return (
            f"total premium at risk after this trade ({total_after_pct:.4f} of equity) "
            f"would exceed premium_at_risk_pct_total ({config.premium_at_risk_pct_total:.4f})"
        )
    return None


def check_iv_vs_forecast(
    proposal: TradeProposal,
    pricing: OptionPricing,
    forecast_vol: float,
    config: IntradayOptionsConfig,
) -> list[str]:
    """Brief: "only buy premium when the project's GARCH or HAR forecast volatility
    exceeds implied volatility by a margin; prefer spreads when implied is rich relative
    to the forecast."

    A hard gate for `long_call`/`long_put` (outright premium purchases); `debit_vertical`
    is the brief's own accepted way to trade richer implied vol, so it is never failed
    here regardless of the reading. `validate_proposal`'s contract is all-or-nothing
    (any returned string rejects the whole proposal) with no channel for a non-blocking
    note, so "prefer spreads when rich" is expressed as "spreads are never gated on this
    reading," not as an unenforced suggestion alongside a real gate for the other
    structures.
    """
    if math.isnan(pricing.implied_vol):
        return ["implied vol could not be computed from this quote (crossed or stale)"]
    if proposal.structure == "debit_vertical":
        return []
    if forecast_vol - pricing.implied_vol < config.min_forecast_vol_premium_over_iv:
        return [
            f"forecast vol ({forecast_vol:.4f}) does not exceed implied vol "
            f"({pricing.implied_vol:.4f}) by min_forecast_vol_premium_over_iv "
            f"({config.min_forecast_vol_premium_over_iv:.4f}) -- long premium not favored"
        ]
    return []


def check_earnings(
    proposal: TradeProposal, next_earnings_date: dict[str, str] | None
) -> str | None:
    """Brief: "no options held through an earnings print unless the thesis is explicitly
    an earnings trade, it's defined risk, and it's inside the premium cap."

    Every structure here is defined-risk by construction (the schema has no short-premium
    value) and the premium cap is `check_premium_at_risk`'s own job, so the only thing
    left to check is the "explicit earnings thesis" disclosure -- there is no dedicated
    schema field for it, so this reads `thesis`/`risk_flags` for an "earnings" mention
    (case-insensitive), the same free-text disclosure mechanism `trader.md`'s own prompt
    already asks the Trader to use for "earnings within a few days." A heuristic, disclosed
    as one rather than a structured field, matching every other placeholder in this module.
    """
    if not next_earnings_date:
        return None
    earnings_date = next_earnings_date.get(proposal.symbol)
    if earnings_date is None:
        return None
    if pd.Timestamp(proposal.expiry) < pd.Timestamp(earnings_date):
        return None  # expires before the print -- never held through it
    disclosed = (proposal.thesis or "") + " ".join(proposal.risk_flags)
    if "earnings" in disclosed.lower():
        return None
    return (
        f"{proposal.symbol} has earnings on {earnings_date}, before this contract's "
        "expiry, with no earnings thesis disclosed in thesis/risk_flags"
    )


def check_portfolio_delta_cap(
    proposal: TradeProposal,
    pricing: OptionPricing,
    spot: float,
    equity: float,
    current_portfolio_delta_pct: float,
    config: IntradayOptionsConfig,
) -> str | None:
    """Brief: "a portfolio net delta cap," expressed here as delta-equivalent notional
    over equity -- the same percent-of-equity unit every other cap in this module uses.

    `implied_contracts` is this proposal's premium budget divided by one contract's own
    cost (`_CONTRACT_MULTIPLIER`x the quoted premium) -- a continuous approximation, not
    floored to a whole contract, since this is a risk estimate at proposal time, not the
    actual order Stage 5 would eventually build.
    """
    if pricing.mid_price <= 0 or math.isnan(pricing.delta):
        return None  # nothing to size against -- check_liquidity/check_iv_vs_forecast
        # already flag a quote or pricing this broken.
    implied_contracts = (proposal.size_pct * equity) / (pricing.mid_price * _CONTRACT_MULTIPLIER)
    delta_notional_pct = implied_contracts * pricing.delta * spot * _CONTRACT_MULTIPLIER / equity
    resulting = current_portfolio_delta_pct + delta_notional_pct
    if abs(resulting) > config.net_delta_cap:
        return (
            f"resulting portfolio delta ({resulting:.4f} of equity) would exceed "
            f"net_delta_cap ({config.net_delta_cap:.4f})"
        )
    return None


def check_daily_theta_cap(
    proposal: TradeProposal,
    pricing: OptionPricing,
    equity: float,
    daily_theta_so_far_pct: float,
    config: IntradayOptionsConfig,
) -> str | None:
    """Brief: "a daily theta cap." `pricing.theta` is annualized
    (`black_scholes_greeks`'s own convention) -- divided by 365.25 here for a daily
    figure, the same conversion that function's own docstring already names. Theta is
    negative for a long option (time decay costs the holder); the cap is on the
    *magnitude* of daily decay, matching `net_delta_cap`'s own percent-of-equity unit.
    """
    if pricing.mid_price <= 0 or math.isnan(pricing.theta):
        return None
    implied_contracts = (proposal.size_pct * equity) / (pricing.mid_price * _CONTRACT_MULTIPLIER)
    daily_theta_pct = (
        abs(implied_contracts * pricing.theta * _CONTRACT_MULTIPLIER / 365.25) / equity
    )
    resulting = daily_theta_so_far_pct + daily_theta_pct
    if resulting > config.daily_theta_cap:
        return (
            f"resulting daily theta decay ({resulting:.4f} of equity) would exceed "
            f"daily_theta_cap ({config.daily_theta_cap:.4f})"
        )
    return None


def portfolio_greeks(open_option_positions: list[OptionPricing]) -> dict[str, float]:
    """Brief: "Report portfolio delta, gamma, vega, and theta every cycle" -- summed
    across every currently-open option position's own last-computed pricing.

    Built for Stage 5's sake: during Stage 4 no option position is ever actually held
    (nothing here executes), so this always returns all zeros in practice on an empty
    list until then -- the aggregation itself has nothing to do with whether execution is
    live, so it is built and tested now rather than deferred.
    """
    return {
        "delta": sum(p.delta for p in open_option_positions),
        "gamma": sum(p.gamma for p in open_option_positions),
        "vega": sum(p.vega for p in open_option_positions),
        "theta": sum(p.theta for p in open_option_positions),
    }


def validate_option_proposal(proposal: TradeProposal, context: ValidationContext) -> list[str]:
    """Every options-specific gate the brief lists, run against one already-schema-valid
    option proposal.

    Called from `proposals.py::validate_proposal` once the blanket `shadow_pricing`/
    `enabled` gate has already passed -- this function assumes that and never re-checks
    it. Requires `context.option_quote`/`context.option_pricing` to already be populated
    (`intraday/jobs.py` fetches and prices this proposal's own specific contract before
    building its `ValidationContext`, Phase 12b) -- validated without them, this fails
    with one clear reason rather than crashing on a `None` attribute access, which matters
    today because Phase 12a lands before Phase 12b wires that fetch in.
    """
    config = context.config.options
    if context.option_quote is None or context.option_pricing is None:
        return ["no options pricing available for this proposal"]

    failures: list[str] = []
    for finding in (
        check_liquidity(context.option_quote, config),
        check_dte(proposal, context.as_of, config),
        check_premium_at_risk(proposal, context.equity, context.open_option_premium_usd, config),
        check_earnings(proposal, context.next_earnings_date),
    ):
        if finding is not None:
            failures.append(finding)

    if context.option_forecast_vol is not None:
        failures.extend(
            check_iv_vs_forecast(
                proposal, context.option_pricing, context.option_forecast_vol, config
            )
        )

    if context.spot_price is not None:
        delta_finding = check_portfolio_delta_cap(
            proposal,
            context.option_pricing,
            context.spot_price,
            context.equity,
            context.open_option_delta_pct,
            config,
        )
        if delta_finding is not None:
            failures.append(delta_finding)
        theta_finding = check_daily_theta_cap(
            proposal,
            context.option_pricing,
            context.equity,
            context.open_option_theta_pct_today,
            config,
        )
        if theta_finding is not None:
            failures.append(theta_finding)

    return failures

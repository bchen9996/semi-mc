"""`intraday/options.py`: S4's options gates, pricing, and greeks aggregation (Phase 12,
BRIEF_INTRADAY.md's "## Options" section). Exercised entirely against synthetic quotes --
no network, matching this project's convention; the real Alpaca options fetch is Phase
12b's own, separately-tested module.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from semimc.config import IntradayConfig, IntradayOptionsConfig
from semimc.data.options import black_scholes_price
from semimc.intraday.options import (
    OptionPricing,
    OptionQuote,
    check_daily_theta_cap,
    check_dte,
    check_earnings,
    check_iv_vs_forecast,
    check_liquidity,
    check_portfolio_delta_cap,
    check_premium_at_risk,
    portfolio_greeks,
    price_contract,
    should_close_for_dte,
    validate_option_proposal,
)
from semimc.intraday.proposals import TradeProposal, ValidationContext

AS_OF = pd.Timestamp("2026-01-05")


def _option_proposal(**overrides) -> TradeProposal:
    fields = {
        "action": "open",
        "instrument": "option",
        "symbol": "NVDA",
        "side": "buy",
        "size_pct": 0.01,
        "limit_price": 5.00,
        "thesis": "Vol forecast exceeds implied ahead of a catalyst.",
        "invalidation_price": 3.00,
        "time_stop_minutes": 240,
        "expiry": AS_OF.date() + dt.timedelta(days=30),
        "strike": 150.0,
        "right": "call",
        "structure": "long_call",
    }
    fields.update(overrides)
    return TradeProposal(**fields)


def _quote(bid=4.80, ask=5.20, open_interest=500, volume=100, feed="opra") -> OptionQuote:
    return OptionQuote(bid=bid, ask=ask, open_interest=open_interest, volume=volume, feed=feed)


def _pricing(**overrides) -> OptionPricing:
    fields = {
        "mid_price": 5.0,
        "implied_vol": 0.30,
        "delta": 0.5,
        "gamma": 0.02,
        "vega": 0.15,
        "theta": -0.05,
    }
    fields.update(overrides)
    return OptionPricing(**fields)


def _context(options_config, **overrides) -> ValidationContext:
    fields = {
        "as_of": AS_OF,
        "now": dt.datetime(2026, 1, 5, 10, 0),
        "config": IntradayConfig(options=options_config),
        "ledger": None,
        "equity": 100_000.0,
        "equity_at_session_open": 100_000.0,
        "positions": {},
        "prices": {"NVDA": 150.0},
        "turnover_so_far_usd": 0.0,
        "last_stopout_at": {},
    }
    fields.update(overrides)
    return ValidationContext(**fields)


# ---------------------------------------------------------------------------------------
# price_contract
# ---------------------------------------------------------------------------------------


def test_price_contract_recovers_a_known_implied_vol():
    spot, strike, true_sigma, rate = 150.0, 150.0, 0.35, 0.04
    proposal = _option_proposal(strike=strike)
    time_to_expiry = (pd.Timestamp(proposal.expiry) - AS_OF).days / 365.25
    true_price = black_scholes_price(spot, strike, time_to_expiry, rate, true_sigma, "call")
    quote = _quote(bid=true_price, ask=true_price)  # zero-spread quote -> mid == true_price

    pricing = price_contract(proposal, quote, spot, rate, AS_OF)

    assert pricing.implied_vol == pytest.approx(true_sigma, abs=1e-3)
    assert pricing.delta > 0  # a call's delta is always positive
    assert pricing.mid_price == pytest.approx(true_price)


def test_price_contract_returns_nan_greeks_for_a_crossed_quote():
    proposal = _option_proposal()
    # bid > ask * 100 -- a mid price no volatility in the inversion's search range could
    # have produced (implied_volatility_from_price's own no-arbitrage-bounds convention).
    quote = _quote(bid=140.0, ask=141.0)
    pricing = price_contract(proposal, quote, spot=150.0, rate=0.04, as_of=AS_OF)
    assert pricing.implied_vol != pricing.implied_vol  # NaN
    assert pricing.delta != pricing.delta
    assert pricing.gamma != pricing.gamma
    assert pricing.vega != pricing.vega
    assert pricing.theta != pricing.theta


# ---------------------------------------------------------------------------------------
# check_liquidity
# ---------------------------------------------------------------------------------------


def test_check_liquidity_passes_a_clean_quote():
    config = IntradayOptionsConfig()
    assert check_liquidity(_quote(), config) is None


def test_check_liquidity_flags_low_open_interest():
    config = IntradayOptionsConfig()
    failure = check_liquidity(_quote(open_interest=1), config)
    assert failure is not None and "open interest" in failure


def test_check_liquidity_flags_low_volume():
    config = IntradayOptionsConfig()
    failure = check_liquidity(_quote(volume=0), config)
    assert failure is not None and "volume" in failure


def test_check_liquidity_flags_a_wide_spread():
    config = IntradayOptionsConfig()
    failure = check_liquidity(_quote(bid=1.0, ask=9.0), config)
    assert failure is not None and "spread" in failure


# ---------------------------------------------------------------------------------------
# check_dte / should_close_for_dte
# ---------------------------------------------------------------------------------------


def test_check_dte_passes_within_bounds():
    config = IntradayOptionsConfig()
    proposal = _option_proposal(expiry=AS_OF.date() + dt.timedelta(days=30))
    assert check_dte(proposal, AS_OF, config) is None


def test_check_dte_flags_too_soon():
    config = IntradayOptionsConfig()
    proposal = _option_proposal(expiry=AS_OF.date() + dt.timedelta(days=3))
    failure = check_dte(proposal, AS_OF, config)
    assert failure is not None and "min_dte" in failure


def test_check_dte_flags_too_far():
    config = IntradayOptionsConfig()
    proposal = _option_proposal(expiry=AS_OF.date() + dt.timedelta(days=90))
    failure = check_dte(proposal, AS_OF, config)
    assert failure is not None and "max_dte" in failure


def test_should_close_for_dte():
    config = IntradayOptionsConfig()
    assert should_close_for_dte(AS_OF + pd.Timedelta(days=2), AS_OF, config) is True
    assert should_close_for_dte(AS_OF + pd.Timedelta(days=10), AS_OF, config) is False


# ---------------------------------------------------------------------------------------
# check_premium_at_risk
# ---------------------------------------------------------------------------------------


def test_check_premium_at_risk_passes_a_small_position():
    config = IntradayOptionsConfig()
    proposal = _option_proposal(size_pct=0.001)
    assert (
        check_premium_at_risk(
            proposal, equity=100_000.0, open_option_premium_usd=0.0, config=config
        )
        is None
    )


def test_check_premium_at_risk_flags_an_oversized_position():
    config = IntradayOptionsConfig()
    proposal = _option_proposal(size_pct=0.05)  # far above the 0.005 per-position default
    failure = check_premium_at_risk(
        proposal, equity=100_000.0, open_option_premium_usd=0.0, config=config
    )
    assert failure is not None and "premium_at_risk_pct_per_position" in failure


def test_check_premium_at_risk_flags_the_portfolio_total_cap():
    config = IntradayOptionsConfig()
    proposal = _option_proposal(size_pct=0.001)
    # Already near the 0.02 total cap from other open option positions.
    failure = check_premium_at_risk(
        proposal, equity=100_000.0, open_option_premium_usd=19_950.0, config=config
    )
    assert failure is not None and "premium_at_risk_pct_total" in failure


# ---------------------------------------------------------------------------------------
# check_iv_vs_forecast
# ---------------------------------------------------------------------------------------


def test_check_iv_vs_forecast_passes_when_forecast_comfortably_exceeds_implied():
    config = IntradayOptionsConfig()
    proposal = _option_proposal(structure="long_call")
    pricing = _pricing(implied_vol=0.20)
    assert check_iv_vs_forecast(proposal, pricing, forecast_vol=0.40, config=config) == []


def test_check_iv_vs_forecast_fails_long_premium_when_margin_is_insufficient():
    config = IntradayOptionsConfig()
    proposal = _option_proposal(structure="long_call")
    pricing = _pricing(implied_vol=0.35)
    failures = check_iv_vs_forecast(proposal, pricing, forecast_vol=0.37, config=config)
    assert len(failures) == 1 and "long premium not favored" in failures[0]


def test_check_iv_vs_forecast_never_fails_a_debit_vertical():
    config = IntradayOptionsConfig()
    proposal = _option_proposal(structure="debit_vertical")
    pricing = _pricing(implied_vol=0.80)  # richly priced -- exactly when a spread is preferred
    assert check_iv_vs_forecast(proposal, pricing, forecast_vol=0.20, config=config) == []


def test_check_iv_vs_forecast_flags_a_nan_implied_vol():
    config = IntradayOptionsConfig()
    proposal = _option_proposal(structure="long_call")
    pricing = _pricing(implied_vol=float("nan"))
    failures = check_iv_vs_forecast(proposal, pricing, forecast_vol=0.40, config=config)
    assert len(failures) == 1 and "could not be computed" in failures[0]


# ---------------------------------------------------------------------------------------
# check_earnings
# ---------------------------------------------------------------------------------------


def test_check_earnings_passes_with_no_earnings_data():
    proposal = _option_proposal()
    assert check_earnings(proposal, None) is None
    assert check_earnings(proposal, {}) is None


def test_check_earnings_passes_when_expiry_is_before_the_print():
    proposal = _option_proposal(expiry=AS_OF.date() + dt.timedelta(days=5))
    next_earnings = {"NVDA": str((AS_OF + pd.Timedelta(days=20)).date())}
    assert check_earnings(proposal, next_earnings) is None


def test_check_earnings_flags_an_undisclosed_earnings_hold():
    proposal = _option_proposal(
        expiry=AS_OF.date() + dt.timedelta(days=30), thesis="A momentum breakout setup."
    )
    next_earnings = {"NVDA": str((AS_OF + pd.Timedelta(days=10)).date())}
    failure = check_earnings(proposal, next_earnings)
    assert failure is not None and "earnings" in failure


def test_check_earnings_passes_a_disclosed_earnings_thesis():
    proposal = _option_proposal(
        expiry=AS_OF.date() + dt.timedelta(days=30),
        thesis="An explicit earnings trade ahead of the print.",
    )
    next_earnings = {"NVDA": str((AS_OF + pd.Timedelta(days=10)).date())}
    assert check_earnings(proposal, next_earnings) is None


# ---------------------------------------------------------------------------------------
# check_portfolio_delta_cap / check_daily_theta_cap
# ---------------------------------------------------------------------------------------


def test_check_portfolio_delta_cap_passes_a_small_position():
    config = IntradayOptionsConfig()
    proposal = _option_proposal(size_pct=0.001)
    pricing = _pricing(delta=0.5)
    failure = check_portfolio_delta_cap(
        proposal,
        pricing,
        spot=150.0,
        equity=100_000.0,
        current_portfolio_delta_pct=0.0,
        config=config,
    )
    assert failure is None


def test_check_portfolio_delta_cap_flags_exceeding_the_cap():
    config = IntradayOptionsConfig()
    proposal = _option_proposal(size_pct=0.005)
    pricing = _pricing(delta=0.9, mid_price=1.0)  # cheap, high-delta -- many implied contracts
    failure = check_portfolio_delta_cap(
        proposal,
        pricing,
        spot=150.0,
        equity=100_000.0,
        current_portfolio_delta_pct=0.0,
        config=config,
    )
    assert failure is not None and "net_delta_cap" in failure


def test_check_daily_theta_cap_flags_exceeding_the_cap():
    config = IntradayOptionsConfig()
    proposal = _option_proposal(size_pct=0.005)
    pricing = _pricing(theta=-400.0, mid_price=1.0)  # large annualized decay, cheap contract
    failure = check_daily_theta_cap(
        proposal, pricing, equity=100_000.0, daily_theta_so_far_pct=0.0, config=config
    )
    assert failure is not None and "daily_theta_cap" in failure


# ---------------------------------------------------------------------------------------
# portfolio_greeks
# ---------------------------------------------------------------------------------------


def test_portfolio_greeks_sums_across_positions():
    positions = [
        _pricing(delta=0.5, gamma=0.02, vega=0.1, theta=-0.05),
        _pricing(delta=-0.3, gamma=0.01, vega=0.2, theta=-0.02),
    ]
    totals = portfolio_greeks(positions)
    assert totals == {
        "delta": pytest.approx(0.2),
        "gamma": pytest.approx(0.03),
        "vega": pytest.approx(0.3),
        "theta": pytest.approx(-0.07),
    }


def test_portfolio_greeks_is_all_zero_with_no_positions():
    assert portfolio_greeks([]) == {"delta": 0, "gamma": 0, "vega": 0, "theta": 0}


# ---------------------------------------------------------------------------------------
# validate_option_proposal (the aggregator proposals.py::validate_proposal calls)
# ---------------------------------------------------------------------------------------


def test_validate_option_proposal_fails_cleanly_with_no_pricing():
    config = IntradayOptionsConfig(shadow_pricing=True)
    context = _context(config)  # option_quote/option_pricing default to None
    failures = validate_option_proposal(_option_proposal(), context)
    assert failures == ["no options pricing available for this proposal"]


def test_validate_option_proposal_passes_a_clean_proposal():
    config = IntradayOptionsConfig(shadow_pricing=True)
    context = _context(
        config,
        option_quote=_quote(),
        option_pricing=_pricing(implied_vol=0.20, delta=0.3),
        option_forecast_vol=0.40,
        spot_price=150.0,
    )
    assert validate_option_proposal(_option_proposal(size_pct=0.001), context) == []


def test_validate_option_proposal_surfaces_a_liquidity_failure():
    config = IntradayOptionsConfig(shadow_pricing=True)
    context = _context(
        config,
        option_quote=_quote(open_interest=0),
        option_pricing=_pricing(implied_vol=0.20),
        option_forecast_vol=0.40,
        spot_price=150.0,
    )
    failures = validate_option_proposal(_option_proposal(size_pct=0.001), context)
    assert any("open interest" in f for f in failures)

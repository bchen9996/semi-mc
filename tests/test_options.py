"""Options-implied volatility: Black-Scholes pricing/inversion, liquidity filtering, and
the term-structure interpolation -- everything in `data/options.py` except the one
function that actually calls yfinance (`fetch_raw_option_chains`), which is untested here
for the same reason `fetch_calendar_from_provider`/`fetch_fred_series` are untested
elsewhere: this project's tests never touch the network.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from semimc.data.options import (
    ImpliedVolTermStructure,
    black_scholes_greeks,
    black_scholes_price,
    implied_annual_vol_for_horizon,
    implied_vol_for_expiry,
    implied_vol_term_structure,
    implied_volatility_from_price,
    select_liquid_otm_quotes,
)

# ---------------------------------------------------------------------------------------
# Black-Scholes pricing
# ---------------------------------------------------------------------------------------


def test_call_price_increases_with_spot():
    low = black_scholes_price(90, 100, 0.25, 0.04, 0.30, "call")
    high = black_scholes_price(110, 100, 0.25, 0.04, 0.30, "call")
    assert high > low


def test_put_call_parity_holds():
    spot, strike, t, r, sigma, q = 100.0, 105.0, 0.5, 0.04, 0.25, 0.01
    call = black_scholes_price(spot, strike, t, r, sigma, "call", q)
    put = black_scholes_price(spot, strike, t, r, sigma, "put", q)
    assert (call - put) == pytest.approx(spot * np.exp(-q * t) - strike * np.exp(-r * t), abs=1e-9)


def test_price_at_expiry_is_intrinsic_value():
    assert black_scholes_price(110, 100, 0.0, 0.04, 0.30, "call") == pytest.approx(10.0)
    assert black_scholes_price(90, 100, 0.0, 0.04, 0.30, "call") == pytest.approx(0.0)
    assert black_scholes_price(90, 100, 0.0, 0.04, 0.30, "put") == pytest.approx(10.0)


def test_rejects_an_unknown_option_type():
    with pytest.raises(ValueError, match="call.*put"):
        black_scholes_price(100, 100, 0.25, 0.04, 0.3, "straddle")


# ---------------------------------------------------------------------------------------
# Greeks (Phase 12: S4's options.py needs delta/gamma/vega/theta)
# ---------------------------------------------------------------------------------------


def test_greeks_delta_matches_a_finite_difference_of_price():
    spot, strike, t, r, sigma = 100.0, 105.0, 0.5, 0.04, 0.30
    bump = 0.01
    price_up = black_scholes_price(spot + bump, strike, t, r, sigma, "call")
    price_down = black_scholes_price(spot - bump, strike, t, r, sigma, "call")
    finite_diff_delta = (price_up - price_down) / (2 * bump)
    greeks = black_scholes_greeks(spot, strike, t, r, sigma, "call")
    assert greeks["delta"] == pytest.approx(finite_diff_delta, abs=1e-4)


def test_greeks_gamma_matches_a_finite_difference_of_delta():
    spot, strike, t, r, sigma = 100.0, 105.0, 0.5, 0.04, 0.30
    bump = 0.01
    delta_up = black_scholes_greeks(spot + bump, strike, t, r, sigma, "call")["delta"]
    delta_down = black_scholes_greeks(spot - bump, strike, t, r, sigma, "call")["delta"]
    finite_diff_gamma = (delta_up - delta_down) / (2 * bump)
    gamma = black_scholes_greeks(spot, strike, t, r, sigma, "call")["gamma"]
    assert gamma == pytest.approx(finite_diff_gamma, abs=1e-3)


def test_greeks_deep_itm_call_delta_approaches_one():
    greeks = black_scholes_greeks(200, 100, 0.25, 0.04, 0.20, "call")
    assert greeks["delta"] == pytest.approx(1.0, abs=1e-3)


def test_greeks_deep_otm_call_delta_approaches_zero():
    greeks = black_scholes_greeks(50, 100, 0.25, 0.04, 0.20, "call")
    assert greeks["delta"] == pytest.approx(0.0, abs=1e-3)


def test_greeks_put_call_delta_parity():
    spot, strike, t, r, sigma, q = 100.0, 105.0, 0.5, 0.04, 0.25, 0.01
    call_delta = black_scholes_greeks(spot, strike, t, r, sigma, "call", q)["delta"]
    put_delta = black_scholes_greeks(spot, strike, t, r, sigma, "put", q)["delta"]
    assert (call_delta - put_delta) == pytest.approx(np.exp(-q * t), abs=1e-9)


def test_greeks_gamma_and_vega_are_identical_for_call_and_put():
    spot, strike, t, r, sigma = 100.0, 105.0, 0.5, 0.04, 0.25
    call = black_scholes_greeks(spot, strike, t, r, sigma, "call")
    put = black_scholes_greeks(spot, strike, t, r, sigma, "put")
    assert call["gamma"] == pytest.approx(put["gamma"])
    assert call["vega"] == pytest.approx(put["vega"])


def test_greeks_at_expiry_have_zero_gamma_vega_theta():
    for option_type in ("call", "put"):
        greeks = black_scholes_greeks(110, 100, 0.0, 0.04, 0.30, option_type)
        assert greeks["gamma"] == 0.0
        assert greeks["vega"] == 0.0
        assert greeks["theta"] == 0.0


def test_greeks_at_expiry_delta_is_the_intrinsic_step_function():
    assert black_scholes_greeks(110, 100, 0.0, 0.04, 0.30, "call")["delta"] == pytest.approx(1.0)
    assert black_scholes_greeks(90, 100, 0.0, 0.04, 0.30, "call")["delta"] == pytest.approx(0.0)
    assert black_scholes_greeks(90, 100, 0.0, 0.04, 0.30, "put")["delta"] == pytest.approx(-1.0)
    assert black_scholes_greeks(110, 100, 0.0, 0.04, 0.30, "put")["delta"] == pytest.approx(0.0)


def test_greeks_rejects_an_unknown_option_type():
    with pytest.raises(ValueError, match="call.*put"):
        black_scholes_greeks(100, 100, 0.25, 0.04, 0.3, "straddle")


# ---------------------------------------------------------------------------------------
# Implied volatility inversion
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("true_sigma", [0.15, 0.30, 0.60, 1.20])
@pytest.mark.parametrize("option_type", ["call", "put"])
def test_implied_vol_round_trips_a_known_price(true_sigma, option_type):
    spot, strike, t, r = 100.0, 105.0, 0.25, 0.04
    price = black_scholes_price(spot, strike, t, r, true_sigma, option_type)
    recovered = implied_volatility_from_price(price, spot, strike, t, r, option_type)
    assert recovered == pytest.approx(true_sigma, abs=1e-4)


def test_implied_vol_returns_nan_for_a_price_outside_no_arbitrage_bounds():
    """A price above what any volatility in [1e-4, 5.0] could produce -- a crossed or
    stale quote -- must come back NaN, never the nearest boundary sigma."""
    spot, strike, t, r = 100.0, 100.0, 0.25, 0.04
    absurd_price = spot * 2  # far above any real call price at these terms
    assert np.isnan(implied_volatility_from_price(absurd_price, spot, strike, t, r, "call"))


def test_implied_vol_returns_nan_for_a_negative_price():
    assert np.isnan(implied_volatility_from_price(-1.0, 100.0, 100.0, 0.25, 0.04, "call"))


# ---------------------------------------------------------------------------------------
# Liquidity filtering
# ---------------------------------------------------------------------------------------


def _quote(strike, bid, ask, volume=10, open_interest=10):
    return {
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "volume": volume,
        "openInterest": open_interest,
    }


def test_select_liquid_otm_quotes_drops_illiquid_and_crossed_rows():
    spot = 100.0
    calls = pd.DataFrame(
        [
            _quote(105, 3.0, 3.2),  # OTM call, liquid -- kept
            _quote(110, 0.0, 0.0),  # crossed/zero bid -- dropped
            _quote(115, 1.0, 1.1, volume=0, open_interest=0),  # no liquidity -- dropped
            _quote(95, 5.0, 5.2),  # ITM call -- dropped (only OTM calls kept)
        ]
    )
    puts = pd.DataFrame(
        [
            _quote(95, 2.5, 2.7),  # OTM put, liquid -- kept
            _quote(80, 0.5, 0.6),  # outside the moneyness band -- dropped
        ]
    )
    result = select_liquid_otm_quotes(calls, puts, spot, moneyness_band=0.10)
    assert set(result["strike"]) == {105.0, 95.0}
    assert set(result["option_type"]) == {"call", "put"}
    assert (result["mid"] > 0).all()


def test_select_liquid_otm_quotes_handles_empty_frames():
    result = select_liquid_otm_quotes(pd.DataFrame(), pd.DataFrame(), 100.0)
    assert result.empty


# ---------------------------------------------------------------------------------------
# Per-expiry implied vol
# ---------------------------------------------------------------------------------------


def _synthetic_liquid_quotes(spot, true_sigma, t, r, strikes):
    rows = []
    for strike in strikes:
        option_type = "call" if strike >= spot else "put"
        price = black_scholes_price(spot, strike, t, r, true_sigma, option_type)
        rows.append(
            {
                "strike": strike,
                "option_type": option_type,
                "bid": price - 0.01,
                "ask": price + 0.01,
                "mid": price,
                "volume": 100,
                "open_interest": 100,
            }
        )
    return pd.DataFrame(rows)


def test_implied_vol_for_expiry_recovers_a_known_flat_vol():
    spot, true_sigma, t, r = 100.0, 0.35, 0.25, 0.04
    quotes = _synthetic_liquid_quotes(spot, true_sigma, t, r, [95, 100, 105, 110])
    vol, n_quotes = implied_vol_for_expiry(quotes, spot, r, t)
    assert n_quotes == 4
    assert vol == pytest.approx(true_sigma, abs=1e-3)


def test_implied_vol_for_expiry_requires_the_minimum_quote_count():
    spot, true_sigma, t, r = 100.0, 0.35, 0.25, 0.04
    quotes = _synthetic_liquid_quotes(spot, true_sigma, t, r, [105])
    vol, n_quotes = implied_vol_for_expiry(quotes, spot, r, t, min_quotes=2)
    assert np.isnan(vol)
    assert n_quotes == 1


def test_implied_vol_for_expiry_rejects_a_same_day_expiry():
    quotes = _synthetic_liquid_quotes(100.0, 0.30, 0.25, 0.04, [105])
    vol, n_quotes = implied_vol_for_expiry(quotes, 100.0, 0.04, time_to_expiry=0.0)
    assert np.isnan(vol)
    assert n_quotes == 0


# ---------------------------------------------------------------------------------------
# Term structure and horizon interpolation
# ---------------------------------------------------------------------------------------


def _synthetic_chain_frames(spot, true_sigma, t, r, strikes):
    quotes = _synthetic_liquid_quotes(spot, true_sigma, t, r, strikes)
    calls = quotes[quotes["option_type"] == "call"].rename(
        columns={"open_interest": "openInterest"}
    )
    puts = quotes[quotes["option_type"] == "put"].rename(columns={"open_interest": "openInterest"})
    return {"calls": calls, "puts": puts}


def test_term_structure_recovers_distinct_vols_per_expiry():
    spot, r = 100.0, 0.04
    as_of = pd.Timestamp("2026-01-01")
    strikes = [95, 100, 105, 110]
    expiries = {
        "2026-01-31": _synthetic_chain_frames(spot, 0.20, 30 / 365.25, r, strikes),
        "2026-04-01": _synthetic_chain_frames(spot, 0.35, 90 / 365.25, r, strikes),
    }
    structure = implied_vol_term_structure("TEST", spot, as_of, expiries, r)
    assert isinstance(structure, ImpliedVolTermStructure)
    assert len(structure.curve) == 2
    curve = structure.curve.sort_values("time_to_expiry")
    assert curve["implied_vol"].iloc[0] == pytest.approx(0.20, abs=1e-2)
    assert curve["implied_vol"].iloc[1] == pytest.approx(0.35, abs=1e-2)
    assert structure.diagnostics["n_expiries_usable"] == 2


def test_annual_vol_for_horizon_holds_flat_before_the_first_expiry():
    curve = pd.DataFrame(
        {
            "expiry": [pd.Timestamp("2026-01-31"), pd.Timestamp("2026-04-01")],
            "time_to_expiry": [30 / 365.25, 90 / 365.25],
            "implied_vol": [0.20, 0.35],
            "n_quotes": [4, 4],
        }
    )
    # A horizon well inside the first expiry -- must hold flat at its vol, not extrapolate
    # backward toward zero.
    vol, diagnostics = implied_annual_vol_for_horizon(
        curve, horizon_days=5, trading_days_per_year=365
    )
    assert vol == pytest.approx(0.20, abs=1e-6)
    assert diagnostics["method"] == "flat_at_nearest_expiry"


def test_annual_vol_for_horizon_interpolates_between_two_expiries():
    curve = pd.DataFrame(
        {
            "expiry": [pd.Timestamp("2026-01-31"), pd.Timestamp("2026-04-01")],
            "time_to_expiry": [30 / 365.25, 90 / 365.25],
            "implied_vol": [0.20, 0.40],
            "n_quotes": [4, 4],
        }
    )
    horizon_days = 45  # strictly between the two expiries
    vol, diagnostics = implied_annual_vol_for_horizon(
        curve, horizon_days=horizon_days, trading_days_per_year=365
    )
    assert 0.20 < vol < 0.40
    assert diagnostics["method"] == "variance_interpolation"


def test_annual_vol_for_horizon_holds_flat_outside_the_listed_range():
    curve = pd.DataFrame(
        {
            "expiry": [pd.Timestamp("2026-01-31"), pd.Timestamp("2026-02-15")],
            "time_to_expiry": [30 / 365.25, 45 / 365.25],
            "implied_vol": [0.20, 0.25],
            "n_quotes": [4, 4],
        }
    )
    # Far beyond the last listed expiry: must hold at the last vol, not extrapolate.
    vol, diagnostics = implied_annual_vol_for_horizon(
        curve, horizon_days=252, trading_days_per_year=365
    )
    assert vol == pytest.approx(0.25)
    assert diagnostics["expiry_used"] == "2026-02-15"


def test_annual_vol_for_horizon_returns_nan_with_no_usable_expiries():
    curve = pd.DataFrame({"expiry": [], "time_to_expiry": [], "implied_vol": [], "n_quotes": []})
    vol, diagnostics = implied_annual_vol_for_horizon(curve, horizon_days=21)
    assert np.isnan(vol)
    assert diagnostics["n_usable_expiries"] == 0


def test_term_structure_wrapper_method_delegates_to_the_function():
    structure = ImpliedVolTermStructure(
        ticker="TEST",
        spot=100.0,
        as_of=pd.Timestamp("2026-01-01"),
        curve=pd.DataFrame(
            {
                "expiry": [pd.Timestamp("2026-01-31")],
                "time_to_expiry": [30 / 365.25],
                "implied_vol": [0.22],
                "n_quotes": [4],
            }
        ),
    )
    vol, _ = structure.annual_vol_for_horizon(30, trading_days_per_year=365)
    assert vol == pytest.approx(0.22)

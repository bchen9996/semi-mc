"""Options-implied volatility (brief §6 stretch item: "Calibrating volatility to
options-implied volatility").

**Live-only, and that is a real, disclosed limitation, not an oversight.** Every other
number in this project is either backtested or explicitly flagged when it cannot be. Free
historical option chains do not exist (the same reason the addendum declined Heston: "free
historical option data for backtests is hard to find"), so this module can calibrate only
to *today's* market. It has no walk-forward counterpart, cannot appear in `semimc backtest`,
and its accuracy is simply unverifiable within this project -- there is no history to check
it against. It is wired into `semimc simulate` only, clearly labelled.

Two things this module deliberately does *not* trust:

**Yahoo's own ``impliedVolatility`` field.** A live check on NVDA turned up deep in-the-money
contracts, `openInterest = 1`, no volume, reporting "implied vols" of 1300% and 870% -- a
stale or non-existent market re-priced through Black-Scholes with a garbage quote. Every
implied vol here is instead recomputed from the quoted bid/ask midpoint via our own
Black-Scholes inversion, after filtering to strikes that plausibly traded, so a Black-
Scholes assumption failure produces a `NaN`, not a number quietly built into a forecast.

**A single expiry as an answer to an arbitrary horizon.** ``simulate --horizon`` rarely
lands exactly on a listed expiry date. :func:`implied_annual_vol_for_horizon` interpolates
*linearly in total variance* (``sigma^2 * T``, the additive quantity under
independent-increments diffusion -- not linearly in volatility itself) between the two
listed expiries bracketing the horizon, and holds flat at the nearer endpoint's vol when the
horizon falls outside the listed range altogether rather than extrapolating a trend that
was never actually quoted.

One more approximation, disclosed rather than engineered around: option time-to-expiry is
computed on a calendar-day/365.25 basis (market convention), while the rest of this project
annualizes on ``trading_days_per_year`` (252, trading-day convention). The two conventions
differ by under 1% in annualized-vol terms for horizons of a few weeks to a few months,
which is smaller than the bid/ask spread this whole calibration is already averaging over.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import stats
from scipy.optimize import brentq

LOGGER = logging.getLogger(__name__)

_MIN_SIGMA = 1e-4
_MAX_SIGMA = 5.0  # 500% annualized -- a bound wide enough to never bind on a real quote
_MIN_TIME_TO_EXPIRY_YEARS = 1.0 / 365.25  # same-day expiries are excluded, not inverted


class OptionsUnavailableError(RuntimeError):
    """Raised when no usable option chain or spot price could be fetched or computed."""


def black_scholes_price(
    spot: float,
    strike: float,
    time_to_expiry: float,
    rate: float,
    sigma: float,
    option_type: str,
    dividend_yield: float = 0.0,
) -> float:
    """European Black-Scholes-Merton price with a continuous dividend yield.

    ``option_type`` is ``"call"`` or ``"put"``. Returns the discounted intrinsic value
    directly at ``time_to_expiry <= 0`` or ``sigma <= 0`` rather than dividing by zero.
    """
    if option_type not in ("call", "put"):
        raise ValueError(f"option_type must be 'call' or 'put', got {option_type!r}")
    s, k, t, r, q, sig = (
        float(spot),
        float(strike),
        float(time_to_expiry),
        float(rate),
        float(dividend_yield),
        float(sigma),
    )
    if t <= 0 or sig <= 0:
        forward_intrinsic = s * np.exp(-q * t) - k * np.exp(-r * t)
        return (
            max(forward_intrinsic, 0.0) if option_type == "call" else max(-forward_intrinsic, 0.0)
        )

    d1 = (np.log(s / k) + (r - q + 0.5 * sig * sig) * t) / (sig * np.sqrt(t))
    d2 = d1 - sig * np.sqrt(t)
    if option_type == "call":
        return s * np.exp(-q * t) * stats.norm.cdf(d1) - k * np.exp(-r * t) * stats.norm.cdf(d2)
    return k * np.exp(-r * t) * stats.norm.cdf(-d2) - s * np.exp(-q * t) * stats.norm.cdf(-d1)


def black_scholes_greeks(
    spot: float,
    strike: float,
    time_to_expiry: float,
    rate: float,
    sigma: float,
    option_type: str,
    dividend_yield: float = 0.0,
) -> dict[str, float]:
    """Black-Scholes-Merton delta, gamma, vega, and theta -- the same closed-form family
    and edge-case handling as :func:`black_scholes_price`.

    ``vega`` is per unit change in ``sigma`` (not per percentage point); ``theta`` is
    annualized (this project's simulation convention), not per calendar day -- divide by
    365.25 for a daily figure, matching this module's own disclosed day-count convention
    for ``time_to_expiry`` itself. At the ``time_to_expiry <= 0`` or ``sigma <= 0``
    boundary the option has only intrinsic value left: gamma, vega, and theta are all
    exactly zero (no time value left to decay, or to be sensitive to volatility), and
    delta collapses to the step function an already-expired (or zero-vol) option's price
    implies.
    """
    if option_type not in ("call", "put"):
        raise ValueError(f"option_type must be 'call' or 'put', got {option_type!r}")
    s, k, t, r, q, sig = (
        float(spot),
        float(strike),
        float(time_to_expiry),
        float(rate),
        float(dividend_yield),
        float(sigma),
    )
    if t <= 0 or sig <= 0:
        forward_intrinsic = s * np.exp(-q * t) - k * np.exp(-r * t)
        in_the_money = forward_intrinsic > 0 if option_type == "call" else forward_intrinsic < 0
        delta = (1.0 if option_type == "call" else -1.0) * np.exp(-q * t) if in_the_money else 0.0
        return {"delta": float(delta), "gamma": 0.0, "vega": 0.0, "theta": 0.0}

    d1 = (np.log(s / k) + (r - q + 0.5 * sig * sig) * t) / (sig * np.sqrt(t))
    d2 = d1 - sig * np.sqrt(t)
    pdf_d1 = stats.norm.pdf(d1)
    gamma = np.exp(-q * t) * pdf_d1 / (s * sig * np.sqrt(t))
    vega = s * np.exp(-q * t) * pdf_d1 * np.sqrt(t)
    if option_type == "call":
        delta = np.exp(-q * t) * stats.norm.cdf(d1)
        theta = (
            -s * np.exp(-q * t) * pdf_d1 * sig / (2 * np.sqrt(t))
            - r * k * np.exp(-r * t) * stats.norm.cdf(d2)
            + q * s * np.exp(-q * t) * stats.norm.cdf(d1)
        )
    else:
        delta = np.exp(-q * t) * (stats.norm.cdf(d1) - 1.0)
        theta = (
            -s * np.exp(-q * t) * pdf_d1 * sig / (2 * np.sqrt(t))
            + r * k * np.exp(-r * t) * stats.norm.cdf(-d2)
            - q * s * np.exp(-q * t) * stats.norm.cdf(-d1)
        )
    return {
        "delta": float(delta),
        "gamma": float(gamma),
        "vega": float(vega),
        "theta": float(theta),
    }


def implied_volatility_from_price(
    price: float,
    spot: float,
    strike: float,
    time_to_expiry: float,
    rate: float,
    option_type: str,
    dividend_yield: float = 0.0,
) -> float:
    """Invert Black-Scholes for the volatility a quoted price implies.

    Brent's method on ``sigma in [1e-4, 5.0]``, bracketed by the no-arbitrage price bounds
    at each end. Returns ``NaN`` -- never raises, and never the nearest boundary value --
    when the quoted price sits outside what any volatility in that range could produce
    (a crossed or stale quote), so a bad quote is excluded from an average rather than
    silently dragging it toward 0% or 500%.
    """
    low = black_scholes_price(
        spot, strike, time_to_expiry, rate, _MIN_SIGMA, option_type, dividend_yield
    )
    high = black_scholes_price(
        spot, strike, time_to_expiry, rate, _MAX_SIGMA, option_type, dividend_yield
    )
    if not (min(low, high) <= price <= max(low, high)):
        return float("nan")

    def _objective(sigma: float) -> float:
        return (
            black_scholes_price(
                spot, strike, time_to_expiry, rate, sigma, option_type, dividend_yield
            )
            - price
        )

    try:
        return float(brentq(_objective, _MIN_SIGMA, _MAX_SIGMA, xtol=1e-6, maxiter=200))
    except ValueError:
        return float("nan")


def select_liquid_otm_quotes(
    calls: pd.DataFrame,
    puts: pd.DataFrame,
    spot: float,
    moneyness_band: float = 0.10,
) -> pd.DataFrame:
    """Out-of-the-money calls and puts within ``moneyness_band`` of spot, filtered to
    quotes that plausibly traded.

    OTM contracts are used exclusively -- they are the liquid, tightly-quoted side of the
    market by convention; an ITM option's quote is dominated by intrinsic value and its
    time-value (the part implied vol actually measures) is a small, noisy residual.
    "Plausibly traded" means a positive bid, an ask strictly above the bid, and nonzero
    volume or open interest -- the `openInterest = 1`, no-volume contracts that produced
    the 1300%/870% "implied vols" this module was written specifically not to trust.
    """
    frames = []
    if calls is not None and not calls.empty:
        otm_calls = calls[calls["strike"] >= spot].copy()
        otm_calls["option_type"] = "call"
        frames.append(otm_calls)
    if puts is not None and not puts.empty:
        otm_puts = puts[puts["strike"] <= spot].copy()
        otm_puts["option_type"] = "put"
        frames.append(otm_puts)
    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    lower, upper = spot * (1.0 - moneyness_band), spot * (1.0 + moneyness_band)
    combined = combined[(combined["strike"] >= lower) & (combined["strike"] <= upper)]

    bid = combined.get("bid", pd.Series(0.0, index=combined.index)).fillna(0.0)
    ask = combined.get("ask", pd.Series(0.0, index=combined.index)).fillna(0.0)
    volume = combined.get("volume", pd.Series(0.0, index=combined.index)).fillna(0.0)
    open_interest = combined.get("openInterest", pd.Series(0.0, index=combined.index)).fillna(0.0)
    liquid = (bid > 0) & (ask > bid) & ((volume > 0) | (open_interest > 0))

    out = combined.loc[liquid].copy()
    out["bid"] = bid.loc[liquid]
    out["ask"] = ask.loc[liquid]
    out["mid"] = (out["bid"] + out["ask"]) / 2.0
    out["volume"] = volume.loc[liquid]
    out["open_interest"] = open_interest.loc[liquid]
    return out.reset_index(drop=True)


def implied_vol_for_expiry(
    quotes: pd.DataFrame,
    spot: float,
    rate: float,
    time_to_expiry: float,
    dividend_yield: float = 0.0,
    min_quotes: int = 2,
) -> tuple[float, int]:
    """Volume-weighted mean implied vol across liquid OTM quotes at one expiry.

    Returns ``(nan, 0)`` rather than a number built from fewer than ``min_quotes``
    successfully inverted quotes -- an "average" of one noisy point is not an average.
    """
    if quotes.empty or time_to_expiry < _MIN_TIME_TO_EXPIRY_YEARS:
        return float("nan"), 0

    inverted = [
        implied_volatility_from_price(
            float(row["mid"]),
            spot,
            float(row["strike"]),
            time_to_expiry,
            rate,
            str(row["option_type"]),
            dividend_yield,
        )
        for _, row in quotes.iterrows()
    ]
    frame = quotes.assign(implied_vol=inverted)
    usable = frame[np.isfinite(frame["implied_vol"]) & (frame["implied_vol"] > 0)]
    if len(usable) < min_quotes:
        return float("nan"), int(len(usable))

    weights = usable["volume"].to_numpy(dtype=float)
    if weights.sum() <= 0:
        weights = np.ones(len(usable))
    weighted_mean = float(np.average(usable["implied_vol"].to_numpy(dtype=float), weights=weights))
    return weighted_mean, int(len(usable))


@dataclass
class ImpliedVolTermStructure:
    """One market snapshot's implied-vol-by-expiry curve, and enough to explain it."""

    ticker: str
    spot: float
    as_of: pd.Timestamp
    curve: pd.DataFrame  # columns: expiry, time_to_expiry, implied_vol, n_quotes
    diagnostics: dict = field(default_factory=dict)

    def annual_vol_for_horizon(
        self, horizon_days: int, trading_days_per_year: int = 252
    ) -> tuple[float, dict]:
        return implied_annual_vol_for_horizon(self.curve, horizon_days, trading_days_per_year)


def implied_vol_term_structure(
    ticker: str,
    spot: float,
    as_of: pd.Timestamp,
    expiries: dict[str, dict[str, pd.DataFrame]],
    rate: float,
    dividend_yield: float = 0.0,
    moneyness_band: float = 0.10,
    max_expiries: int = 6,
    min_quotes: int = 2,
) -> ImpliedVolTermStructure:
    """Build the implied-vol term structure from an already-fetched set of option chains.

    Pure function of its inputs -- no network call -- so it is fully testable against a
    synthetic chain. :func:`fetch_implied_vol_term_structure` is the thin wrapper that
    actually calls yfinance and hands its result here.

    Parameters
    ----------
    expiries
        ``{expiry_date_string: {"calls": DataFrame, "puts": DataFrame}}``, ordered nearest
        first. Only the first ``max_expiries`` are used, same-day expiries excluded.
    """
    rows = []
    for expiry_str in list(expiries)[:max_expiries]:
        expiry = pd.Timestamp(expiry_str)
        time_to_expiry = (expiry - pd.Timestamp(as_of)).total_seconds() / (365.25 * 24 * 3600)
        if time_to_expiry < _MIN_TIME_TO_EXPIRY_YEARS:
            continue
        chain = expiries[expiry_str]
        quotes = select_liquid_otm_quotes(
            chain.get("calls"), chain.get("puts"), spot, moneyness_band
        )
        vol, n_quotes = implied_vol_for_expiry(
            quotes, spot, rate, time_to_expiry, dividend_yield, min_quotes
        )
        rows.append(
            {
                "expiry": expiry,
                "time_to_expiry": time_to_expiry,
                "implied_vol": vol,
                "n_quotes": n_quotes,
            }
        )

    curve = pd.DataFrame(rows).sort_values("time_to_expiry").reset_index(drop=True)
    usable = curve[np.isfinite(curve["implied_vol"])] if not curve.empty else curve
    return ImpliedVolTermStructure(
        ticker=ticker,
        spot=float(spot),
        as_of=pd.Timestamp(as_of),
        curve=curve,
        diagnostics={
            "n_expiries_tried": len(curve),
            "n_expiries_usable": len(usable),
        },
    )


def implied_annual_vol_for_horizon(
    curve: pd.DataFrame, horizon_days: int, trading_days_per_year: int = 252
) -> tuple[float, dict]:
    """Annualized implied vol for ``horizon_days``, interpolated from the term structure.

    Interpolates linearly in total variance (``sigma^2 * T``) between the two usable
    expiries bracketing the target horizon -- additive under the independent-increments
    assumption a diffusion makes, unlike volatility itself. A horizon outside the usable
    range holds flat at the nearer endpoint rather than extrapolating a slope that was
    never actually quoted.

    Returns ``(nan, diagnostics)`` if fewer than one expiry produced a usable vol.
    """
    usable = curve[np.isfinite(curve.get("implied_vol", pd.Series(dtype=float)))].sort_values(
        "time_to_expiry"
    )
    diagnostics: dict = {"n_usable_expiries": int(len(usable))}
    if usable.empty:
        return float("nan"), diagnostics

    target_t = horizon_days / trading_days_per_year
    times = usable["time_to_expiry"].to_numpy(dtype=float)
    vols = usable["implied_vol"].to_numpy(dtype=float)
    diagnostics["target_time_to_expiry_years"] = float(target_t)

    if len(usable) == 1 or target_t <= times[0]:
        diagnostics["method"] = "flat_at_nearest_expiry"
        diagnostics["expiry_used"] = str(usable["expiry"].iloc[0].date())
        return float(vols[0]), diagnostics
    if target_t >= times[-1]:
        diagnostics["method"] = "flat_at_nearest_expiry"
        diagnostics["expiry_used"] = str(usable["expiry"].iloc[-1].date())
        return float(vols[-1]), diagnostics

    upper_index = int(np.searchsorted(times, target_t, side="left"))
    lower_index = upper_index - 1
    t_lo, t_hi = times[lower_index], times[upper_index]
    var_lo, var_hi = vols[lower_index] ** 2 * t_lo, vols[upper_index] ** 2 * t_hi
    weight = (target_t - t_lo) / (t_hi - t_lo)
    interpolated_variance = var_lo + weight * (var_hi - var_lo)
    annual_vol = float(np.sqrt(max(interpolated_variance, 0.0) / target_t))

    diagnostics["method"] = "variance_interpolation"
    diagnostics["bracketing_expiries"] = [
        str(usable["expiry"].iloc[lower_index].date()),
        str(usable["expiry"].iloc[upper_index].date()),
    ]
    return annual_vol, diagnostics


def fetch_raw_option_chains(
    ticker: str, max_expiries: int = 6
) -> tuple[float, pd.Timestamp, dict[str, dict[str, pd.DataFrame]]]:
    """The only network call in this module: current spot price and the nearest
    ``max_expiries`` listed option chains from yfinance.

    Isolated here, and nowhere else, so every other function in this module is a plain,
    fixture-testable function of already-fetched data -- the same fetch/compute split the
    project already uses for the earnings calendar and the industry-cycle series.
    """
    import yfinance as yf

    handle = yf.Ticker(ticker)
    fast_info = getattr(handle, "fast_info", None)
    spot = None
    if fast_info is not None:
        spot = fast_info.get("lastPrice") or fast_info.get("last_price")
    if spot is None or not np.isfinite(spot) or spot <= 0:
        history = handle.history(period="1d")
        if history.empty:
            raise OptionsUnavailableError(f"{ticker}: no live spot price available")
        spot = float(history["Close"].iloc[-1])

    expiry_strings = list(handle.options)
    if not expiry_strings:
        raise OptionsUnavailableError(f"{ticker}: no listed option expiries")

    expiries: dict[str, dict[str, pd.DataFrame]] = {}
    for expiry_str in expiry_strings[:max_expiries]:
        try:
            chain = handle.option_chain(expiry_str)
        except Exception as exc:  # noqa: BLE001 - a single bad expiry must not fail the rest
            LOGGER.warning("%s: could not fetch the %s option chain: %s", ticker, expiry_str, exc)
            continue
        expiries[expiry_str] = {"calls": chain.calls, "puts": chain.puts}

    if not expiries:
        raise OptionsUnavailableError(f"{ticker}: every option chain fetch failed")
    return float(spot), pd.Timestamp.now().normalize(), expiries


def fetch_implied_vol_term_structure(
    ticker: str,
    rate: float,
    dividend_yield: float = 0.0,
    moneyness_band: float = 0.10,
    max_expiries: int = 6,
    min_quotes: int = 2,
) -> ImpliedVolTermStructure:
    """Fetch today's option chain and build the implied-vol term structure from it.

    The one function most callers want: network fetch plus the pure computation above,
    combined. Raises :class:`OptionsUnavailableError` if no spot price or option chain
    could be fetched at all; returns a term structure with `NaN` vols (never raises) when
    quotes were fetched but none were liquid enough to invert.
    """
    spot, as_of, expiries = fetch_raw_option_chains(ticker, max_expiries)
    return implied_vol_term_structure(
        ticker,
        spot,
        as_of,
        expiries,
        rate,
        dividend_yield,
        moneyness_band,
        max_expiries,
        min_quotes,
    )

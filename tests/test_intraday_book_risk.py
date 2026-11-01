"""`intraday/book_risk.py`: the book's net SOXX exposure, its simulated tail, and each name's
z-score. Tolerances are Monte Carlo error at the test's path count, and closed forms are
written out in the test."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from semimc.config import IntradayConfig
from semimc.intraday.book_risk import (
    RiskModel,
    build_risk_model,
    correlation_with_book,
    ensure_risk_model,
    measure_book,
    net_beta_usd,
    session_fraction_left,
    simulate_book,
    summarize_book_risk,
    trader_section,
    z_scores,
)
from semimc.intraday.ledger import IntradayLedger

AS_OF = pd.Timestamp("2026-09-21")
CONFIG = IntradayConfig()
# A session's variance: SOXX 4% a day, NVDA 1.5 times it plus 2% of its own.
SOXX_VAR = 0.04**2
OWN_VAR = 0.02**2
COVARIANCE = np.array(
    [
        [1.5**2 * SOXX_VAR + OWN_VAR, 1.5 * SOXX_VAR],
        [1.5 * SOXX_VAR, SOXX_VAR],
    ]
)
MODEL = RiskModel(
    symbols=("NVDA", "SOXX"),
    betas={"NVDA": 1.5, "SOXX": 1.0},
    covariance=COVARIANCE,
    spreads={"10:00": {"NVDA": 0.01, "SOXX": 0.0}, "12:00": {"NVDA": 0.02, "SOXX": 0.0}},
    sessions=60,
    first_day="2026-06-01",
    last_day="2026-09-18",
)


def _sessions(count: int, end: pd.Timestamp = AS_OF) -> list[pd.Timestamp]:
    return list(pd.bdate_range(end=end - pd.Timedelta(days=1), periods=count))


def _bars(days, rng, *, beta=1.5, bars_per_day=13):
    """30-minute bars for NVDA and SOXX. NVDA's return is exactly `beta` times SOXX's, plus
    noise of its own."""
    out = {"NVDA": [], "SOXX": []}
    for day in days:
        opened = pd.Timestamp(day).tz_localize("America/New_York") + pd.Timedelta(hours=9.5)
        soxx, nvda = 100.0, 100.0
        for bar in range(bars_per_day):
            at = opened + pd.Timedelta(minutes=30 * bar)
            shock = float(rng.normal(0, 0.005))
            own = float(rng.normal(0, 0.002))
            soxx_next = soxx * np.exp(shock)
            nvda_next = nvda * np.exp(beta * shock + own)
            utc = at.tz_convert("UTC").to_pydatetime()
            out["SOXX"].append(SimpleNamespace(timestamp=utc, open=soxx, close=soxx_next))
            out["NVDA"].append(SimpleNamespace(timestamp=utc, open=nvda, close=nvda_next))
            soxx, nvda = soxx_next, nvda_next
    return out


class _Market:
    def __init__(self, bars):
        self.bars_by_symbol = bars
        self.calls = []

    def bars(self, symbols, start, end, minutes=5):
        self.calls.append({"symbols": symbols, "start": start, "end": end, "minutes": minutes})
        return self.bars_by_symbol


def _model(days=60, **kwargs) -> RiskModel:
    bars = _bars(_sessions(days), np.random.default_rng(1), **kwargs)
    return build_risk_model(_Market(bars), CONFIG, ["NVDA"], AS_OF)


def test_the_model_recovers_a_known_beta_and_scales_variance_to_a_session():
    bars = _bars(_sessions(60), np.random.default_rng(1))

    model = build_risk_model(_Market(bars), CONFIG, ["NVDA"], AS_OF)

    # 780 bars. The noise is 0.4 of the shock, so beta's standard error is about 0.4/sqrt(780).
    assert model.betas["NVDA"] == pytest.approx(1.5, abs=4 * 0.4 / np.sqrt(780))
    assert model.betas["SOXX"] == pytest.approx(1.0)
    index = model.symbols.index("SOXX")
    # 13 bars of 0.5%: variance 13 * 0.005^2, to within the sample's 2.5 std errors.
    assert model.covariance[index, index] == pytest.approx(
        13 * 0.005**2, rel=2.5 * np.sqrt(2 / 780)
    )
    assert model.sessions == 60


def test_the_model_asks_for_nothing_from_today_and_ignores_it_if_sent():
    days = _sessions(60)
    today = _bars([AS_OF], np.random.default_rng(2))
    bars = _bars(days, np.random.default_rng(1))
    with_today = {s: bars[s] + today[s] for s in bars}
    market = _Market(with_today)

    model = build_risk_model(market, CONFIG, ["NVDA"], AS_OF)

    midnight = AS_OF.tz_localize("America/New_York").tz_convert("UTC").to_pydatetime()
    assert market.calls[0]["end"] == midnight
    assert market.calls[0]["minutes"] == 30
    assert model.last_day == str(days[-1].date())
    assert model.last_day < str(AS_OF.date())


def test_the_model_takes_only_the_lookback():
    assert _model(days=80).sessions == CONFIG.book_risk.lookback_sessions


def test_a_half_day_is_left_out():
    days = _sessions(60)
    rng = np.random.default_rng(1)
    bars = _bars(days[:-1], rng)
    half = _bars(days[-1:], rng, bars_per_day=7)
    merged = {s: bars[s] + half[s] for s in bars}

    model = build_risk_model(_Market(merged), CONFIG, ["NVDA"], AS_OF)

    assert model.sessions == 59
    assert model.last_day == str(days[-2].date())


def test_too_few_sessions_is_no_model():
    assert _model(days=CONFIG.book_risk.min_sessions - 1) is None


def test_no_bars_is_no_model():
    assert build_risk_model(_Market({}), CONFIG, ["NVDA"], AS_OF) is None


def test_the_day_is_built_once_and_survives_a_round_trip_through_the_ledger(tmp_path):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    market = _Market(_bars(_sessions(60), np.random.default_rng(1)))

    first = ensure_risk_model(ledger, market, CONFIG, ["NVDA"], AS_OF)
    second = ensure_risk_model(ledger, market, CONFIG, ["NVDA"], AS_OF)

    assert len(market.calls) == 1
    assert second.betas == first.betas
    assert second.spreads == first.spreads
    np.testing.assert_allclose(second.covariance, first.covariance)
    ledger.close()


def _session(open_price, last, start="10:25"):
    return {"open": open_price, "last": last, "last_bar_start_et": start}


def test_a_z_score_is_the_move_net_of_beta_over_the_usual_spread_at_that_time():
    session = {
        "SOXX": _session(100.0, 100.0 * np.exp(0.02), start="11:55"),
        "NVDA": _session(50.0, 50.0 * np.exp(0.05), start="11:55"),
    }

    scores = z_scores(MODEL, session, "SOXX")

    # NVDA moved 5%; 1.5 times SOXX's 2% explains 3%; 2% is left, on a usual spread of 2%.
    assert scores == {"NVDA": pytest.approx(1.0)}


def test_a_z_score_uses_the_last_completed_half_hour():
    session = {
        "SOXX": _session(100.0, 100.0, start="10:10"),
        "NVDA": _session(50.0, 50.0 * np.exp(0.02), start="10:10"),
    }

    # The bar ended 10:15, so the 10:00 spread applies: 2% over 1%.
    assert z_scores(MODEL, session, "SOXX")["NVDA"] == pytest.approx(2.0)


def test_no_z_score_before_the_first_half_hour_is_done():
    session = {
        "SOXX": _session(100.0, 101.0, start="09:50"),
        "NVDA": _session(50.0, 51.0, start="09:50"),
    }

    assert z_scores(MODEL, session, "SOXX") == {}


def test_no_z_score_without_the_sectors_bars():
    assert z_scores(MODEL, {"NVDA": _session(50.0, 51.0)}, "SOXX") == {}


def test_net_exposure_weights_by_beta_and_counts_an_unknown_symbol_at_one():
    positions = {"NVDA": 10_000.0, "MU": -4_000.0}

    assert net_beta_usd(positions, {"NVDA": 1.5}) == pytest.approx(15_000.0 - 4_000.0)


def _simulated(positions, fraction=1.0, n_paths=400_000, seed=3):
    return simulate_book(positions, MODEL, fraction, n_paths, 8.0, np.random.default_rng(seed))


def test_the_simulated_variance_matches_the_covariance():
    pnl = _simulated({"NVDA": 10_000.0})

    # Small returns, so dollars are about notional * log return. Kurtosis of t(8) is 4.5, so
    # the sample variance's relative error is sqrt((k - 1) / n).
    expected = 10_000.0**2 * COVARIANCE[0, 0]
    assert pnl.var() == pytest.approx(expected, rel=0.03)


def test_variance_scales_with_the_share_of_the_session_left():
    whole = _simulated({"NVDA": 10_000.0})
    half = _simulated({"NVDA": 10_000.0}, fraction=0.5)

    assert half.var() / whole.var() == pytest.approx(0.5, rel=0.03)


def test_the_copula_keeps_the_correlation_so_a_hedge_shows_up():
    """Long NVDA against its beta in SOXX leaves only NVDA's own 2% a session. Independent
    draws would add the sector's variance back."""
    hedged = _simulated({"NVDA": 10_000.0, "SOXX": -15_000.0})

    # Rank correlation of a Gaussian copula with t(8) marginals is close to but under the
    # linear one, so this is a band and not an equality.
    assert hedged.var() == pytest.approx(10_000.0**2 * OWN_VAR, rel=0.25)
    assert hedged.var() < 0.2 * _simulated({"NVDA": 10_000.0}).var()


def test_a_long_book_and_a_short_book_mirror_each_other():
    long_book = _simulated({"NVDA": 10_000.0})
    short_book = _simulated({"NVDA": -10_000.0})

    # Not exactly: expm1 isn't odd. To first order they're the same draw, negated.
    assert np.corrcoef(long_book, short_book)[0, 1] < -0.999


def test_the_simulation_is_reproducible_from_its_seed_and_empty_book_is_zero():
    np.testing.assert_array_equal(
        _simulated({"NVDA": 1_000.0}, n_paths=1_000), _simulated({"NVDA": 1_000.0}, n_paths=1_000)
    )
    assert not _simulated({}, n_paths=10).any()
    assert not _simulated({"NVDA": 1_000.0}, fraction=0.0, n_paths=10).any()


def _measured(positions, equity=100_000.0, opened=100_000.0, fraction=1.0, label="book"):
    return measure_book(positions, equity, opened, MODEL, fraction, CONFIG, AS_OF, "cycle", label)


def test_the_book_is_measured_in_shares_of_equity_and_its_tail_is_a_loss():
    measured = _measured({"NVDA": 10_000.0})

    assert measured["net_beta_pct"] == pytest.approx(0.15)
    assert measured["gross_pct"] == pytest.approx(0.10)
    assert measured["tail_loss_usd"] < 0
    # The whole session's std is sqrt(1.5^2 * 0.04^2 + 0.02^2) = 6.3%, or $632.
    assert measured["tail_loss_usd"] > -2 * 632


def test_the_sector_share_is_the_part_of_the_variance_its_net_exposure_explains():
    measured = _measured({"NVDA": 10_000.0})

    # (1.5 * 0.04)^2 of (1.5 * 0.04)^2 + 0.02^2 = 0.0036 / 0.0040.
    assert measured["sector_share"] == pytest.approx(0.9)


def test_a_day_already_near_the_soft_limit_raises_its_probability():
    book = {"NVDA": 30_000.0}
    calm = _measured(book)
    down = _measured(book, equity=98_500.0)

    assert down["soft_limit_probability"] > calm["soft_limit_probability"]


def test_a_position_outside_the_model_still_counts_toward_net_exposure():
    measured = _measured({"NVDA": 10_000.0, "SMH": 5_000.0})

    assert measured["not_modeled"] == ["SMH"]
    assert measured["net_beta_pct"] == pytest.approx(0.20)


def test_an_empty_book_measures_as_nothing():
    measured = _measured({})

    assert measured["tail_loss_usd"] == 0.0
    assert measured["sector_share"] is None
    assert measured["net_beta_pct"] == 0.0


def test_each_measurement_has_its_own_seed_and_can_be_replayed():
    first = _measured({"NVDA": 10_000.0}, label="book")
    again = _measured({"NVDA": 10_000.0}, label="book")
    after = _measured({"NVDA": 10_000.0}, label="after open NVDA")

    assert first == again
    assert first["seed"] != after["seed"]


def test_correlation_with_the_book_is_the_covariance_scaled():
    out = correlation_with_book({"NVDA": 10_000.0}, MODEL, ["SOXX", "NVDA", "MU"])

    expected = 1.5 * SOXX_VAR / np.sqrt(COVARIANCE[0, 0] * SOXX_VAR)
    assert out["SOXX"] == pytest.approx(expected)
    assert out["NVDA"] == pytest.approx(1.0)
    assert "MU" not in out


def test_a_short_book_reverses_the_correlation():
    assert correlation_with_book({"NVDA": -10_000.0}, MODEL, ["NVDA"])["NVDA"] == pytest.approx(
        -1.0
    )


def test_an_empty_book_has_no_correlation_to_show():
    assert correlation_with_book({}, MODEL, ["NVDA"]) == {}


def test_the_trader_sees_only_the_names_in_view():
    session = {
        "SOXX": _session(100.0, 100.0, start="11:55"),
        "NVDA": _session(50.0, 51.0, start="11:55"),
    }
    measured = _measured({"NVDA": 10_000.0})

    section = trader_section(measured, MODEL, session, ["NVDA"], CONFIG)

    assert set(section["betas"]) == {"NVDA"}
    assert set(section["z_scores"]) == {"NVDA"}
    assert section["net_beta_cap_pct"] == CONFIG.risk.net_beta_cap_pct
    assert "positions" not in section


def test_session_fraction_left_counts_from_the_broker_s_close():
    close = pd.Timestamp("2026-09-21 16:00", tz="America/New_York")

    assert session_fraction_left(dt.datetime(2026, 9, 21, 19, 45, tzinfo=dt.UTC), close) == (
        pytest.approx(15 / 390)
    )
    assert session_fraction_left(dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.UTC), close) == 1.0
    assert session_fraction_left(dt.datetime(2026, 9, 21, 21, 0, tzinfo=dt.UTC), close) == 0.0


def test_the_days_summary_reads_only_the_cycles_own_rows():
    def row(label, net, tail, probability, share, z=None, positions=None):
        return {
            "label": label,
            "payload": {
                "net_beta_pct": net,
                "net_beta_cap_pct": 0.40,
                "tail_probability": 0.05,
                "tail_loss_usd": tail,
                "soft_limit_probability": probability,
                "sector_share": share,
                "z_scores": z or {},
                "positions": positions or {},
            },
        }

    summary = summarize_book_risk(
        [
            row("book", 0.10, -100.0, 0.01, 0.5, {"NVDA": 1.0, "MU": 4.0}, {"NVDA": 1.0}),
            row("book", -0.20, -300.0, 0.02, None, {"NVDA": -2.5}, {"NVDA": 1.0}),
            row("after open MU", 0.90, -900.0, 0.50, 0.99),
        ]
    )

    assert summary["cycles"] == 2
    assert summary["peak_net_beta_pct"] == -0.20
    assert summary["largest_tail_loss_usd"] == -300.0
    assert summary["highest_soft_limit_probability"] == 0.02
    assert summary["highest_sector_share"] == 0.5
    assert summary["largest_held_z"] == ("NVDA", -2.5)  # MU's 4.0 wasn't held


def test_a_day_with_nothing_measured_has_no_summary():
    assert summarize_book_risk([]) is None

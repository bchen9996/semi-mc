"""`TradeProposal` schema and `validate_proposal` (S4, `BRIEF_INTRADAY.md`, Phase 1)."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from pydantic import ValidationError

from semimc.config import IntradayConfig
from semimc.intraday.ledger import IntradayLedger
from semimc.intraday.proposals import TradeProposal, ValidationContext, validate_proposal

AS_OF = pd.Timestamp("2026-09-21")
NOW = dt.datetime(2026, 9, 21, 14, 30, tzinfo=dt.UTC)


def _open(**overrides) -> TradeProposal:
    payload = {
        "action": "open",
        "symbol": "NVDA",
        "side": "buy",
        "size_pct": 0.05,
        "limit_price": 190.0,
        "thesis": "AI capex tailwind into earnings",
        "invalidation_price": 185.0,
        "time_stop_minutes": 60,
        "expected_edge_bps": 100.0,
    }
    payload.update(overrides)
    return TradeProposal(**payload)


def _ledger(tmp_path) -> IntradayLedger:
    return IntradayLedger(tmp_path / "intraday.sqlite3")


def _context(tmp_path, ledger=None, **overrides) -> ValidationContext:
    defaults = dict(
        as_of=AS_OF,
        now=NOW,
        config=IntradayConfig(),
        ledger=ledger if ledger is not None else _ledger(tmp_path),
        equity=100_000.0,
        equity_at_session_open=100_000.0,
        positions={},
        prices={"NVDA": 190.0},
        turnover_so_far_usd=0.0,
        last_stopout_at={},
    )
    defaults.update(overrides)
    return ValidationContext(**defaults)


# -- schema -----------------------------------------------------------------------------


def test_open_requires_thesis_invalidation_and_a_time_stop():
    with pytest.raises(ValidationError):
        TradeProposal(action="open", symbol="NVDA")


def test_open_with_every_required_field_is_valid():
    proposal = _open()
    assert proposal.action == "open"


def test_hold_and_no_trade_need_none_of_the_open_fields():
    TradeProposal(action="hold", symbol="NVDA")
    TradeProposal(action="no_trade", symbol="NVDA")


def test_close_and_trim_do_not_require_thesis_or_invalidation():
    TradeProposal(action="close", symbol="NVDA")
    TradeProposal(action="trim", symbol="NVDA", size_pct=0.02)


def test_trim_requires_size_pct():
    """Phase 4 (S4, BRIEF_INTRADAY.md): a trim without a size can't be turned into an
    order, so the schema now requires it -- close needs no fields from the model at all,
    its qty/side come from the real position instead."""
    with pytest.raises(ValidationError, match="size_pct"):
        TradeProposal(action="trim", symbol="NVDA")


def test_thesis_over_300_characters_is_rejected():
    with pytest.raises(ValidationError):
        _open(thesis="x" * 301)


def test_confidence_outside_zero_to_one_is_rejected():
    with pytest.raises(ValidationError):
        _open(confidence=1.5)


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        _open(not_a_real_field=1)


# -- validator: edge vs. cost -------------------------------------------------------------


def test_edge_below_the_minimum_multiple_is_rejected(tmp_path):
    config = IntradayConfig(risk={"min_edge_multiple": 2.0, "equity_cost_bps_per_side": 8.0})
    proposal = _open(expected_edge_bps=10.0)  # round-trip cost is 16bps; needs >= 32bps
    failures = validate_proposal(proposal, _context(tmp_path, config=config))
    assert any("expected_edge_bps" in f for f in failures)


def test_edge_at_or_above_the_minimum_multiple_passes(tmp_path):
    config = IntradayConfig(risk={"min_edge_multiple": 2.0, "equity_cost_bps_per_side": 8.0})
    proposal = _open(expected_edge_bps=32.0)
    failures = validate_proposal(proposal, _context(tmp_path, config=config))
    assert failures == []


# -- validator: daily budgets -------------------------------------------------------------


def test_new_positions_per_day_budget_is_enforced(tmp_path):
    config = IntradayConfig(risk={"new_positions_per_day": 1})
    ledger = _ledger(tmp_path)
    ledger.record_proposal(AS_OF, "cycle_1", "AMD", "open", "accepted", payload={})
    failures = validate_proposal(_open(), _context(tmp_path, ledger=ledger, config=config))
    assert any("new_positions_per_day" in f for f in failures)
    ledger.close()


def test_round_trips_per_symbol_per_day_is_enforced(tmp_path):
    config = IntradayConfig(risk={"round_trips_per_symbol_per_day": 1})
    ledger = _ledger(tmp_path)
    ledger.record_proposal(AS_OF, "cycle_1", "NVDA", "open", "accepted", payload={})
    failures = validate_proposal(_open(), _context(tmp_path, ledger=ledger, config=config))
    assert any("round_trips_per_symbol_per_day" in f for f in failures)
    ledger.close()


def test_rejected_proposals_do_not_count_against_the_budget(tmp_path):
    config = IntradayConfig(risk={"new_positions_per_day": 1})
    ledger = _ledger(tmp_path)
    ledger.record_proposal(
        AS_OF, "cycle_1", "AMD", "open", "rejected", payload={}, reason="edge too low"
    )
    failures = validate_proposal(_open(), _context(tmp_path, ledger=ledger, config=config))
    assert not any("new_positions_per_day" in f for f in failures)
    ledger.close()


# -- validator: per-trade risk and concentration -------------------------------------------


def test_risk_to_invalidation_beyond_the_cap_is_rejected(tmp_path):
    config = IntradayConfig(risk={"risk_per_trade_pct": 0.001})
    # size_pct=0.05, distance = (190-185)/190 = 2.6% -> risk = 0.05 * 0.026 = 0.0013 > 0.001
    failures = validate_proposal(_open(), _context(tmp_path, config=config))
    assert any("risk to invalidation" in f for f in failures)


def test_single_name_cap_is_enforced(tmp_path):
    config = IntradayConfig(risk={"single_name_cap_pct": 0.04})
    # size_pct=0.05 alone already exceeds a 4% single-name cap
    failures = validate_proposal(_open(), _context(tmp_path, config=config))
    assert any("single_name_cap_pct" in f for f in failures)


def test_gross_cap_is_enforced(tmp_path):
    config = IntradayConfig(risk={"gross_cap_pct": 0.5})
    context = _context(tmp_path, config=config, positions={"AMD": 48_000.0})
    failures = validate_proposal(_open(), context)
    assert any("gross_cap_pct" in f for f in failures)


def test_net_long_cap_is_enforced(tmp_path):
    config = IntradayConfig(risk={"net_long_cap_pct": 0.5})
    context = _context(tmp_path, config=config, positions={"AMD": 48_000.0})
    failures = validate_proposal(_open(), context)
    assert any("net_long_cap_pct" in f for f in failures)


def test_a_short_proposal_reduces_net_exposure_correctly(tmp_path):
    """Selling short should not be double-counted as adding to net long exposure."""
    config = IntradayConfig(risk={"net_long_cap_pct": 0.5})
    context = _context(tmp_path, config=config, positions={"AMD": 48_000.0})
    proposal = _open(side="sell")
    failures = validate_proposal(proposal, context)
    assert not any("net_long_cap_pct" in f for f in failures)


# -- validator: daily loss backstop -------------------------------------------------------


def test_daily_loss_soft_limit_blocks_new_opens(tmp_path):
    config = IntradayConfig(risk={"daily_loss_soft_pct": 0.01})
    context = _context(tmp_path, config=config, equity=98_000.0, equity_at_session_open=100_000.0)
    failures = validate_proposal(_open(), context)
    assert any("daily_loss_soft_pct" in f for f in failures)


def test_daily_loss_hard_limit_is_also_flagged(tmp_path):
    config = IntradayConfig(risk={"daily_loss_hard_pct": 0.01})
    context = _context(tmp_path, config=config, equity=98_000.0, equity_at_session_open=100_000.0)
    failures = validate_proposal(_open(), context)
    assert any("daily_loss_hard_pct" in f for f in failures)


def test_no_daily_loss_failure_when_equity_is_flat_or_up(tmp_path):
    context = _context(tmp_path, equity=101_000.0, equity_at_session_open=100_000.0)
    failures = validate_proposal(_open(), context)
    assert not any("daily_loss" in f for f in failures)


# -- validator: cooldown and turnover -------------------------------------------------------


def test_cooldown_after_a_stopout_blocks_a_new_open_in_that_symbol(tmp_path):
    config = IntradayConfig(risk={"cooldown_minutes_after_stopout": 30})
    context = _context(
        tmp_path,
        config=config,
        last_stopout_at={"NVDA": NOW - dt.timedelta(minutes=10)},
    )
    failures = validate_proposal(_open(), context)
    assert any("cooldown" in f for f in failures)


def test_cooldown_expires_after_the_configured_window(tmp_path):
    config = IntradayConfig(risk={"cooldown_minutes_after_stopout": 30})
    context = _context(
        tmp_path,
        config=config,
        last_stopout_at={"NVDA": NOW - dt.timedelta(minutes=45)},
    )
    failures = validate_proposal(_open(), context)
    assert not any("cooldown" in f for f in failures)


def test_daily_turnover_cap_is_enforced(tmp_path):
    config = IntradayConfig(risk={"daily_turnover_pct": 0.06})
    context = _context(tmp_path, config=config, turnover_so_far_usd=2_000.0)
    # proposed notional = 0.05 * 100_000 = 5_000; (2_000 + 5_000) / 100_000 = 0.07 > 0.06
    failures = validate_proposal(_open(), context)
    assert any("daily_turnover_pct" in f for f in failures)


# -- schema: option-only fields -----------------------------------------------------------


def _option_open(**overrides) -> TradeProposal:
    payload = {
        "instrument": "option",
        "expiry": AS_OF.date() + dt.timedelta(days=30),
        "strike": 190.0,
        "right": "call",
        "structure": "long_call",
    }
    payload.update(overrides)
    return _open(**payload)


def test_option_open_with_every_option_field_is_valid():
    proposal = _option_open()
    assert proposal.instrument == "option"


def test_option_instrument_requires_expiry_strike_right_and_structure():
    with pytest.raises(ValidationError, match="expiry"):
        _open(instrument="option")


def test_equity_instrument_must_not_set_option_only_fields():
    with pytest.raises(ValidationError, match="strike"):
        _open(strike=190.0)


# -- validator: options gate ---------------------------------------------------------------


def test_option_instrument_is_rejected_while_options_are_disabled(tmp_path):
    config = IntradayConfig()
    assert config.options.enabled is False
    assert config.options.shadow_pricing is False
    proposal = _option_open()
    failures = validate_proposal(proposal, _context(tmp_path, config=config))
    assert any("options are not enabled" in f for f in failures)


def test_option_instrument_still_gates_on_pricing_once_shadow_pricing_is_on(tmp_path):
    """`shadow_pricing=True` lets a well-formed option proposal reach the full gate
    suite (`intraday/options.py::validate_option_proposal`) instead of being rejected
    outright -- but with no quote/pricing supplied, that suite itself fails cleanly
    (`intraday/options.py`'s own test coverage), which is what should surface here."""
    options = IntradayConfig().options.model_copy(update={"shadow_pricing": True})
    config = IntradayConfig(options=options)
    proposal = _option_open()
    failures = validate_proposal(proposal, _context(tmp_path, config=config))
    assert failures == ["no options pricing available for this proposal"]


def test_option_instrument_passes_with_pricing_and_shadow_pricing_on(tmp_path):
    from semimc.intraday.options import OptionPricing, OptionQuote

    options = IntradayConfig().options.model_copy(update={"shadow_pricing": True})
    config = IntradayConfig(options=options)
    proposal = _option_open(size_pct=0.001, expected_edge_bps=None)
    context = _context(
        tmp_path,
        config=config,
        option_quote=OptionQuote(bid=4.8, ask=5.2, open_interest=500, volume=100, feed="opra"),
        option_pricing=OptionPricing(
            mid_price=5.0, implied_vol=0.20, delta=0.3, gamma=0.02, vega=0.1, theta=-0.02
        ),
        option_forecast_vol=0.40,
        spot_price=190.0,
    )
    assert validate_proposal(proposal, context) == []


def test_option_edge_vs_cost_uses_the_options_cost_model_not_the_equity_one(tmp_path):
    """An option's wide quoted spread must price into the edge-vs-cost check via
    `cost_model_spread_fraction`/`cost_model_extra_bps`, not the flat
    `equity_cost_bps_per_side` -- 100bps of expected edge clears the equity round-trip bar
    (2 * 2.0x * 8bps = 32bps) but not a 10%-of-mid-spread option's own (2 * 2.0x *
    (0.10*1e4*0.5 + 5) = 2020bps), proving the instrument-aware branch is actually wired
    to the right model, not silently falling through to the other one."""
    from semimc.intraday.options import OptionPricing, OptionQuote

    options = IntradayConfig().options.model_copy(update={"shadow_pricing": True})
    config = IntradayConfig(options=options)
    proposal = _option_open(size_pct=0.001, expected_edge_bps=100.0)
    context = _context(
        tmp_path,
        config=config,
        option_quote=OptionQuote(bid=4.75, ask=5.25, open_interest=500, volume=100, feed="opra"),
        option_pricing=OptionPricing(
            mid_price=5.0, implied_vol=0.20, delta=0.3, gamma=0.02, vega=0.1, theta=-0.02
        ),
        option_forecast_vol=0.40,
        spot_price=190.0,
    )
    failures = validate_proposal(proposal, context)
    assert any("modeled round-trip cost" in f for f in failures)


# -- validator: first-class hold/no_trade/close/trim ---------------------------------------


def test_hold_and_no_trade_always_pass(tmp_path):
    # A context that would fail every open/add check.
    config = IntradayConfig(risk={"daily_loss_hard_pct": 0.0001, "gross_cap_pct": 0.0001})
    context = _context(tmp_path, config=config, equity=1.0, equity_at_session_open=100_000.0)
    assert validate_proposal(TradeProposal(action="hold", symbol="NVDA"), context) == []
    assert validate_proposal(TradeProposal(action="no_trade", symbol="NVDA"), context) == []


def test_close_and_trim_bypass_every_risk_increasing_check(tmp_path):
    config = IntradayConfig(risk={"daily_loss_hard_pct": 0.0001, "gross_cap_pct": 0.0001})
    context = _context(tmp_path, config=config, equity=1.0, equity_at_session_open=100_000.0)
    assert validate_proposal(TradeProposal(action="close", symbol="NVDA"), context) == []
    assert (
        validate_proposal(TradeProposal(action="trim", symbol="NVDA", size_pct=0.01), context) == []
    )

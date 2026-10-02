"""`IntradayConfig` (S4, `BRIEF_INTRADAY.md`, Phase 0).

Deliberately top-level (`Config.intraday`), not nested inside `PaperConfig` -- S4's
LLM-proposed trades don't fit `Decision.weights`'s closed-form target-weight shape, and it
trades its own, separate Alpaca paper account rather than the one `paper.registered_accounts`
guards. See config.py's `IntradayConfig` docstring.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from semimc.config import PROJECT_ROOT, load_config


def test_intraday_defaults_to_disabled_with_no_registered_account():
    cfg = load_config()
    assert cfg.intraday.trading_enabled is False
    assert cfg.intraday.registered_account is None


def test_intraday_defaults_to_shadow_mode():
    """Rollout Stage 1 (Phase 11) -- safe by default, same reasoning `trading_enabled`
    defaults `False`: flipping `trading_enabled` alone must not be enough to place a real
    order, `shadow_mode` also has to be deliberately turned off."""
    cfg = load_config()
    assert cfg.intraday.shadow_mode is True
    assert cfg.intraday.shadow_starting_equity == pytest.approx(100_000.0)


def test_the_spread_gate_reads_the_delayed_consolidated_quote_by_default():
    cfg = load_config()
    assert cfg.intraday.risk.spread_feed == "delayed_sip"
    with pytest.raises(ValueError, match="spread_feed"):
        load_config(overrides=["intraday.risk.spread_feed=nasdaq"])


def test_intraday_section_matches_the_briefs_defaults_table():
    cfg = load_config()
    assert cfg.intraday.budget.max_cost_usd_per_call == pytest.approx(1.0)
    assert cfg.intraday.budget.max_cost_usd_per_day == pytest.approx(10.0)
    assert cfg.intraday.risk.new_positions_per_day == 6
    assert cfg.intraday.risk.round_trips_per_symbol_per_day == 2
    assert cfg.intraday.risk.daily_turnover_pct == pytest.approx(1.50)
    assert cfg.intraday.risk.risk_per_trade_pct == pytest.approx(0.0075)
    assert cfg.intraday.risk.single_name_cap_pct == pytest.approx(0.15)
    assert cfg.intraday.risk.gross_cap_pct == pytest.approx(1.50)
    assert cfg.intraday.risk.net_long_cap_pct == pytest.approx(1.00)
    assert cfg.intraday.risk.daily_loss_soft_pct == pytest.approx(0.02)
    assert cfg.intraday.risk.daily_loss_hard_pct == pytest.approx(0.03)
    assert cfg.intraday.risk.cooldown_minutes_after_stopout == 30
    assert cfg.intraday.risk.overnight_gross_cap_pct == pytest.approx(1.00)
    assert cfg.intraday.risk.min_edge_multiple == pytest.approx(2.0)
    assert cfg.intraday.risk.equity_cost_bps_per_side == pytest.approx(8.0)
    assert cfg.intraday.schedule.timezone == "America/Chicago"
    assert cfg.intraday.schedule.cycle_interval_minutes == 20
    assert cfg.intraday.options.enabled is False
    assert cfg.intraday.options.shadow_pricing is False
    assert cfg.intraday.options.premium_at_risk_pct_per_position == pytest.approx(0.005)
    assert cfg.intraday.options.premium_at_risk_pct_total == pytest.approx(0.02)
    assert cfg.intraday.options.min_dte == 7
    assert cfg.intraday.options.max_dte == 60
    assert cfg.intraday.options.close_at_dte == 2
    assert cfg.intraday.models.researcher == "sonnet"
    assert cfg.intraday.models.trader == "haiku"
    assert cfg.intraday.models.reviewer == "sonnet"
    assert cfg.intraday.models.max_turns == 6
    assert cfg.intraday.models.timeout_seconds == 300


def test_intraday_ledger_and_kill_switch_paths_are_separate_from_paper():
    """S4 writes far more often than s0/s3 -- a shared ledger file would reintroduce the
    git push race Phase 8 of paper trading already fixed once, at a lower frequency."""
    cfg = load_config()
    assert cfg.intraday.ledger_path == "ledger/intraday.sqlite3"
    assert cfg.intraday.kill_switch_path == "runs/intraday/KILL_SWITCH"
    assert cfg.intraday.ledger_path != cfg.paper.ledger_path
    assert cfg.intraday.kill_switch_path != cfg.paper.kill_switch_path


def test_unknown_intraday_key_is_rejected():
    """`extra='forbid'` must cover the new section the same as every other one."""
    with pytest.raises(ValidationError, match="intraday"):
        load_config(overrides=["intraday.not_a_real_field=1"])


def test_intraday_settings_are_overridable_with_set():
    cfg = load_config(
        overrides=[
            "intraday.trading_enabled=true",
            "intraday.budget.max_cost_usd_per_day=5",
        ]
    )
    assert cfg.intraday.trading_enabled is True
    assert cfg.intraday.budget.max_cost_usd_per_day == pytest.approx(5.0)


def test_paper_yaml_loads_intraday_at_rollout_stage_1():
    """`config/paper.yaml` is the shared config root for both s0/s3 and S4. Rollout Stage
    1 started 2026-09-23 (owner's deliberate action, mirroring s3's own two-step go-live --
    `test_paper_yaml_loads_and_keeps_runs_separate_from_research_runs`'s docstring):
    `trading_enabled: true` lets the real loop run for real, but `shadow_mode: true`
    (unchanged, still the safe default) means every fill still goes through `FakeBroker`,
    never Alpaca -- see `tasks/todo.md`'s "Rollout stages from the brief" entry."""
    cfg = load_config(PROJECT_ROOT / "config" / "paper.yaml")
    assert cfg.intraday.trading_enabled is True
    assert cfg.intraday.registered_account == "PA33HJGNHLI7"
    # The free plan's delayed consolidated quote for the spread gate, not IEX's own book.
    assert cfg.intraday.risk.spread_feed == "delayed_sip"
    assert cfg.intraday.options.enabled is False
    assert cfg.intraday.options.shadow_pricing is False
    assert cfg.intraday.shadow_mode is True

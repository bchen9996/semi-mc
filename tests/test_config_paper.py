"""`PaperConfig` (semis_paper_trading_brief.md, Phase 0).

Scoped to what S0/S3 need -- see the class docstring in config.py for why S1/S2 have no
schema fields yet (the addendum's Phase 4B holdout came back null; the brief's own rule is
that paper trading can't rescue a signal that failed it).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from semimc.config import PROJECT_ROOT, load_config


def test_every_sleeve_defaults_to_disabled():
    cfg = load_config()
    assert cfg.paper.sleeves["s0"].trading_enabled is False
    assert cfg.paper.sleeves["s3"].trading_enabled is False


def test_paper_section_matches_the_briefs_defaults_table():
    cfg = load_config()
    assert cfg.paper.s3_target_vol == pytest.approx(0.25)
    assert cfg.paper.s3_max_leverage == pytest.approx(1.0)
    assert cfg.paper.minimum_trade_pct == pytest.approx(0.005)
    assert cfg.paper.execution.collar_bps == pytest.approx(20.0)
    assert cfg.paper.execution.reprice_interval_minutes == 15
    assert cfg.paper.reconciliation.position_tolerance_shares == pytest.approx(1e-6)
    assert cfg.paper.reconciliation.cash_tolerance_usd == pytest.approx(1.0)
    assert cfg.paper.sector_tilt_strength == pytest.approx(0.15)
    assert cfg.paper.review_months == [3, 6, 12]
    assert cfg.paper.evaluation.n_bootstrap == 2000
    assert cfg.paper.evaluation.block_length_days == 21


def test_sector_shadow_has_no_sleeve_entry():
    """T4 (`strategy.decide.decide_sector_shadow`) is shadow-only by design -- unlike s0/s3
    it has no `trading_enabled` switch at all, so there is no way to turn it live."""
    assert "sector_shadow" not in load_config().paper.sleeves


def test_registered_accounts_is_empty_until_deliberately_added():
    """Paper-only guard: nothing is registered by default."""
    assert load_config().paper.registered_accounts == []


def test_unknown_paper_key_is_rejected():
    """`extra='forbid'` must cover the new section the same as every other one."""
    with pytest.raises(ValidationError, match="paper"):
        load_config(overrides=["paper.not_a_real_field=1"])


def test_ledger_path_and_rebalance_frequency_defaults():
    """Phase 5: the ledger's own persistent location, and each sleeve's rebalance
    cadence -- both new this phase, both with defaults that preserve prior behavior."""
    cfg = load_config()
    assert cfg.paper.ledger_path == "ledger/paper.sqlite3"
    assert cfg.paper.sleeves["s0"].rebalance_frequency == "ME"
    assert cfg.paper.sleeves["s3"].rebalance_frequency == "ME"


def test_paper_settings_are_overridable_with_set():
    cfg = load_config(
        overrides=["paper.sleeves.s0.trading_enabled=true", "paper.s3_target_vol=0.30"]
    )
    assert cfg.paper.sleeves["s0"].trading_enabled is True
    assert cfg.paper.s3_target_vol == pytest.approx(0.30)


def test_paper_yaml_loads_and_keeps_runs_separate_from_research_runs():
    """`config/paper.yaml`: a full config file, not a second schema -- see config.py's
    module docstring and CLAUDE.md's 'one place defaults live' rule."""
    cfg = load_config(PROJECT_ROOT / "config" / "paper.yaml")
    assert cfg.reporting.runs_dir == "runs/paper"
    # s0 stays off; s3 was deliberately enabled at go-live (docs/go_live_checklist.md
    # item 6) -- see tasks/todo.md's Phase 8 entry for why not both.
    assert cfg.paper.sleeves["s0"].trading_enabled is False
    assert cfg.paper.sleeves["s3"].trading_enabled is True
    assert set(cfg.universe.tickers) == set(load_config().universe.tickers)

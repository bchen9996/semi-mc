"""``live/decide.py``: freezing a sleeve's decision into the ledger, with the
rebalance-cadence skip and the differing-decision-same-day refusal that
``strategy/decide.py`` itself deliberately knows nothing about.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace

import pandas as pd
import pytest

from semimc.live.decide import git_commit, run_decide, seed_for
from semimc.live.ledger import Ledger


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    yield ledger
    ledger.close()


def test_first_decide_records_data_vintage_and_git_commit(config, market, ledger):
    result = run_decide("s0", market, config, ["NVDA", "AMD"], pd.Timestamp("2023-01-05"), ledger)
    assert result.skipped_reason is None
    assert result.decision is not None

    row = ledger.get_decision("s0", pd.Timestamp("2023-01-05"))
    # The newest close the decision could see: the trading day before as_of.
    assert row["data_vintage"] == "2023-01-04"
    # git_commit() may legitimately be None off a real checkout with no git available, but
    # this repo *is* a git checkout in CI/dev -- assert it's either a real hash or None,
    # never something that silently swallowed a different exception.
    assert row["git_commit"] is None or len(row["git_commit"]) == 40


def test_same_exact_day_rerun_is_an_idempotent_no_op(config, market, ledger):
    first = run_decide("s0", market, config, ["NVDA", "AMD"], pd.Timestamp("2023-01-05"), ledger)
    second = run_decide("s0", market, config, ["NVDA", "AMD"], pd.Timestamp("2023-01-05"), ledger)
    assert second.skipped_reason is None
    assert second.decision.snapshot_hash == first.decision.snapshot_hash


def test_a_later_day_in_the_same_rebalance_period_is_skipped(config, market, ledger):
    run_decide("s0", market, config, ["NVDA", "AMD"], pd.Timestamp("2023-01-05"), ledger)
    later = run_decide("s0", market, config, ["NVDA", "AMD"], pd.Timestamp("2023-01-20"), ledger)
    assert later.decision is None
    assert "already decided" in later.skipped_reason
    # No second row was written for the skipped date.
    assert ledger.get_decision("s0", pd.Timestamp("2023-01-20")) is None


def test_force_overrides_the_cadence_skip(config, market, ledger):
    run_decide("s0", market, config, ["NVDA", "AMD"], pd.Timestamp("2023-01-05"), ledger)
    forced = run_decide(
        "s0", market, config, ["NVDA", "AMD"], pd.Timestamp("2023-01-20"), ledger, force=True
    )
    assert forced.decision is not None
    assert ledger.get_decision("s0", pd.Timestamp("2023-01-20")) is not None


def test_a_later_rebalance_period_is_decided_normally(config, market, ledger):
    run_decide("s0", market, config, ["NVDA", "AMD"], pd.Timestamp("2023-01-05"), ledger)
    next_month = run_decide(
        "s0", market, config, ["NVDA", "AMD"], pd.Timestamp("2023-02-02"), ledger
    )
    assert next_month.skipped_reason is None
    assert next_month.decision is not None


def test_a_different_decision_on_the_exact_same_day_is_refused(config, market, ledger):
    # Plant a decision under a different set of weights (a different tickers set changes
    # the snapshot_hash) for the exact same (sleeve, as_of) run_decide will compute for.
    from semimc.strategy.decide import decide_s0

    planted = decide_s0(["NVDA"], pd.Timestamp("2023-01-05"))
    ledger.record_decision(planted)

    with pytest.raises(ValueError, match="different decision already exists"):
        run_decide("s0", market, config, ["NVDA", "AMD"], pd.Timestamp("2023-01-05"), ledger)


def test_force_overrides_the_differing_decision_refusal(config, market, ledger):
    from semimc.strategy.decide import decide_s0

    planted = decide_s0(["NVDA"], pd.Timestamp("2023-01-05"))
    ledger.record_decision(planted)

    result = run_decide(
        "s0", market, config, ["NVDA", "AMD"], pd.Timestamp("2023-01-05"), ledger, force=True
    )
    assert result.decision is not None
    assert result.decision.snapshot_hash != planted.snapshot_hash


def test_a_decision_ignores_its_own_days_bar(config, market, ledger):
    """2026-10-01: decide ran at 10:05 New York on a partial bar for that day, and a late
    rerun that afternoon saw a different partial bar and refused to record. s3 sizes off
    its volatility forecast, so a wild as_of row would move its weights if it were read."""
    tickers = ["NVDA", "AMD"]
    as_of = pd.Timestamp("2023-01-05")
    first = run_decide("s3", market, config, tickers, as_of, ledger)

    wild = market.prices.copy()
    wild.loc[as_of, tickers] = wild.loc[as_of, tickers] * 1.5  # a different "partial bar"
    rerun = run_decide("s3", replace(market, prices=wild), config, tickers, as_of, ledger)

    assert rerun.decision.snapshot_hash == first.decision.snapshot_hash


def test_market_data_before_drops_the_day_and_everything_after(market):
    cut = market.before(pd.Timestamp("2023-01-05"))
    assert cut.last_date == pd.Timestamp("2023-01-04")
    assert cut.log_returns.index.max() == pd.Timestamp("2023-01-04")
    assert all(frame.index.max() <= pd.Timestamp("2023-01-04") for frame in cut.ohlc.values())


def test_git_commit_returns_none_outside_a_git_checkout(tmp_path, monkeypatch):
    def _raise(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert git_commit() is None


def test_seed_for_is_deterministic_per_day_but_differs_across_days():
    same_day_first = seed_for(42, pd.Timestamp("2023-01-05")).random()
    same_day_second = seed_for(42, pd.Timestamp("2023-01-05")).random()
    different_day = seed_for(42, pd.Timestamp("2023-02-02")).random()
    assert same_day_first == same_day_second
    assert same_day_first != different_day

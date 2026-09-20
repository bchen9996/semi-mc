"""`intraday.budget` (S4, `BRIEF_INTRADAY.md`, Phase 0): the daily hard-stop check every
future LLM-calling job must run before making a call."""

from __future__ import annotations

import pandas as pd

from semimc.config import IntradayConfig
from semimc.intraday.budget import ModelCallCost, check_budget, record_call_cost, spent_today
from semimc.intraday.ledger import IntradayLedger

AS_OF = pd.Timestamp("2026-09-21")


def _cost(cost_usd: float, role: str = "trader") -> ModelCallCost:
    return ModelCallCost(
        role=role,
        model="claude-haiku-4-5",
        prompt_hash="abc123",
        input_tokens=2000,
        output_tokens=500,
        cost_usd=cost_usd,
    )


def test_spent_today_sums_only_todays_calls(tmp_path):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    record_call_cost(ledger, AS_OF, "cycle_1", _cost(0.01))
    record_call_cost(ledger, AS_OF, "cycle_2", _cost(0.02))
    record_call_cost(ledger, pd.Timestamp("2026-09-20"), "cycle_1", _cost(5.00))  # other day
    assert spent_today(ledger, AS_OF) == 0.01 + 0.02
    ledger.close()


def test_spent_today_is_zero_before_any_call(tmp_path):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    assert spent_today(ledger, AS_OF) == 0.0
    ledger.close()


def test_check_budget_allows_calls_under_the_cap(tmp_path):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    config = IntradayConfig(budget={"max_cost_usd_per_day": 10.0})
    record_call_cost(ledger, AS_OF, "cycle_1", _cost(1.0))
    assert check_budget(ledger, AS_OF, config) is None
    ledger.close()


def test_check_budget_returns_a_reason_once_the_cap_is_met(tmp_path):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    config = IntradayConfig(budget={"max_cost_usd_per_day": 1.0})
    record_call_cost(ledger, AS_OF, "cycle_1", _cost(1.0))
    reason = check_budget(ledger, AS_OF, config)
    assert reason is not None
    assert "budget exhausted" in reason
    ledger.close()


def test_check_budget_blocks_beyond_the_cap_too(tmp_path):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    config = IntradayConfig(budget={"max_cost_usd_per_day": 1.0})
    record_call_cost(ledger, AS_OF, "cycle_1", _cost(0.6))
    record_call_cost(ledger, AS_OF, "cycle_2", _cost(0.6))
    assert check_budget(ledger, AS_OF, config) is not None
    ledger.close()

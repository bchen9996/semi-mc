"""``live/execute.py``: turning a sleeve's most recently frozen decision into broker
orders. Extends the offline path ``tests/test_execution_integration.py`` already proves
against ``FakeBroker``, through the actual ``run_execute`` wrapper this phase adds.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from semimc.config import PaperSleeveConfig
from semimc.execution.fake_broker import FakeBroker
from semimc.live.execute import run_execute
from semimc.live.ledger import Ledger
from semimc.strategy.decide import decide_s0

NOW = dt.datetime(2026, 1, 5, 11, 0)


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    yield ledger
    ledger.close()


def _tradeable_config(config, tmp_path, account_number="FAKE-PAPER-0001"):
    paper = config.paper.model_copy(
        update={
            "sleeves": {**config.paper.sleeves, "s0": PaperSleeveConfig(trading_enabled=True)},
            "registered_accounts": [account_number],
            "kill_switch_path": str(tmp_path / "KILL_SWITCH"),
        }
    )
    return config.model_copy(update={"paper": paper})


def _market_with_spot(prices: dict[str, float]):
    """A minimal stand-in exposing only the ``.spot`` method ``run_execute`` calls --
    ``build_orders``/``decide`` don't need a real ``MarketData`` here since the decision is
    already frozen and s0's own weights don't depend on market data at all."""

    class _Market:
        def spot(self, tickers, as_of=None):
            return pd.Series({t: prices[t] for t in tickers if t in prices})

    return _Market()


def test_missing_decision_is_a_clean_skip_not_an_error(config, tmp_path, ledger):
    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker()
    market = _market_with_spot({"NVDA": 100.0, "AMD": 50.0})

    result = run_execute(
        "s0", cfg, broker, ledger, market, ["NVDA", "AMD"], pd.Timestamp("2026-01-05"), NOW
    )
    assert result.skipped_reason is not None
    assert "no decision has ever been recorded" in result.skipped_reason
    assert result.orders == []
    assert result.submitted == []


def test_pretrade_failure_short_circuits_and_logs_an_incident(config, tmp_path, ledger):
    cfg = config  # trading_enabled defaults False, registered_accounts empty -> declines
    broker = FakeBroker()
    market = _market_with_spot({"NVDA": 100.0, "AMD": 50.0})
    decision = decide_s0(["NVDA", "AMD"], pd.Timestamp("2026-01-05"))
    ledger.record_decision(decision)

    result = run_execute(
        "s0", cfg, broker, ledger, market, ["NVDA", "AMD"], pd.Timestamp("2026-01-05"), NOW
    )
    assert result.pretrade_failures != []
    assert result.orders == []
    incidents = ledger.incidents("s0")
    assert len(incidents) == 1
    assert incidents[0]["severity"] == "warning"


def test_dry_run_makes_no_broker_or_ledger_writes(config, tmp_path, ledger):
    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker()
    market = _market_with_spot({"NVDA": 100.0, "AMD": 50.0})
    decision = decide_s0(["NVDA", "AMD"], pd.Timestamp("2026-01-05"))
    ledger.record_decision(decision)

    result = run_execute(
        "s0",
        cfg,
        broker,
        ledger,
        market,
        ["NVDA", "AMD"],
        pd.Timestamp("2026-01-05"),
        NOW,
        dry_run=True,
    )
    assert len(result.orders) == 2
    assert result.submitted == []
    assert broker.list_positions() == []
    assert ledger.recent_orders("s0") == []
    assert ledger.latest_snapshot("s0") is None


def test_full_execute_submits_orders_and_records_the_ledger(config, tmp_path, ledger):
    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker()
    market = _market_with_spot({"NVDA": 100.0, "AMD": 50.0})
    decision = decide_s0(["NVDA", "AMD"], pd.Timestamp("2026-01-05"))
    ledger.record_decision(decision)

    result = run_execute(
        "s0", cfg, broker, ledger, market, ["NVDA", "AMD"], pd.Timestamp("2026-01-05"), NOW
    )
    assert {o.symbol for o in result.orders} == {"NVDA", "AMD"}
    assert len(result.submitted) == 2
    for order in result.submitted:
        row = ledger.get_order(order.client_order_id)
        assert row["status"] == "filled"
        assert len(ledger.fills_for_order(order.client_order_id)) == 1

    snapshot = ledger.latest_snapshot("s0")
    equity = broker.get_account().equity
    assert snapshot["positions"]["NVDA"]["qty"] == pytest.approx(equity * 0.5 / 100.0)


def test_a_later_execute_against_the_same_decision_is_a_no_op(config, tmp_path, ledger):
    """Executing twice against the same still-current decision (no rebalance in between)
    must not double a position -- the second call finds nothing left to trade."""
    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker()
    market = _market_with_spot({"NVDA": 100.0, "AMD": 50.0})
    decision = decide_s0(["NVDA", "AMD"], pd.Timestamp("2026-01-05"))
    ledger.record_decision(decision)

    first = run_execute(
        "s0", cfg, broker, ledger, market, ["NVDA", "AMD"], pd.Timestamp("2026-01-05"), NOW
    )
    later_now = NOW.replace(day=6)
    second = run_execute(
        "s0", cfg, broker, ledger, market, ["NVDA", "AMD"], pd.Timestamp("2026-01-06"), later_now
    )
    assert len(first.orders) == 2
    assert second.orders == []


def test_a_tradability_failure_is_skipped_without_aborting_other_orders(config, tmp_path, ledger):
    from semimc.execution.broker import Asset

    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker(assets={"NVDA": Asset(symbol="NVDA", tradable=False)})
    market = _market_with_spot({"NVDA": 100.0, "AMD": 50.0})
    decision = decide_s0(["NVDA", "AMD"], pd.Timestamp("2026-01-05"))
    ledger.record_decision(decision)

    result = run_execute(
        "s0", cfg, broker, ledger, market, ["NVDA", "AMD"], pd.Timestamp("2026-01-05"), NOW
    )
    assert len(result.order_failures) == 1
    assert result.order_failures[0][0].symbol == "NVDA"
    assert len(result.submitted) == 1
    assert result.submitted[0].symbol == "AMD"
    incidents = ledger.incidents("s0")
    assert any("not tradable" in i["message"] for i in incidents)

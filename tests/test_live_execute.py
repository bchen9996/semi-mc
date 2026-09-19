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

    # Phase 7: a tradability failure is a real problem (a halted/delisted symbol), not a
    # routine skip like a pretrade decline -- it must be recorded at "error" severity so the
    # CLI can fail the job on it.
    incidents = ledger.incidents("s0")
    assert incidents[-1]["severity"] == "error"
    incidents = ledger.incidents("s0")
    assert any("not tradable" in i["message"] for i in incidents)


def test_a_timeout_after_a_successful_submit_recovers_by_looking_up_the_order(
    config, tmp_path, ledger
):
    """Phase 8: FakeBroker's own docstring is explicit that a timeout here means the order
    was recorded anyway -- run_execute must recover it by client_order_id, not lose it."""
    cfg = _tradeable_config(config, tmp_path)
    market = _market_with_spot({"NVDA": 100.0, "AMD": 50.0})
    decision = decide_s0(["NVDA", "AMD"], pd.Timestamp("2026-01-05"))
    ledger.record_decision(decision)

    # Discover the deterministic client_order_id for NVDA's order without duplicating
    # execution/orders.py's own id logic -- dry_run makes no ledger writes (see
    # test_dry_run_makes_no_broker_or_ledger_writes), so reusing `ledger` here is safe.
    probe = run_execute(
        "s0",
        cfg,
        FakeBroker(),
        ledger,
        market,
        ["NVDA", "AMD"],
        pd.Timestamp("2026-01-05"),
        NOW,
        dry_run=True,
    )
    nvda_id = next(o.client_order_id for o in probe.orders if o.symbol == "NVDA")

    broker = FakeBroker(timeout_after_success_ids={nvda_id})
    result = run_execute(
        "s0", cfg, broker, ledger, market, ["NVDA", "AMD"], pd.Timestamp("2026-01-05"), NOW
    )
    assert result.order_failures == []
    assert {o.symbol for o in result.submitted} == {"NVDA", "AMD"}
    assert ledger.get_order(nvda_id)["status"] == "filled"
    assert ledger.latest_snapshot("s0") is not None


def test_a_rate_limit_on_one_order_does_not_abort_the_rest_of_the_batch(config, tmp_path, ledger):
    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker(rate_limit_calls_remaining=1)  # AMD (processed first) is rejected once
    market = _market_with_spot({"NVDA": 100.0, "AMD": 50.0})
    decision = decide_s0(["NVDA", "AMD"], pd.Timestamp("2026-01-05"))
    ledger.record_decision(decision)

    result = run_execute(
        "s0", cfg, broker, ledger, market, ["NVDA", "AMD"], pd.Timestamp("2026-01-05"), NOW
    )
    assert len(result.order_failures) == 1
    assert result.order_failures[0][0].symbol == "AMD"
    assert len(result.submitted) == 1
    assert result.submitted[0].symbol == "NVDA"
    assert ledger.latest_snapshot("s0") is not None  # the batch still finishes
    incidents = ledger.incidents("s0")
    assert any(i["severity"] == "error" and "rate limited" in i["message"] for i in incidents)


def test_a_wash_trade_rejection_is_skipped_without_aborting_other_orders(config, tmp_path, ledger):
    from semimc.execution.broker import OrderRequest

    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker(wash_trade_symbols={"NVDA"}, partial_fill_fraction=0.0)
    # Seed an already-open opposite-side order in NVDA so the coming buy collides with it.
    broker.submit_order(
        OrderRequest(
            symbol="NVDA",
            side="sell",
            type="limit",
            time_in_force="day",
            client_order_id="seed-nvda-sell",
            qty=1.0,
            limit_price=100.0,
        )
    )
    market = _market_with_spot({"NVDA": 100.0, "AMD": 50.0})
    decision = decide_s0(["NVDA", "AMD"], pd.Timestamp("2026-01-05"))
    ledger.record_decision(decision)

    result = run_execute(
        "s0", cfg, broker, ledger, market, ["NVDA", "AMD"], pd.Timestamp("2026-01-05"), NOW
    )
    assert len(result.order_failures) == 1
    assert result.order_failures[0][0].symbol == "NVDA"
    assert [o.symbol for o in result.submitted] == ["AMD"]
    incidents = ledger.incidents("s0")
    assert any(
        i["severity"] == "error" and "could interact with one already open" in i["message"]
        for i in incidents
    )

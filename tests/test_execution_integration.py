"""End-to-end "execution mechanics" (Phase 3 of the paper-trading plan): a `Decision`
turned into orders, cleared through pretrade, submitted to `FakeBroker`, and recorded in
the `Ledger` -- the whole offline path a later phase's `live/execute.py` job will wrap.
No broker but `FakeBroker` is ever constructed here; no network anywhere.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from semimc.config import PaperSleeveConfig
from semimc.execution.fake_broker import FakeBroker
from semimc.execution.orders import build_orders
from semimc.execution.pretrade import PretradeContext, check_order_tradable, check_pretrade
from semimc.live.ledger import Ledger
from semimc.strategy.decide import decide_s0

NOW = dt.datetime(2026, 1, 5, 11, 0)


def _tradeable_config(config, tmp_path, account_number="FAKE-PAPER-0001"):
    paper = config.paper.model_copy(
        update={
            "sleeves": {**config.paper.sleeves, "s0": PaperSleeveConfig(trading_enabled=True)},
            "registered_accounts": [account_number],
            "kill_switch_path": str(tmp_path / "KILL_SWITCH"),
        }
    )
    return config.model_copy(update={"paper": paper})


def _execute_decision(decision, broker, cfg, ledger, prices):
    context = PretradeContext(sleeve=decision.sleeve, now=NOW, config=cfg, broker=broker)
    assert check_pretrade(context) == []

    account = broker.get_account()
    positions = {p.symbol: p for p in broker.list_positions()}
    orders = build_orders(decision, positions, prices, equity=account.equity, config=cfg)

    for request in orders:
        assert check_order_tradable(request, broker) is None
        submitted = broker.submit_order(request)
        ledger.record_order(decision.sleeve, decision.snapshot_hash, request, submitted)
        if submitted.filled_qty > 0:
            ledger.record_fill(
                request.client_order_id,
                qty=submitted.filled_qty,
                price=submitted.filled_avg_price,
                filled_at=submitted.filled_at or NOW,
            )

    account = broker.get_account()
    positions = {p.symbol: p for p in broker.list_positions()}
    ledger.record_snapshot(decision.sleeve, decision.as_of, account.equity, account.cash, positions)
    return orders


def test_decision_to_broker_to_ledger_round_trip(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker()
    prices = pd.Series({"NVDA": 100.0, "AMD": 50.0})
    decision = decide_s0(["NVDA", "AMD"], pd.Timestamp("2026-01-05"))
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.record_decision(decision)

    orders = _execute_decision(decision, broker, cfg, ledger, prices)
    assert {o.symbol for o in orders} == {"NVDA", "AMD"}

    for order in orders:
        row = ledger.get_order(order.client_order_id)
        assert row["status"] == "filled"
        assert len(ledger.fills_for_order(order.client_order_id)) == 1

    snapshot = ledger.latest_snapshot("s0")
    equity = broker.get_account().equity
    assert snapshot["positions"]["NVDA"]["qty"] == pytest.approx(equity * 0.5 / 100.0)
    assert snapshot["positions"]["AMD"]["qty"] == pytest.approx(equity * 0.5 / 50.0)
    ledger.close()


def test_re_executing_the_same_frozen_decision_places_no_new_orders(config, tmp_path):
    """The whole point of freezing a decision and deriving client_order_id from its own
    hash: rerunning today's decide/execute step after it already ran is a no-op, not a
    doubling of every position."""
    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker()
    prices = pd.Series({"NVDA": 100.0, "AMD": 50.0})
    decision = decide_s0(["NVDA", "AMD"], pd.Timestamp("2026-01-05"))
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.record_decision(decision)

    first_orders = _execute_decision(decision, broker, cfg, ledger, prices)
    second_orders = _execute_decision(decision, broker, cfg, ledger, prices)

    assert len(first_orders) == 2
    assert second_orders == []  # already at target -- nothing left to trade
    ledger.close()

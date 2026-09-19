"""`execution/pretrade.py`: the last gate before an order reaches a broker. Exercised only
against `FakeBroker` -- no network, matching this project's convention.
"""

from __future__ import annotations

import datetime as dt

import pytest

from semimc.config import PaperSleeveConfig
from semimc.execution.broker import Asset, OrderRequest
from semimc.execution.fake_broker import FakeBroker
from semimc.execution.pretrade import (
    KillSwitchActive,
    PretradeContext,
    check_kill_switch,
    check_order_tradable,
    check_pretrade,
)

IN_WINDOW = dt.datetime(2026, 1, 5, 11, 0)


def _tradeable_config(config, tmp_path, account_number="FAKE-PAPER-0001"):
    paper = config.paper.model_copy(
        update={
            "sleeves": {**config.paper.sleeves, "s0": PaperSleeveConfig(trading_enabled=True)},
            "registered_accounts": [account_number],
            "kill_switch_path": str(tmp_path / "KILL_SWITCH"),
        }
    )
    return config.model_copy(update={"paper": paper})


def test_check_kill_switch_is_silent_when_the_file_is_absent(tmp_path):
    check_kill_switch(tmp_path / "KILL_SWITCH")  # must not raise


def test_check_kill_switch_raises_when_the_file_exists(tmp_path):
    path = tmp_path / "KILL_SWITCH"
    path.write_text("stop", encoding="utf-8")
    with pytest.raises(KillSwitchActive, match="kill switch"):
        check_kill_switch(path)


def test_check_pretrade_passes_when_everything_is_clear(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker()
    context = PretradeContext(sleeve="s0", now=IN_WINDOW, config=cfg, broker=broker)
    assert check_pretrade(context) == []


def test_check_pretrade_flags_kill_switch_active(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    (tmp_path / "KILL_SWITCH").write_text("stop", encoding="utf-8")
    context = PretradeContext(sleeve="s0", now=IN_WINDOW, config=cfg, broker=FakeBroker())
    failures = check_pretrade(context)
    assert any("kill switch" in f for f in failures)


def test_check_pretrade_flags_a_disabled_sleeve(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    # Leave s3 at its config default (trading_enabled=False) instead of s0.
    context = PretradeContext(sleeve="s3", now=IN_WINDOW, config=cfg, broker=FakeBroker())
    failures = check_pretrade(context)
    assert any("not trading_enabled" in f for f in failures)


def test_check_pretrade_flags_an_unregistered_account(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, account_number="SOME-OTHER-ACCOUNT")
    context = PretradeContext(sleeve="s0", now=IN_WINDOW, config=cfg, broker=FakeBroker())
    failures = check_pretrade(context)
    assert any("registered_accounts" in f for f in failures)


def test_check_pretrade_flags_a_blocked_account(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker(account_status="ACCOUNT_BLOCKED")
    context = PretradeContext(sleeve="s0", now=IN_WINDOW, config=cfg, broker=broker)
    failures = check_pretrade(context)
    assert any("ACCOUNT_BLOCKED" in f for f in failures)


def test_check_pretrade_flags_a_closed_market(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker(market_open=False)
    context = PretradeContext(sleeve="s0", now=IN_WINDOW, config=cfg, broker=broker)
    failures = check_pretrade(context)
    assert any("market is closed" in f for f in failures)


def test_check_pretrade_flags_outside_the_execution_window(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    too_early = dt.datetime(2026, 1, 5, 8, 0)
    context = PretradeContext(sleeve="s0", now=too_early, config=cfg, broker=FakeBroker())
    failures = check_pretrade(context)
    assert any("execution window" in f for f in failures)


def test_check_pretrade_reports_every_failure_at_once(config, tmp_path):
    """A closed market and a disabled sleeve found together -- both must be reported, not
    just whichever check happens to run first."""
    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker(market_open=False)
    context = PretradeContext(sleeve="s3", now=IN_WINDOW, config=cfg, broker=broker)
    failures = check_pretrade(context)
    assert any("not trading_enabled" in f for f in failures)
    assert any("market is closed" in f for f in failures)


def test_check_order_tradable_passes_for_a_tradable_asset():
    broker = FakeBroker()
    order = OrderRequest(
        symbol="NVDA",
        side="buy",
        type="limit",
        time_in_force="day",
        client_order_id="x",
        qty=1.0,
        limit_price=100.0,
    )
    assert check_order_tradable(order, broker) is None


def test_check_order_tradable_flags_a_non_tradable_asset():
    broker = FakeBroker(assets={"NVDA": Asset(symbol="NVDA", tradable=False)})
    order = OrderRequest(
        symbol="NVDA",
        side="buy",
        type="limit",
        time_in_force="day",
        client_order_id="x",
        qty=1.0,
        limit_price=100.0,
    )
    assert "not tradable" in check_order_tradable(order, broker)

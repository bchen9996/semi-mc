"""`intraday/pretrade.py`: S4's own sleeve-level gate (S4, `BRIEF_INTRADAY.md`, Phase 3).
Exercised only against `FakeBroker` -- no network, matching this project's convention.
"""

from __future__ import annotations

import datetime as dt

from semimc.execution.fake_broker import FakeBroker
from semimc.intraday.pretrade import IntradayPretradeContext, check_intraday_pretrade

NOW = dt.datetime(2026, 1, 5, 11, 0)
ACCOUNT = "FAKE-PAPER-0001"


def _tradeable_config(config, tmp_path, account_number=ACCOUNT):
    intraday = config.intraday.model_copy(
        update={
            "trading_enabled": True,
            "registered_account": account_number,
            "kill_switch_path": str(tmp_path / "KILL_SWITCH"),
        }
    )
    return config.model_copy(update={"intraday": intraday})


def test_check_intraday_pretrade_passes_when_everything_is_clear(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    context = IntradayPretradeContext(now=NOW, config=cfg, broker=FakeBroker())
    assert check_intraday_pretrade(context) == []


def test_check_intraday_pretrade_flags_kill_switch_active(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    (tmp_path / "KILL_SWITCH").write_text("stop", encoding="utf-8")
    context = IntradayPretradeContext(now=NOW, config=cfg, broker=FakeBroker())
    failures = check_intraday_pretrade(context)
    assert any("kill switch" in f for f in failures)


def test_check_intraday_pretrade_flags_trading_disabled(config, tmp_path):
    # Leave config.intraday at its default (trading_enabled=False).
    context = IntradayPretradeContext(now=NOW, config=config, broker=FakeBroker())
    failures = check_intraday_pretrade(context)
    assert any("not trading_enabled" in f for f in failures)


def test_check_intraday_pretrade_flags_an_unregistered_account_without_naming_it(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, account_number="SOME-OTHER-ACCOUNT")
    context = IntradayPretradeContext(now=NOW, config=cfg, broker=FakeBroker())
    failures = check_intraday_pretrade(context)
    assert any("registered_account" in f for f in failures)
    assert not any(ACCOUNT in f for f in failures)


def test_check_intraday_pretrade_flags_a_blocked_account(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker(account_status="ACCOUNT_BLOCKED")
    context = IntradayPretradeContext(now=NOW, config=cfg, broker=broker)
    failures = check_intraday_pretrade(context)
    assert any("ACCOUNT_BLOCKED" in f for f in failures)


def test_check_intraday_pretrade_flags_a_closed_market(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker(market_open=False)
    context = IntradayPretradeContext(now=NOW, config=cfg, broker=broker)
    failures = check_intraday_pretrade(context)
    assert any("market is closed" in f for f in failures)


def test_check_intraday_pretrade_reports_every_failure_at_once(config, tmp_path):
    """A closed market and trading disabled found together -- both must be reported."""
    broker = FakeBroker(market_open=False)
    context = IntradayPretradeContext(now=NOW, config=config, broker=broker)
    failures = check_intraday_pretrade(context)
    assert any("not trading_enabled" in f for f in failures)
    assert any("market is closed" in f for f in failures)

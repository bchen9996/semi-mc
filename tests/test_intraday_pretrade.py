"""`intraday/pretrade.py`: S4's own sleeve-level gate (S4, `BRIEF_INTRADAY.md`, Phase 3).
Exercised only against `FakeBroker` -- no network, matching this project's convention.
"""

from __future__ import annotations

import datetime as dt

from semimc.config import IntradayConfig
from semimc.execution.fake_broker import FakeBroker
from semimc.intraday.pretrade import (
    IntradayPretradeContext,
    check_intraday_pretrade,
    check_spread_gate,
)

NOW = dt.datetime(2026, 1, 5, 11, 0)
ACCOUNT = "FAKE-PAPER-0001"


def _tradeable_config(config, tmp_path, account_number=ACCOUNT):
    """`shadow_mode: False` (Phase 11's default is `True`) -- these tests exercise the
    real-account-identity path; shadow mode's own bypass gets separate, explicit tests
    below."""
    intraday = config.intraday.model_copy(
        update={
            "trading_enabled": True,
            "registered_account": account_number,
            "shadow_mode": False,
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


# -- shadow / check_market_open (Phase 11, Rollout Stage 1) ------------------------------


def test_check_intraday_pretrade_shadow_skips_the_account_mismatch_check(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, account_number="SOME-OTHER-ACCOUNT")
    context = IntradayPretradeContext(now=NOW, config=cfg, broker=FakeBroker())
    failures = check_intraday_pretrade(context, shadow=True)
    assert not any("registered_account" in f for f in failures)


def test_check_intraday_pretrade_shadow_skips_the_blocked_account_check(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker(account_status="ACCOUNT_BLOCKED")
    context = IntradayPretradeContext(now=NOW, config=cfg, broker=broker)
    failures = check_intraday_pretrade(context, shadow=True)
    assert not any("ACCOUNT_BLOCKED" in f for f in failures)


def test_check_intraday_pretrade_shadow_still_flags_the_kill_switch(config, tmp_path):
    """Shadow mode is still something the owner must deliberately turn on -- an
    emergency halt must stop it exactly like the real loop."""
    cfg = _tradeable_config(config, tmp_path)
    (tmp_path / "KILL_SWITCH").write_text("stop", encoding="utf-8")
    context = IntradayPretradeContext(now=NOW, config=cfg, broker=FakeBroker())
    failures = check_intraday_pretrade(context, shadow=True)
    assert any("kill switch" in f for f in failures)


def test_check_intraday_pretrade_shadow_still_flags_trading_disabled(config, tmp_path):
    context = IntradayPretradeContext(now=NOW, config=config, broker=FakeBroker())
    failures = check_intraday_pretrade(context, shadow=True)
    assert any("not trading_enabled" in f for f in failures)


def test_check_intraday_pretrade_defaults_shadow_to_false(config, tmp_path):
    """No `shadow=` argument reproduces today's exact behavior -- an account mismatch
    still fails."""
    cfg = _tradeable_config(config, tmp_path, account_number="SOME-OTHER-ACCOUNT")
    context = IntradayPretradeContext(now=NOW, config=cfg, broker=FakeBroker())
    failures = check_intraday_pretrade(context)
    assert any("registered_account" in f for f in failures)


def test_check_intraday_pretrade_check_market_open_false_skips_the_closed_market_check(
    config, tmp_path
):
    """`run_research` passes this -- it runs at 07:30 Central, before the 08:30 open, by
    design (found while building Phase 11's shadow mode: the default `True` would
    otherwise fail research every single call, live or shadow)."""
    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker(market_open=False)
    context = IntradayPretradeContext(now=NOW, config=cfg, broker=broker)
    failures = check_intraday_pretrade(context, check_market_open=False)
    assert not any("market is closed" in f for f in failures)


def test_check_intraday_pretrade_defaults_check_market_open_to_true(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    broker = FakeBroker(market_open=False)
    context = IntradayPretradeContext(now=NOW, config=cfg, broker=broker)
    failures = check_intraday_pretrade(context)
    assert any("market is closed" in f for f in failures)


# -- check_spread_gate (S4, Phase 5) -----------------------------------------------------


def test_check_spread_gate_passes_a_tight_fresh_quote():
    config = IntradayConfig()
    reason = check_spread_gate("NVDA", 190.0, 190.05, NOW, NOW, config)
    assert reason is None


def test_check_spread_gate_rejects_a_wide_spread():
    config = IntradayConfig(risk={"max_spread_bps": 10.0})
    reason = check_spread_gate("NVDA", 190.0, 191.0, NOW, NOW, config)  # ~52 bps
    assert reason is not None
    assert "spread" in reason


def test_check_spread_gate_rejects_a_stale_quote():
    config = IntradayConfig(risk={"max_quote_age_seconds": 5.0})
    stale_time = NOW - dt.timedelta(seconds=30)
    reason = check_spread_gate("NVDA", 190.0, 190.05, stale_time, NOW, config)
    assert reason is not None
    assert "old" in reason


def test_check_spread_gate_rejects_an_invalid_quote():
    config = IntradayConfig()
    assert check_spread_gate("NVDA", 0.0, 0.0, NOW, NOW, config) is not None
    assert check_spread_gate("NVDA", 191.0, 190.0, NOW, NOW, config) is not None  # crossed


def test_check_spread_gate_normalizes_naive_and_aware_datetimes():
    """`Quote.timestamp` always comes back aware UTC; a naive `now`/`quote_time` must not
    raise "can't subtract offset-naive and offset-aware datetimes"."""
    config = IntradayConfig()
    aware_quote_time = dt.datetime(2026, 1, 5, 11, 0, tzinfo=dt.UTC)
    naive_now = dt.datetime(2026, 1, 5, 11, 0, 2)
    reason = check_spread_gate("NVDA", 190.0, 190.05, aware_quote_time, naive_now, config)
    assert reason is None

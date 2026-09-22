"""`intraday/reflect.py::record_mechanical_reflection` (S4, Phase 9a) -- pure ledger-read
computation, so every test seeds `proposals`/`orders`/`fills` rows directly rather than
going through the full submission pipeline (`FakeBroker` isn't needed at all here).
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from semimc.intraday.ledger import IntradayLedger
from semimc.intraday.reflect import record_mechanical_reflection

AS_OF = pd.Timestamp("2026-09-21")
NOW = dt.datetime(2026, 9, 21, 9, 0, tzinfo=dt.UTC)


def _ledger(tmp_path) -> IntradayLedger:
    return IntradayLedger(tmp_path / "intraday.sqlite3")


def _seed_proposal(
    ledger: IntradayLedger,
    symbol: str,
    side: str,
    invalidation_price: float | None,
    thesis: str = "test thesis",
    confidence: float = 0.6,
) -> int:
    payload = {
        "action": "open",
        "symbol": symbol,
        "side": side,
        "size_pct": 0.05,
        "limit_price": 100.0,
        "thesis": thesis,
        "invalidation_price": invalidation_price,
        "time_stop_minutes": 120,
        "confidence": confidence,
    }
    return ledger.record_proposal(AS_OF, "cycle_1", symbol, "open", "accepted", payload)


def _seed_fill_order(
    ledger: IntradayLedger,
    *,
    client_order_id: str,
    symbol: str,
    side: str,
    proposal_id: int | None,
    leg: str,
    qty: float,
    price: float,
    filled_at: dt.datetime,
) -> None:
    ledger.record_order(
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        status="filled",
        qty=qty,
        notional=qty * price,
        proposal_id=proposal_id,
        leg=leg,
    )
    ledger.record_fill(client_order_id, qty=qty, price=price, filled_at=filled_at)


def test_computes_gross_and_net_pnl_for_a_respected_long(config, tmp_path):
    ledger = _ledger(tmp_path)
    proposal_id = _seed_proposal(ledger, "NVDA", "buy", invalidation_price=95.0)
    _seed_fill_order(
        ledger,
        client_order_id="entry-1",
        symbol="NVDA",
        side="buy",
        proposal_id=proposal_id,
        leg="entry",
        qty=10,
        price=100.0,
        filled_at=NOW,
    )
    _seed_fill_order(
        ledger,
        client_order_id="exit-1",
        symbol="NVDA",
        side="sell",
        proposal_id=None,
        leg="exit",
        qty=10,
        price=110.0,
        filled_at=NOW + dt.timedelta(minutes=30),
    )

    reflection_id = record_mechanical_reflection(
        "NVDA", "exit-1", "trader_close", ledger, config.intraday, AS_OF
    )

    assert reflection_id is not None
    content = ledger.reflections_for(AS_OF)[0]["content"]
    assert content["pnl_gross"] == 100.0
    assert content["pnl_net"] == 100.0 - 0.0008 * (1000.0 + 1100.0)
    assert content["invalidation_respected"] is True
    assert content["time_in_trade_minutes"] == 30.0
    assert content["close_reason"] == "trader_close"
    assert content["thesis"] == "test thesis"
    assert content["confidence"] == 0.6
    ledger.close()


def test_computes_pnl_for_a_short(config, tmp_path):
    ledger = _ledger(tmp_path)
    proposal_id = _seed_proposal(ledger, "NVDA", "sell", invalidation_price=105.0)
    _seed_fill_order(
        ledger,
        client_order_id="entry-1",
        symbol="NVDA",
        side="sell",
        proposal_id=proposal_id,
        leg="entry",
        qty=10,
        price=100.0,
        filled_at=NOW,
    )
    _seed_fill_order(
        ledger,
        client_order_id="exit-1",
        symbol="NVDA",
        side="buy",
        proposal_id=None,
        leg="exit",
        qty=10,
        price=90.0,
        filled_at=NOW + dt.timedelta(minutes=45),
    )

    record_mechanical_reflection("NVDA", "exit-1", "trader_close", ledger, config.intraday, AS_OF)

    content = ledger.reflections_for(AS_OF)[0]["content"]
    assert content["pnl_gross"] == 100.0
    assert content["pnl_net"] == 100.0 - 0.0008 * (1000.0 + 900.0)
    assert content["invalidation_respected"] is True
    ledger.close()


def test_flags_a_violated_invalidation_on_a_discretionary_close(config, tmp_path):
    """A `close_reason="invalidation"` close is the *system* reacting to a breach --
    its exit price is expected to be at/through the level by construction, so that case
    is always "respected" (see reflect.py's own comment). A genuine violation instead
    looks like a discretionary close that happened to land worse than the stated level
    without the system ever having caught it as a stop-out."""
    ledger = _ledger(tmp_path)
    proposal_id = _seed_proposal(ledger, "NVDA", "buy", invalidation_price=95.0)
    _seed_fill_order(
        ledger,
        client_order_id="entry-1",
        symbol="NVDA",
        side="buy",
        proposal_id=proposal_id,
        leg="entry",
        qty=10,
        price=100.0,
        filled_at=NOW,
    )
    _seed_fill_order(
        ledger,
        client_order_id="exit-1",
        symbol="NVDA",
        side="sell",
        proposal_id=None,
        leg="exit",
        qty=10,
        price=90.0,
        filled_at=NOW + dt.timedelta(minutes=10),
    )

    record_mechanical_reflection("NVDA", "exit-1", "trader_close", ledger, config.intraday, AS_OF)

    content = ledger.reflections_for(AS_OF)[0]["content"]
    assert content["invalidation_respected"] is False
    ledger.close()


def test_an_invalidation_triggered_close_is_always_respected(config, tmp_path):
    """The system-triggered case: the exit price lands at/through the stated level by
    construction (that's what fired it), so this must not score as a violation."""
    ledger = _ledger(tmp_path)
    proposal_id = _seed_proposal(ledger, "NVDA", "buy", invalidation_price=95.0)
    _seed_fill_order(
        ledger,
        client_order_id="entry-1",
        symbol="NVDA",
        side="buy",
        proposal_id=proposal_id,
        leg="entry",
        qty=10,
        price=100.0,
        filled_at=NOW,
    )
    _seed_fill_order(
        ledger,
        client_order_id="exit-1",
        symbol="NVDA",
        side="sell",
        proposal_id=None,
        leg="exit",
        qty=10,
        price=94.0,  # through the invalidation level, as a real stop-out would be
        filled_at=NOW + dt.timedelta(minutes=10),
    )

    record_mechanical_reflection("NVDA", "exit-1", "invalidation", ledger, config.intraday, AS_OF)

    content = ledger.reflections_for(AS_OF)[0]["content"]
    assert content["invalidation_respected"] is True
    ledger.close()


def test_sums_multiple_entry_tranches_sharing_one_proposal(config, tmp_path):
    ledger = _ledger(tmp_path)
    proposal_id = _seed_proposal(ledger, "NVDA", "buy", invalidation_price=95.0)
    _seed_fill_order(
        ledger,
        client_order_id="entry-tranche-1",
        symbol="NVDA",
        side="buy",
        proposal_id=proposal_id,
        leg="entry",
        qty=5,
        price=100.0,
        filled_at=NOW,
    )
    _seed_fill_order(
        ledger,
        client_order_id="entry-tranche-2",
        symbol="NVDA",
        side="buy",
        proposal_id=proposal_id,
        leg="entry",
        qty=5,
        price=102.0,
        filled_at=NOW + dt.timedelta(minutes=2),
    )
    _seed_fill_order(
        ledger,
        client_order_id="exit-1",
        symbol="NVDA",
        side="sell",
        proposal_id=None,
        leg="exit",
        qty=10,
        price=110.0,
        filled_at=NOW + dt.timedelta(minutes=30),
    )

    record_mechanical_reflection("NVDA", "exit-1", "trader_close", ledger, config.intraday, AS_OF)

    content = ledger.reflections_for(AS_OF)[0]["content"]
    # avg entry price = (5*100 + 5*102) / 10 = 101.0
    assert content["avg_entry_price"] == 101.0
    assert content["pnl_gross"] == (110.0 - 101.0) * 10
    # time in trade measured from the earliest tranche's own fill, not the latest.
    assert content["time_in_trade_minutes"] == 30.0
    ledger.close()


def test_finds_the_entry_via_latest_filled_entry_even_when_the_close_has_a_different_proposal_id(
    config, tmp_path
):
    """The real gap this project's own code has: `run_cycle`'s Trader-issued close
    attaches a brand-new close-action proposal_id to the closing order, not the entry's
    own -- confirmed by reading `run_cycle` directly. `record_mechanical_reflection` must
    still find the *entry's* thesis, not the close proposal's (which has none)."""
    ledger = _ledger(tmp_path)
    entry_proposal_id = _seed_proposal(
        ledger, "NVDA", "buy", invalidation_price=95.0, thesis="AI capex cycle"
    )
    close_proposal_id = ledger.record_proposal(
        AS_OF, "cycle_2", "NVDA", "close", "accepted", payload={"action": "close", "symbol": "NVDA"}
    )
    assert close_proposal_id != entry_proposal_id

    _seed_fill_order(
        ledger,
        client_order_id="entry-1",
        symbol="NVDA",
        side="buy",
        proposal_id=entry_proposal_id,
        leg="entry",
        qty=10,
        price=100.0,
        filled_at=NOW,
    )
    _seed_fill_order(
        ledger,
        client_order_id="exit-1",
        symbol="NVDA",
        side="sell",
        proposal_id=close_proposal_id,  # the close order's OWN proposal_id, not the entry's
        leg="exit",
        qty=10,
        price=110.0,
        filled_at=NOW + dt.timedelta(minutes=15),
    )

    record_mechanical_reflection("NVDA", "exit-1", "trader_close", ledger, config.intraday, AS_OF)

    content = ledger.reflections_for(AS_OF)[0]["content"]
    assert content["thesis"] == "AI capex cycle"
    ledger.close()


def test_returns_none_with_no_traceable_entry(config, tmp_path):
    ledger = _ledger(tmp_path)
    result = record_mechanical_reflection(
        "NVDA", "nonexistent", "trader_close", ledger, config.intraday, AS_OF
    )
    assert result is None
    assert ledger.reflections_for(AS_OF) == []
    ledger.close()


def test_returns_none_when_the_close_order_has_no_fill_yet(config, tmp_path):
    ledger = _ledger(tmp_path)
    proposal_id = _seed_proposal(ledger, "NVDA", "buy", invalidation_price=95.0)
    _seed_fill_order(
        ledger,
        client_order_id="entry-1",
        symbol="NVDA",
        side="buy",
        proposal_id=proposal_id,
        leg="entry",
        qty=10,
        price=100.0,
        filled_at=NOW,
    )
    ledger.record_order(
        client_order_id="exit-1", symbol="NVDA", side="sell", status="new", leg="exit"
    )

    result = record_mechanical_reflection(
        "NVDA", "exit-1", "trader_close", ledger, config.intraday, AS_OF
    )

    assert result is None
    assert ledger.reflections_for(AS_OF) == []
    ledger.close()

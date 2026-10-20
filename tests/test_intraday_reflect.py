"""`intraday/reflect.py::record_mechanical_reflection` (S4, Phase 9a) -- pure ledger-read
computation, so every test seeds `proposals`/`orders`/`fills` rows directly rather than
going through the full submission pipeline (`FakeBroker` isn't needed at all here).
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from semimc.intraday.ledger import IntradayLedger
from semimc.intraday.reflect import record_mechanical_reflection, round_trip_count

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


# -- a position is every entry since the symbol was last flat (fixed 2026-10-16) -----------


def _seed_entry(ledger, client_order_id, proposal_id, qty, price, minutes, symbol="NVDA"):
    _seed_fill_order(
        ledger,
        client_order_id=client_order_id,
        symbol=symbol,
        side="buy",
        proposal_id=proposal_id,
        leg="entry",
        qty=qty,
        price=price,
        filled_at=NOW + dt.timedelta(minutes=minutes),
    )


def _seed_exit(ledger, client_order_id, qty, price, minutes, leg="exit", symbol="NVDA"):
    _seed_fill_order(
        ledger,
        client_order_id=client_order_id,
        symbol=symbol,
        side="sell",
        proposal_id=None,
        leg=leg,
        qty=qty,
        price=price,
        filled_at=NOW + dt.timedelta(minutes=minutes),
    )


def test_prices_a_position_built_from_an_open_and_an_add_from_every_entry(config, tmp_path):
    """2026-10-16: NVDA's open (two tranches) and its add are separate proposals, and the
    entry price came from the add's fills alone: $217.33 against a true $217.49."""
    ledger = _ledger(tmp_path)
    open_id = _seed_proposal(ledger, "NVDA", "buy", invalidation_price=216.5)
    add_id = _seed_proposal(ledger, "NVDA", "buy", invalidation_price=216.5)
    _seed_entry(ledger, "open-t1", open_id, 9, 217.61, 0)
    _seed_entry(ledger, "open-t2", open_id, 9, 217.43, 29)
    _seed_entry(ledger, "add-t1", add_id, 4, 217.33, 30)
    _seed_exit(ledger, "exit-1", 22, 216.25, 39)

    record_mechanical_reflection("NVDA", "exit-1", "trader_close", ledger, config.intraday, AS_OF)

    content = ledger.reflections_for(AS_OF)[0]["content"]
    entry_notional = 9 * 217.61 + 9 * 217.43 + 4 * 217.33
    assert content["avg_entry_price"] == pytest.approx(entry_notional / 22)
    assert content["pnl_gross"] == pytest.approx(22 * 216.25 - entry_notional)
    assert content["time_in_trade_minutes"] == 39.0  # from the first tranche, not the add
    assert content["client_order_ids"] == ["exit-1"]
    ledger.close()


def test_an_earlier_closed_position_in_the_symbol_is_not_part_of_the_next(config, tmp_path):
    ledger = _ledger(tmp_path)
    first_id = _seed_proposal(ledger, "NVDA", "buy", invalidation_price=95.0)
    _seed_entry(ledger, "entry-1", first_id, 10, 100.0, 0)
    _seed_exit(ledger, "exit-1", 10, 105.0, 10)
    second_id = _seed_proposal(ledger, "NVDA", "buy", invalidation_price=115.0)
    _seed_entry(ledger, "entry-2", second_id, 5, 120.0, 20)
    _seed_exit(ledger, "exit-2", 5, 118.0, 30)

    record_mechanical_reflection("NVDA", "exit-2", "trader_close", ledger, config.intraday, AS_OF)

    content = ledger.reflections_for(AS_OF)[0]["content"]
    assert content["avg_entry_price"] == 120.0
    assert content["pnl_gross"] == pytest.approx(-10.0)
    assert content["time_in_trade_minutes"] == 10.0
    ledger.close()


def test_a_trims_modeled_cost_covers_only_the_trimmed_shares(config, tmp_path):
    """The entry side of the cost used to be the whole position's, so every trim charged
    it again."""
    ledger = _ledger(tmp_path)
    proposal_id = _seed_proposal(ledger, "NVDA", "buy", invalidation_price=95.0)
    _seed_entry(ledger, "entry-1", proposal_id, 10, 100.0, 0)
    _seed_exit(ledger, "trim-1", 4, 110.0, 10)

    record_mechanical_reflection("NVDA", "trim-1", "trader_trim", ledger, config.intraday, AS_OF)

    content = ledger.reflections_for(AS_OF)[0]["content"]
    assert content["qty"] == 4.0
    assert content["pnl_gross"] == pytest.approx(40.0)
    assert content["pnl_net"] == pytest.approx(40.0 - 0.0008 * (400.0 + 440.0))
    ledger.close()


def test_one_reflection_can_cover_several_stop_legs(config, tmp_path):
    ledger = _ledger(tmp_path)
    proposal_id = _seed_proposal(ledger, "NVDA", "buy", invalidation_price=95.0)
    _seed_entry(ledger, "entry-t1", proposal_id, 5, 100.0, 0)
    _seed_entry(ledger, "entry-t2", proposal_id, 5, 100.0, 1)
    _seed_exit(ledger, "entry-t1::stop", 5, 95.0, 20, leg="stop")
    _seed_exit(ledger, "entry-t2::stop", 5, 94.0, 20, leg="stop")

    record_mechanical_reflection(
        "NVDA",
        "entry-t1::stop",
        "invalidation",
        ledger,
        config.intraday,
        AS_OF,
        also_closed_by=["entry-t2::stop"],
    )

    [reflection] = ledger.reflections_for(AS_OF)
    content = reflection["content"]
    assert content["qty"] == 10.0
    assert content["avg_exit_price"] == pytest.approx(94.5)
    assert content["pnl_gross"] == pytest.approx(-55.0)
    assert content["client_order_ids"] == ["entry-t1::stop", "entry-t2::stop"]
    assert ledger.unreflected_exits() == []  # both legs count as reflected
    ledger.close()


# -- round_trip_count: positions closed, not exits (added 2026-10-20) ---------------------


def test_a_position_closed_in_two_steps_is_one_trade(tmp_path):
    """2026-10-20: AVGO's add was stopped out at 10:12 CT and the rest closed on a time
    stop at 11:00. Two reflections, one trade; Stage 3 needs 8 closed trades."""
    ledger = _ledger(tmp_path)
    _seed_entry(ledger, "open-t1", 1, 2.0, 335.96, 0, symbol="AVGO")
    _seed_entry(ledger, "open-t2", 1, 3.0, 340.61, 29, symbol="AVGO")
    _seed_entry(ledger, "add-t1", 2, 1.0, 340.29, 30, symbol="AVGO")
    _seed_entry(ledger, "add-t2", 2, 1.0, 338.09, 59, symbol="AVGO")
    _seed_exit(ledger, "add-stop-1", 1.0, 335.55, 71, leg="stop", symbol="AVGO")
    _seed_exit(ledger, "add-stop-2", 1.0, 335.55, 71, leg="stop", symbol="AVGO")
    _seed_exit(ledger, "time-stop", 5.0, 335.95, 119, symbol="AVGO")
    # A second AVGO position later the same day is a second trade.
    _seed_entry(ledger, "reopen", 3, 2.0, 334.0, 150, symbol="AVGO")
    _seed_exit(ledger, "reclose", 2.0, 335.0, 180, symbol="AVGO")
    reflections = [
        {"id": 1, "symbol": "AVGO", "content": {"client_order_ids": ["add-stop-1", "add-stop-2"]}},
        {"id": 2, "symbol": "AVGO", "content": {"client_order_ids": ["time-stop"]}},
        {"id": 3, "symbol": "AVGO", "content": {"client_order_ids": ["reclose"]}},
        # No order ids (a hand-recorded reflection): counted on its own.
        {"id": 4, "symbol": "NVDA", "content": {}},
    ]

    assert round_trip_count(ledger, reflections) == 3
    ledger.close()

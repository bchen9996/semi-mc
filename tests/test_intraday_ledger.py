"""`IntradayLedger` (S4, `BRIEF_INTRADAY.md`, Phase 0) -- round-trip every table through
its own `record_x`/read method pair, same testing shape as `live/ledger.py`'s own tests."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from semimc.intraday.ledger import IntradayLedger

AS_OF = pd.Timestamp("2026-09-21")


def _ledger(tmp_path) -> IntradayLedger:
    return IntradayLedger(tmp_path / "intraday.sqlite3")


def test_brief_round_trips(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_brief(AS_OF, {"summary": "quiet morning", "watchlist_count": 3})
    brief = ledger.latest_brief()
    assert brief is not None
    assert brief["content"] == {"summary": "quiet morning", "watchlist_count": 3}
    ledger.close()


def test_latest_brief_is_none_before_any_recorded(tmp_path):
    ledger = _ledger(tmp_path)
    assert ledger.latest_brief() is None
    ledger.close()


def test_watchlist_round_trips(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(
        AS_OF,
        "NVDA",
        thesis="AI capex tailwind into earnings",
        entry_trigger="break above 190",
        invalidation="185",
        size_band="medium",
        horizon="intraday",
        expected_move=0.02,
        expected_cost_bps=8.0,
    )
    rows = ledger.watchlist_for(AS_OF)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "NVDA"
    assert rows[0]["thesis"] == "AI capex tailwind into earnings"
    ledger.close()


def test_every_proposal_is_recorded_including_rejected_ones(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_proposal(AS_OF, "cycle_1", "NVDA", "open", "accepted", payload={"size_pct": 0.02})
    ledger.record_proposal(
        AS_OF,
        "cycle_1",
        "AMD",
        "open",
        "rejected",
        payload={"size_pct": 0.5},
        reason="expected_edge_bps below 2x modeled cost",
    )
    rows = ledger.proposals_for(AS_OF)
    assert len(rows) == 2
    statuses = {row["symbol"]: row["status"] for row in rows}
    assert statuses == {"NVDA": "accepted", "AMD": "rejected"}
    rejected = next(r for r in rows if r["symbol"] == "AMD")
    assert rejected["reason"] == "expected_edge_bps below 2x modeled cost"
    assert rejected["payload"] == {"size_pct": 0.5}
    ledger.close()


def test_order_upsert_by_client_order_id_updates_status_in_place(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_order("s4-nvda-1", "NVDA", "buy", status="new", qty=10, limit_price=190.0)
    ledger.record_order("s4-nvda-1", "NVDA", "buy", status="filled", broker_order_id="abc123")
    order = ledger.get_order("s4-nvda-1")
    assert order is not None
    assert order["status"] == "filled"
    assert order["broker_order_id"] == "abc123"
    assert order["qty"] == 10  # untouched by the second call, which didn't pass qty
    ledger.close()


def test_open_orders_filters_by_status(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_order("s4-open-1", "NVDA", "buy", status="new")
    ledger.record_order("s4-filled-1", "AMD", "buy", status="filled")
    open_ids = {row["client_order_id"] for row in ledger.open_orders()}
    assert open_ids == {"s4-open-1"}
    ledger.close()


def test_record_order_defaults_to_the_entry_leg(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_order("s4-nvda-1", "NVDA", "buy", status="new")
    assert ledger.get_order("s4-nvda-1")["leg"] == "entry"
    ledger.close()


def test_open_stop_leg_for_finds_the_most_recent_open_stop(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_order("s4-nvda-1", "NVDA", "buy", status="filled", leg="entry")
    ledger.record_order(
        "s4-nvda-1::stop", "NVDA", "sell", status="new", broker_order_id="stop-1", leg="stop"
    )
    leg = ledger.open_stop_leg_for("NVDA")
    assert leg is not None
    assert leg["client_order_id"] == "s4-nvda-1::stop"
    assert leg["broker_order_id"] == "stop-1"
    ledger.close()


def test_open_stop_leg_for_is_none_once_canceled(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_order("s4-nvda-1::stop", "NVDA", "sell", status="new", leg="stop")
    ledger.record_order("s4-nvda-1::stop", "NVDA", "sell", status="canceled", leg="stop")
    assert ledger.open_stop_leg_for("NVDA") is None
    ledger.close()


def test_open_stop_leg_for_is_none_for_a_symbol_with_no_stop(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_order("s4-amd-1", "AMD", "buy", status="new", leg="entry")
    assert ledger.open_stop_leg_for("AMD") is None
    ledger.close()


def test_record_order_defaults_to_tranche_one_of_one(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_order("s4-nvda-1", "NVDA", "buy", status="new")
    order = ledger.get_order("s4-nvda-1")
    assert order["tranche_index"] == 1
    assert order["tranche_total"] == 1
    ledger.close()


def test_pending_tranche_for_finds_a_partially_filled_entry(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_order(
        "s4-nvda-1",
        "NVDA",
        "buy",
        status="filled",
        leg="entry",
        tranche_index=1,
        tranche_total=2,
    )
    pending = ledger.pending_tranche_for("NVDA")
    assert pending is not None
    assert pending["client_order_id"] == "s4-nvda-1"
    assert pending["tranche_index"] == 1
    ledger.close()


def test_pending_tranche_for_is_none_once_fully_tranched_in(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_order(
        "s4-nvda-2",
        "NVDA",
        "buy",
        status="filled",
        leg="entry",
        tranche_index=2,
        tranche_total=2,
    )
    assert ledger.pending_tranche_for("NVDA") is None
    ledger.close()


def test_pending_tranche_for_ignores_an_unfilled_entry(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_order(
        "s4-nvda-1",
        "NVDA",
        "buy",
        status="new",
        leg="entry",
        tranche_index=1,
        tranche_total=2,
    )
    assert ledger.pending_tranche_for("NVDA") is None
    ledger.close()


def test_get_proposal_round_trips(tmp_path):
    ledger = _ledger(tmp_path)
    proposal_id = ledger.record_proposal(
        AS_OF, "cycle_1", "NVDA", "open", "accepted", payload={"invalidation_price": 185.0}
    )
    row = ledger.get_proposal(proposal_id)
    assert row is not None
    assert row["payload"]["invalidation_price"] == 185.0
    ledger.close()


def test_get_proposal_is_none_for_an_unknown_id(tmp_path):
    ledger = _ledger(tmp_path)
    assert ledger.get_proposal(999) is None
    ledger.close()


def test_latest_filled_entry_for_finds_the_most_recent_filled_entry(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_order("s4-nvda-1", "NVDA", "buy", status="filled", leg="entry")
    ledger.record_order("s4-nvda-1::stop", "NVDA", "sell", status="new", leg="stop")
    entry = ledger.latest_filled_entry_for("NVDA")
    assert entry is not None
    assert entry["client_order_id"] == "s4-nvda-1"


def test_latest_filled_entry_for_ignores_an_exit_order(tmp_path):
    """A close/trim order recorded with leg='exit' must never masquerade as the entry
    that's still held, or monitor.py's invalidation lookup would break (S4, Phase 5)."""
    ledger = _ledger(tmp_path)
    ledger.record_order("s4-nvda-1", "NVDA", "buy", status="filled", leg="entry")
    ledger.record_order("s4-nvda-2", "NVDA", "sell", status="filled", leg="exit")
    entry = ledger.latest_filled_entry_for("NVDA")
    assert entry is not None
    assert entry["client_order_id"] == "s4-nvda-1"
    ledger.close()


def test_latest_filled_entry_for_is_none_with_no_filled_entry(tmp_path):
    ledger = _ledger(tmp_path)
    assert ledger.latest_filled_entry_for("NVDA") is None
    ledger.close()


def test_session_open_equity_is_set_once_and_never_drifts(tmp_path):
    ledger = _ledger(tmp_path)
    assert ledger.session_open_equity(AS_OF) is None
    ledger.record_session_open_equity(AS_OF, 100_000.0)
    ledger.record_session_open_equity(AS_OF, 90_000.0)  # later cycle -- must not overwrite
    assert ledger.session_open_equity(AS_OF) == pytest.approx(100_000.0)
    ledger.close()


def test_last_stopout_times_reads_the_stopout_prefixed_incidents(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_incident("warning", "stopout:NVDA")
    ledger.record_incident("error", "unrelated incident")
    times = ledger.last_stopout_times()
    assert "NVDA" in times
    assert "unrelated" not in times
    ledger.close()


def test_fill_round_trips(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_order("s4-nvda-1", "NVDA", "buy", status="filled")
    ledger.record_fill("s4-nvda-1", qty=10, price=190.5, filled_at=dt.datetime(2026, 9, 21, 14, 30))
    fills = ledger.fills_for_order("s4-nvda-1")
    assert len(fills) == 1
    assert fills[0]["qty"] == 10
    assert fills[0]["price"] == 190.5
    ledger.close()


def test_snapshot_is_idempotent_by_as_of(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_snapshot(AS_OF, equity=100_000.0, cash=50_000.0, positions={"NVDA": {"qty": 10}})
    ledger.record_snapshot(AS_OF, equity=101_000.0, cash=49_000.0, positions={"NVDA": {"qty": 10}})
    snapshot = ledger.latest_snapshot()
    assert snapshot is not None
    assert snapshot["equity"] == pytest.approx(101_000.0)
    assert snapshot["positions"] == {"NVDA": {"qty": 10}}
    ledger.close()


def test_reconciliation_round_trips(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_reconciliation(AS_OF, status="mismatch", discrepancies=[{"symbol": "NVDA"}])
    latest = ledger.latest_reconciliation()
    assert latest is not None
    assert latest["status"] == "mismatch"
    assert latest["discrepancies"] == [{"symbol": "NVDA"}]
    ledger.close()


def test_reflection_round_trips(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_reflection(
        AS_OF, "NVDA", {"category": "thesis right", "process_grade": 4}, client_order_id="s4-nvda-1"
    )
    reflections = ledger.reflections_for(AS_OF)
    assert len(reflections) == 1
    assert reflections[0]["content"]["process_grade"] == 4
    assert reflections[0]["client_order_id"] == "s4-nvda-1"
    ledger.close()


def test_model_call_round_trips(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_model_call(
        AS_OF,
        cycle="cycle_1",
        role="trader",
        model="claude-haiku-4-5",
        prompt_hash="abc123",
        input_tokens=2000,
        output_tokens=500,
        cost_usd=0.0045,
    )
    rows = ledger.model_calls_for(AS_OF)
    assert len(rows) == 1
    assert rows[0]["role"] == "trader"
    assert rows[0]["cost_usd"] == pytest.approx(0.0045)
    ledger.close()


def test_headline_seen_dedups_by_source_and_hash(tmp_path):
    ledger = _ledger(tmp_path)
    assert ledger.headline_already_seen("alpaca_news", "hash1") is False
    ledger.record_headline_seen(AS_OF, "alpaca_news", "hash1")
    ledger.record_headline_seen(AS_OF, "alpaca_news", "hash1")  # duplicate, should not raise
    assert ledger.headline_already_seen("alpaca_news", "hash1") is True
    ledger.close()


def test_record_headline_seen_stores_symbols_and_headline_text(tmp_path):
    """Phase 6: `headlines_seen` gained `symbols`/`headline` so `recent_headlines_for` has
    something to show the Trader -- the pre-Phase-6 3-arg call (above) still works with
    both left `None`."""
    ledger = _ledger(tmp_path)
    ledger.record_headline_seen(
        AS_OF, "alpaca_news", "hash2", symbols=["NVDA", "AMD"], headline="NVDA rallies on guidance"
    )
    rows = ledger.recent_headlines_for(AS_OF, ["NVDA"])
    assert len(rows) == 1
    assert rows[0]["headline"] == "NVDA rallies on guidance"
    assert rows[0]["symbols"] == "NVDA,AMD"
    ledger.close()


def test_recent_headlines_for_matches_on_symbol_overlap(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_headline_seen(AS_OF, "alpaca_news", "h1", symbols=["NVDA"], headline="NVDA news")
    ledger.record_headline_seen(AS_OF, "alpaca_news", "h2", symbols=["AMD"], headline="AMD news")
    ledger.record_headline_seen(AS_OF, "alpaca_news", "h3", headline=None)  # no symbols/text
    rows = ledger.recent_headlines_for(AS_OF, ["AMD", "TSM"])
    assert [r["headline"] for r in rows] == ["AMD news"]
    ledger.close()


def test_recent_headlines_for_ignores_other_days(tmp_path):
    ledger = _ledger(tmp_path)
    other_day = pd.Timestamp("2026-09-20")
    ledger.record_headline_seen(
        other_day, "alpaca_news", "h1", symbols=["NVDA"], headline="old news"
    )
    assert ledger.recent_headlines_for(AS_OF, ["NVDA"]) == []
    ledger.close()


def test_incidents_reused_from_the_same_shape_as_live_ledger(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_incident("error", "daily model-call budget exhausted")
    incidents = ledger.incidents()
    assert len(incidents) == 1
    assert incidents[0]["severity"] == "error"
    assert incidents[0]["message"] == "daily model-call budget exhausted"
    ledger.close()

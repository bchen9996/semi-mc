"""`IntradayLedger` (S4, `BRIEF_INTRADAY.md`, Phase 0) -- round-trip every table through
its own `record_x`/read method pair, same testing shape as `live/ledger.py`'s own tests."""

from __future__ import annotations

import datetime as dt
import sqlite3

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


def test_opening_a_pre_phase_10_ledger_adds_the_missing_playbook_version_column(tmp_path):
    """Found 2026-09-26: `CREATE TABLE IF NOT EXISTS` never adds a column to a `proposals`
    table that already exists on disk -- the real, long-lived `ledger/intraday.sqlite3`
    predates `playbook_version` (Phase 10), and the first real `record_proposal` call that
    ever reached it raised `OperationalError: table proposals has no column named
    playbook_version`. Reproduce that exact pre-migration file by hand (no `IntradayLedger`
    involved, since it always creates the current, complete schema) and confirm opening it
    now heals the column rather than erroring."""
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            as_of TEXT NOT NULL,
            cycle TEXT NOT NULL,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    ledger = IntradayLedger(path)
    proposal_id = ledger.record_proposal(
        AS_OF, "cycle_1", "NVDA", "open", "accepted", payload={"size_pct": 0.02}, playbook_version=3
    )
    proposal = ledger.get_proposal(proposal_id)
    assert proposal["playbook_version"] == 3
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


def test_update_reflection_overwrites_content_in_place(tmp_path):
    """Phase 9b: `eod_review` fills in `category`/`process_grade`/`lesson` on a row
    Phase 9a's `reflect.py` already created with those fields `None`."""
    ledger = _ledger(tmp_path)
    reflection_id = ledger.record_reflection(
        AS_OF, "NVDA", {"pnl_net": 100.0, "category": None}, client_order_id="s4-nvda-1"
    )
    ledger.update_reflection(reflection_id, {"pnl_net": 100.0, "category": "thesis_right"})

    reflections = ledger.reflections_for(AS_OF)
    assert len(reflections) == 1
    assert reflections[0]["content"] == {"pnl_net": 100.0, "category": "thesis_right"}
    ledger.close()


def test_all_reflections_spans_every_trading_day(tmp_path):
    """`review.py`'s running confidence-calibration curve needs every reflection ever
    recorded, not just one day's (`reflections_for`'s own scope)."""
    ledger = _ledger(tmp_path)
    ledger.record_reflection(pd.Timestamp("2026-09-20"), "NVDA", {"confidence": 0.6})
    ledger.record_reflection(AS_OF, "AMD", {"confidence": 0.8})

    all_rows = ledger.all_reflections()

    assert len(all_rows) == 2
    assert {row["symbol"] for row in all_rows} == {"NVDA", "AMD"}
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


def test_incidents_for_filters_to_the_given_days_incidents(tmp_path):
    """`incidents` has no `as_of` column -- `incidents_for` (Phase 9b, `review.py`'s
    process scorecard) date-filters by `created_at`'s own UTC calendar date instead."""
    ledger = _ledger(tmp_path)
    ledger.record_incident("warning", "today's incident")
    today = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()

    assert len(ledger.incidents_for(today)) == 1
    assert ledger.incidents_for(pd.Timestamp("2000-01-01")) == []
    ledger.close()


# -- shadow broker state / realized cash flow (S4's placebo agent, Phase 7) ---------------


def test_shadow_broker_state_is_none_before_anything_saved(tmp_path):
    ledger = _ledger(tmp_path)
    assert ledger.load_shadow_broker_state() is None
    ledger.close()


def test_shadow_broker_state_round_trips_and_upserts(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.save_shadow_broker_state({"orders": {}, "positions": {}, "open_sides": {}})
    ledger.save_shadow_broker_state({"orders": {"co-1": {}}, "positions": {}, "open_sides": {}})
    state = ledger.load_shadow_broker_state()
    assert state == {"orders": {"co-1": {}}, "positions": {}, "open_sides": {}}
    ledger.close()


def test_total_realized_cash_flow_sums_a_buy_and_a_sell(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_order("s4-nvda-1", "NVDA", "buy", status="filled", leg="entry")
    ledger.record_fill("s4-nvda-1", qty=10, price=100.0, filled_at=dt.datetime(2026, 9, 21, 14, 30))
    ledger.record_order("s4-nvda-2", "NVDA", "sell", status="filled", leg="exit")
    ledger.record_fill("s4-nvda-2", qty=10, price=110.0, filled_at=dt.datetime(2026, 9, 21, 15, 30))
    # buy: -10*100 = -1000; sell: +10*110 = +1100 -> net +100
    assert ledger.total_realized_cash_flow() == pytest.approx(100.0)
    ledger.close()


def test_total_realized_cash_flow_ignores_a_resting_stop_leg(tmp_path):
    """A resting `type="stop"` order never produces a `fills` row (`FakeBroker`'s own
    rule) -- confirms the join needs no explicit `leg` filter to be correct."""
    ledger = _ledger(tmp_path)
    ledger.record_order("s4-nvda-1", "NVDA", "buy", status="filled", leg="entry")
    ledger.record_fill("s4-nvda-1", qty=10, price=100.0, filled_at=dt.datetime(2026, 9, 21, 14, 30))
    ledger.record_order("s4-nvda-1::stop", "NVDA", "sell", status="new", leg="stop")  # never fills
    assert ledger.total_realized_cash_flow() == pytest.approx(-1000.0)
    ledger.close()


def test_total_realized_cash_flow_is_zero_with_no_fills(tmp_path):
    ledger = _ledger(tmp_path)
    assert ledger.total_realized_cash_flow() == pytest.approx(0.0)
    ledger.close()


# -- playbook (S4, Phase 10) --------------------------------------------------------------


def test_record_playbook_decision_round_trips_an_acceptance(tmp_path):
    ledger = _ledger(tmp_path)
    entry_id = ledger.record_playbook_decision(
        "active",
        "size cap: never open a position over 10% of equity",
        "proposals/2026-09-21.md",
        adopted_at=AS_OF,
        review_date=pd.Timestamp("2026-10-21"),
    )
    entry = ledger.get_playbook_entry(entry_id)
    assert entry is not None
    assert entry["status"] == "active"
    assert entry["source_proposal_path"] == "proposals/2026-09-21.md"
    assert entry["resolution_reason"] is None
    ledger.close()


def test_get_playbook_entry_is_none_for_an_unknown_id(tmp_path):
    ledger = _ledger(tmp_path)
    assert ledger.get_playbook_entry(999) is None
    ledger.close()


def test_active_playbook_entries_excludes_rejected_and_retired(tmp_path):
    ledger = _ledger(tmp_path)
    active_id = ledger.record_playbook_decision(
        "active", "rule A", "proposals/a.md", adopted_at=AS_OF, review_date=AS_OF
    )
    ledger.record_playbook_decision(
        "rejected", "rule B", "proposals/b.md", resolution_reason="not enough evidence"
    )
    retired_id = ledger.record_playbook_decision(
        "active", "rule C", "proposals/c.md", adopted_at=AS_OF, review_date=AS_OF
    )
    ledger.update_playbook_entry_status(retired_id, "retired", AS_OF, "stopped holding")

    active = ledger.active_playbook_entries()
    assert [row["id"] for row in active] == [active_id]
    assert len(ledger.all_playbook_entries()) == 3
    ledger.close()


def test_update_playbook_entry_status_sets_resolution_fields(tmp_path):
    ledger = _ledger(tmp_path)
    entry_id = ledger.record_playbook_decision(
        "active", "rule A", "proposals/a.md", adopted_at=AS_OF, review_date=AS_OF
    )
    ledger.update_playbook_entry_status(entry_id, "retired", AS_OF, "stopped holding")
    entry = ledger.get_playbook_entry(entry_id)
    assert entry["status"] == "retired"
    assert entry["resolution_reason"] == "stopped holding"
    assert entry["resolved_at"] is not None
    ledger.close()


def test_current_playbook_version_is_none_before_any_version_recorded(tmp_path):
    ledger = _ledger(tmp_path)
    assert ledger.current_playbook_version() is None
    ledger.close()


def test_record_playbook_version_and_current_playbook_version(tmp_path):
    ledger = _ledger(tmp_path)
    entry_id = ledger.record_playbook_decision(
        "active", "rule A", "proposals/a.md", adopted_at=AS_OF, review_date=AS_OF
    )
    v1 = ledger.record_playbook_version("# PLAYBOOK.md\n\nrule A", "accepted", entry_id)
    assert ledger.current_playbook_version() == v1
    v2 = ledger.record_playbook_version("# PLAYBOOK.md\n\n(empty)", "retired", entry_id)
    assert v2 > v1
    assert ledger.current_playbook_version() == v2
    ledger.close()


def test_distinct_trading_days_spans_proposals_reflections_and_briefs(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_brief(pd.Timestamp("2026-09-18"), {"summary": "quiet"})
    ledger.record_proposal(
        pd.Timestamp("2026-09-19"), "cycle_1", "NVDA", "open", "accepted", payload={}
    )
    ledger.record_reflection(pd.Timestamp("2026-09-21"), "NVDA", content={"pnl_net": 10.0})
    assert ledger.distinct_trading_days() == ["2026-09-18", "2026-09-19", "2026-09-21"]
    ledger.close()


def test_distinct_trading_days_is_empty_with_no_activity(tmp_path):
    ledger = _ledger(tmp_path)
    assert ledger.distinct_trading_days() == []
    ledger.close()
    ledger.close()


# -- monitor checks (S4, cadence telemetry, found 2026-09-23) -----------------------------


def test_record_monitor_check_returns_none_gap_for_the_days_first_check(tmp_path):
    ledger = _ledger(tmp_path)
    gap = ledger.record_monitor_check(AS_OF, "cycle_1", dt.datetime(2026, 9, 21, 13, 30))
    assert gap is None
    ledger.close()


def test_record_monitor_check_computes_the_gap_since_the_same_days_previous_check(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_monitor_check(AS_OF, "cycle_1", dt.datetime(2026, 9, 21, 13, 30))
    gap = ledger.record_monitor_check(AS_OF, "cycle_2", dt.datetime(2026, 9, 21, 13, 35))
    assert gap == pytest.approx(300.0)
    ledger.close()


def test_record_monitor_check_never_counts_the_overnight_gap_across_days(tmp_path):
    """The day's first check must compare against nothing, not yesterday's last check --
    otherwise every single morning would report a multi-hour "cadence failure" that's
    really just the market being closed overnight."""
    ledger = _ledger(tmp_path)
    ledger.record_monitor_check(
        pd.Timestamp("2026-09-20"), "carry", dt.datetime(2026, 9, 20, 19, 45)
    )
    gap = ledger.record_monitor_check(AS_OF, "preflight", dt.datetime(2026, 9, 21, 11, 45))
    assert gap is None
    ledger.close()


def test_record_monitor_check_normalizes_a_naive_datetime_to_utc(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_monitor_check(AS_OF, "cycle_1", dt.datetime(2026, 9, 21, 13, 30, tzinfo=dt.UTC))
    gap = ledger.record_monitor_check(AS_OF, "cycle_2", dt.datetime(2026, 9, 21, 13, 40))
    assert gap == pytest.approx(600.0)
    ledger.close()


def test_monitor_checks_for_returns_every_check_recorded_that_day_in_order(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_monitor_check(AS_OF, "cycle_1", dt.datetime(2026, 9, 21, 13, 30))
    ledger.record_monitor_check(AS_OF, "cycle_2", dt.datetime(2026, 9, 21, 13, 50))
    ledger.record_monitor_check(
        pd.Timestamp("2026-09-22"), "cycle_1", dt.datetime(2026, 9, 22, 13, 30)
    )
    checks = ledger.monitor_checks_for(AS_OF)
    assert [c["cycle"] for c in checks] == ["cycle_1", "cycle_2"]
    assert checks[0]["gap_seconds"] is None
    assert checks[1]["gap_seconds"] == pytest.approx(1200.0)
    ledger.close()

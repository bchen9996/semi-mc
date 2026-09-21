"""`intraday/context.py`: the compact per-cycle context envelope (S4, `BRIEF_INTRADAY.md`,
Phase 2)."""

from __future__ import annotations

import pandas as pd
import pytest

from semimc.config import IntradayConfig
from semimc.intraday.context import (
    WatchlistCandidate,
    build_context,
    build_researcher_context,
    build_trader_context,
    context_hash,
    researcher_output_json_schema,
)
from semimc.intraday.ledger import IntradayLedger

AS_OF = pd.Timestamp("2026-09-21")


def _ledger(tmp_path) -> IntradayLedger:
    return IntradayLedger(tmp_path / "intraday.sqlite3")


# -- build_context ---------------------------------------------------------------------


def test_build_context_labels_exactly_the_requested_keys_as_untrusted():
    bundle = build_context(
        "trader",
        AS_OF,
        "cycle_1",
        sections={"watchlist": [1], "positions": {}, "prices": {}},
        untrusted_keys=["watchlist", "prices"],
    )
    assert bundle.payload["untrusted"] == ["prices", "watchlist"]
    assert bundle.payload["sections"] == {"watchlist": [1], "positions": {}, "prices": {}}
    assert bundle.role == "trader"
    assert bundle.cycle == "cycle_1"


def test_build_context_defaults_to_no_untrusted_keys():
    bundle = build_context("researcher", AS_OF, "research", sections={"news": []})
    assert bundle.payload["untrusted"] == []


def test_build_context_rejects_an_untrusted_key_not_in_sections():
    with pytest.raises(ValueError, match="not_a_section"):
        build_context(
            "trader", AS_OF, "cycle_1", sections={"watchlist": []}, untrusted_keys=["not_a_section"]
        )


# -- context_hash -----------------------------------------------------------------------


def test_context_hash_is_stable_for_the_same_payload():
    bundle_a = build_context("trader", AS_OF, "cycle_1", sections={"a": 1, "b": 2})
    bundle_b = build_context("trader", AS_OF, "cycle_1", sections={"b": 2, "a": 1})
    assert context_hash(bundle_a) == context_hash(bundle_b)


def test_context_hash_changes_when_a_value_changes():
    bundle_a = build_context("trader", AS_OF, "cycle_1", sections={"a": 1})
    bundle_b = build_context("trader", AS_OF, "cycle_1", sections={"a": 2})
    assert context_hash(bundle_a) != context_hash(bundle_b)


# -- build_trader_context -----------------------------------------------------------------


def test_build_trader_context_reflects_todays_accepted_proposals_in_remaining_budget(tmp_path):
    ledger = _ledger(tmp_path)
    config = IntradayConfig(risk={"new_positions_per_day": 3, "round_trips_per_symbol_per_day": 2})
    ledger.record_proposal(AS_OF, "cycle_1", "NVDA", "open", "accepted", payload={})
    ledger.record_proposal(AS_OF, "cycle_1", "AMD", "open", "accepted", payload={})

    bundle = build_trader_context(
        AS_OF, "cycle_2", config, ledger, watchlist=[], positions={}, prices={}
    )

    remaining = bundle.payload["sections"]["budget_remaining"]
    assert remaining["new_positions_per_day"] == 1
    assert remaining["round_trips_per_symbol_per_day"] == {"NVDA": 1, "AMD": 1}
    ledger.close()


def test_build_trader_context_includes_rejected_proposals_with_reasons(tmp_path):
    ledger = _ledger(tmp_path)
    config = IntradayConfig()
    ledger.record_proposal(
        AS_OF, "cycle_1", "NVDA", "open", "rejected", payload={}, reason="edge too low"
    )

    bundle = build_trader_context(
        AS_OF, "cycle_2", config, ledger, watchlist=[], positions={}, prices={}
    )

    rejected = bundle.payload["sections"]["rejected_today"]
    assert rejected == [{"symbol": "NVDA", "action": "open", "reason": "edge too low"}]
    ledger.close()


def test_build_trader_context_marks_watchlist_prices_and_headlines_as_untrusted(tmp_path):
    ledger = _ledger(tmp_path)
    config = IntradayConfig()

    bundle = build_trader_context(
        AS_OF, "cycle_1", config, ledger, watchlist=[], positions={}, prices={}
    )

    assert set(bundle.payload["untrusted"]) == {"watchlist", "prices", "headlines"}
    assert "positions" not in bundle.payload["untrusted"]
    ledger.close()


def test_build_trader_context_defaults_headlines_to_an_empty_list(tmp_path):
    ledger = _ledger(tmp_path)
    config = IntradayConfig()

    bundle = build_trader_context(
        AS_OF, "cycle_1", config, ledger, watchlist=[], positions={}, prices={}
    )

    assert bundle.payload["sections"]["headlines"] == []
    ledger.close()


def test_build_trader_context_passes_through_headlines_when_given(tmp_path):
    """Phase 6: `run_cycle` sources this from `ledger.recent_headlines_for` -- without it,
    `preopen`'s/`research`'s headline logging would be an audit trail nobody reads."""
    ledger = _ledger(tmp_path)
    config = IntradayConfig()
    headlines = [{"headline": "NVDA rallies", "symbols": "NVDA"}]

    bundle = build_trader_context(
        AS_OF, "cycle_1", config, ledger, watchlist=[], positions={}, prices={}, headlines=headlines
    )

    assert bundle.payload["sections"]["headlines"] == headlines
    ledger.close()


def test_build_trader_context_passes_through_watchlist_positions_and_prices(tmp_path):
    ledger = _ledger(tmp_path)
    config = IntradayConfig()
    watchlist = [{"symbol": "NVDA", "thesis": "AI capex"}]
    positions = {"AMD": 5_000.0}
    prices = {"NVDA": 190.0}

    bundle = build_trader_context(AS_OF, "cycle_1", config, ledger, watchlist, positions, prices)

    sections = bundle.payload["sections"]
    assert sections["watchlist"] == watchlist
    assert sections["positions"] == positions
    assert sections["prices"] == prices
    ledger.close()


# -- build_researcher_context (Phase 6) --------------------------------------------------


def test_build_researcher_context_marks_only_news_as_untrusted():
    config = IntradayConfig()
    bundle = build_researcher_context(
        AS_OF,
        "research",
        config,
        news=[{"headline": "NVDA rallies"}],
        premarket_moves={"NVDA": 0.01},
        upcoming_earnings={"AMD": "2026-09-25"},
        realized_vol={"NVDA": 0.35},
    )
    assert bundle.payload["untrusted"] == ["news"]
    assert bundle.role == "researcher"


def test_build_researcher_context_passes_through_every_section():
    config = IntradayConfig()
    news = [{"headline": "NVDA rallies"}]
    premarket_moves = {"NVDA": 0.01}
    upcoming_earnings = {"AMD": "2026-09-25"}
    realized_vol = {"NVDA": 0.35}

    bundle = build_researcher_context(
        AS_OF, "research", config, news, premarket_moves, upcoming_earnings, realized_vol
    )

    sections = bundle.payload["sections"]
    assert sections["news"] == news
    assert sections["premarket_moves"] == premarket_moves
    assert sections["upcoming_earnings"] == upcoming_earnings
    assert sections["realized_vol"] == realized_vol


# -- WatchlistCandidate / researcher_output_json_schema -----------------------------------


def test_watchlist_candidate_requires_only_symbol_and_thesis():
    candidate = WatchlistCandidate(symbol="NVDA", thesis="AI capex cycle")
    assert candidate.entry_trigger is None
    assert candidate.horizon is None


def test_watchlist_candidate_rejects_an_unknown_field():
    with pytest.raises(ValueError, match="size"):
        WatchlistCandidate(symbol="NVDA", thesis="AI capex cycle", size=10_000)


def test_researcher_output_json_schema_requires_brief_and_watchlist():
    schema = researcher_output_json_schema()
    assert schema["required"] == ["morning_brief", "watchlist"]
    assert schema["properties"]["watchlist"]["type"] == "array"

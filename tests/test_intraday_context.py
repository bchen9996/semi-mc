"""`intraday/context.py`: the compact per-cycle context envelope (S4, `BRIEF_INTRADAY.md`,
Phase 2)."""

from __future__ import annotations

import pandas as pd
import pytest

from semimc.config import IntradayConfig
from semimc.intraday.context import build_context, build_trader_context, context_hash
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


def test_build_trader_context_marks_watchlist_and_prices_as_untrusted(tmp_path):
    ledger = _ledger(tmp_path)
    config = IntradayConfig()

    bundle = build_trader_context(
        AS_OF, "cycle_1", config, ledger, watchlist=[], positions={}, prices={}
    )

    assert set(bundle.payload["untrusted"]) == {"watchlist", "prices"}
    assert "positions" not in bundle.payload["untrusted"]
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

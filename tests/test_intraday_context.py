"""`intraday/context.py`: the compact per-cycle context envelope (S4, `BRIEF_INTRADAY.md`,
Phase 2)."""

from __future__ import annotations

import pandas as pd
import pytest

from semimc.config import IntradayConfig
from semimc.intraday.context import (
    TRADER_SECTIONS,
    ReflectionUpdate,
    WatchlistCandidate,
    build_context,
    build_researcher_context,
    build_reviewer_context,
    build_trader_context,
    context_hash,
    researcher_output_json_schema,
    reviewer_output_json_schema,
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


def test_build_trader_context_marks_feed_sourced_sections_as_untrusted(tmp_path):
    ledger = _ledger(tmp_path)
    config = IntradayConfig()

    bundle = build_trader_context(
        AS_OF, "cycle_1", config, ledger, watchlist=[], positions={}, prices={}
    )

    # `session` (2026-09-30) comes from the same market-data feed as `prices`.
    assert set(bundle.payload["untrusted"]) == {"watchlist", "prices", "headlines", "session"}
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


def test_build_trader_context_options_available_is_false_by_default(tmp_path):
    """Rollout Stage 1-3: `IntradayOptionsConfig` defaults both `shadow_pricing` and
    `enabled` to `False`, so the Trader is told options aren't worth proposing -- the same
    "config governs what the model is told" discipline this function already follows for
    every other switch."""
    ledger = _ledger(tmp_path)
    config = IntradayConfig()

    bundle = build_trader_context(
        AS_OF, "cycle_1", config, ledger, watchlist=[], positions={}, prices={}
    )

    assert bundle.payload["sections"]["options_available"] is False
    ledger.close()


def test_build_trader_context_options_available_is_true_under_shadow_pricing(tmp_path):
    """Rollout Stage 4 (Phase 12b): `shadow_pricing` alone (without `enabled`) is enough --
    the Trader may propose options and they get priced/gated/logged even though none of
    them ever reach a real order."""
    ledger = _ledger(tmp_path)
    base = IntradayConfig()
    config = base.model_copy(
        update={"options": base.options.model_copy(update={"shadow_pricing": True})}
    )

    bundle = build_trader_context(
        AS_OF, "cycle_1", config, ledger, watchlist=[], positions={}, prices={}
    )

    assert bundle.payload["sections"]["options_available"] is True
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
        trader_view={"market_panel": ["NVDA"]},
    )
    assert bundle.payload["untrusted"] == ["news"]
    assert bundle.role == "researcher"


def test_build_researcher_context_passes_through_every_section():
    config = IntradayConfig()
    news = [{"headline": "NVDA rallies"}]
    premarket_moves = {"NVDA": 0.01}
    upcoming_earnings = {"AMD": "2026-09-25"}
    realized_vol = {"NVDA": 0.35}

    trader_view = {"market_panel": ["NVDA", "SOXX"], "session_fields": {"last": "..."}}

    bundle = build_researcher_context(
        AS_OF,
        "research",
        config,
        news,
        premarket_moves,
        upcoming_earnings,
        realized_vol,
        trader_view=trader_view,
    )

    sections = bundle.payload["sections"]
    assert sections["news"] == news
    assert sections["premarket_moves"] == premarket_moves
    assert sections["upcoming_earnings"] == upcoming_earnings
    assert sections["realized_vol"] == realized_vol
    assert sections["trader_view"] == trader_view


def test_trader_sections_describe_exactly_the_trader_payloads_sections(tmp_path):
    # The Researcher reads TRADER_SECTIONS as the list of what the Trader receives; a
    # section added to the payload without a description would go unmentioned.
    ledger = _ledger(tmp_path)
    bundle = build_trader_context(
        AS_OF, "cycle_1", IntradayConfig(), ledger, watchlist=[], positions={}, prices={}
    )
    assert set(TRADER_SECTIONS) == set(bundle.payload["sections"])
    ledger.close()


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


# -- build_reviewer_context / ReflectionUpdate / reviewer_output_json_schema (Phase 9b) --


def test_build_reviewer_context_marks_blotter_and_news_as_untrusted():
    config = IntradayConfig()
    bundle = build_reviewer_context(
        AS_OF,
        "eod_review",
        config,
        blotter={"proposals": [], "reflections": []},
        account={"session_open_equity": 100_000.0},
        benchmarks={"soxx_return": 0.01},
        process_metrics={"rejection_rate": 0.0},
        news=[{"headline": "NVDA rallies"}],
        playbook_current="no playbook exists yet",
    )
    assert set(bundle.payload["untrusted"]) == {"blotter", "news"}
    assert "account" not in bundle.payload["untrusted"]
    assert "process_metrics" not in bundle.payload["untrusted"]
    assert bundle.role == "reviewer"


def test_build_reviewer_context_passes_through_every_section():
    config = IntradayConfig()
    blotter = {"proposals": [{"symbol": "NVDA"}], "reflections": []}
    account = {"session_open_equity": 100_000.0}
    benchmarks = {"soxx_return": 0.01, "s0_return": None}
    process_metrics = {"rejection_rate": 0.0, "attribution": {"sector": 10.0}}
    news = [{"headline": "NVDA rallies"}]

    bundle = build_reviewer_context(
        AS_OF,
        "eod_review",
        config,
        blotter,
        account,
        benchmarks,
        process_metrics,
        news,
        "no playbook exists yet",
    )

    sections = bundle.payload["sections"]
    assert sections["blotter"] == blotter
    assert sections["account"] == account
    assert sections["benchmarks"] == benchmarks
    assert sections["process_metrics"] == process_metrics
    assert sections["news"] == news
    assert sections["playbook_current"] == "no playbook exists yet"


def test_reflection_update_requires_a_valid_category_and_grade_range():
    update = ReflectionUpdate(reflection_id=1, category="thesis_right", process_grade=4)
    assert update.lesson is None
    with pytest.raises(ValueError, match="category"):
        ReflectionUpdate(reflection_id=1, category="not_a_category", process_grade=4)
    with pytest.raises(ValueError, match="process_grade"):
        ReflectionUpdate(reflection_id=1, category="thesis_right", process_grade=6)


def test_reflection_update_rejects_an_unknown_field():
    with pytest.raises(ValueError, match="pnl"):
        ReflectionUpdate(reflection_id=1, category="thesis_right", process_grade=4, pnl=100.0)


def test_reviewer_output_json_schema_requires_every_field():
    schema = reviewer_output_json_schema()
    assert schema["required"] == [
        "reflection_updates",
        "closing_market_report",
        "attribution_narrative",
        "process_scorecard_narrative",
        "playbook_proposal",
    ]
    assert schema["properties"]["reflection_updates"]["type"] == "array"


def test_build_trader_context_carries_the_session_section(tmp_path):
    ledger = _ledger(tmp_path)
    session = {"NVDA": {"open": 190.0, "relative_volume": 1.4}}

    with_session = build_trader_context(
        AS_OF, "cycle_1", IntradayConfig(), ledger, [], {}, {}, session=session
    )
    without = build_trader_context(AS_OF, "cycle_1", IntradayConfig(), ledger, [], {}, {})

    assert with_session.payload["sections"]["session"] == session
    assert without.payload["sections"]["session"] == {}
    ledger.close()

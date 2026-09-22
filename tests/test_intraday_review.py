"""`intraday/review.py`: mechanical scoring, attribution, and calibration (S4, Phase 9b)
-- pure ledger-read computation, so tests seed rows directly the same way
`test_intraday_reflect.py` does, rather than going through the full submission pipeline.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from semimc.config import IntradayConfig
from semimc.intraday.ledger import IntradayLedger
from semimc.intraday.review import (
    confidence_calibration,
    day_attribution,
    process_scorecard,
    render_weekly_review_markdown,
    weekly_summary,
)

AS_OF = pd.Timestamp("2026-09-21")


def _ledger(tmp_path) -> IntradayLedger:
    return IntradayLedger(tmp_path / "intraday.sqlite3")


# -- process_scorecard --------------------------------------------------------------------


def test_process_scorecard_computes_rejection_rate_and_reasons(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_proposal(AS_OF, "cycle_1", "NVDA", "open", "accepted", payload={"side": "buy"})
    ledger.record_proposal(
        AS_OF, "cycle_1", "AMD", "open", "rejected", payload={}, reason="edge too low"
    )
    ledger.record_proposal(
        AS_OF, "cycle_2", "TSM", "open", "rejected", payload={}, reason="single_name_cap_pct"
    )

    scorecard = process_scorecard(ledger, IntradayConfig(), AS_OF)

    assert scorecard["rejection_rate"] == pytest.approx(2 / 3)
    assert scorecard["rejection_reasons"] == ["edge too low", "single_name_cap_pct"]
    assert scorecard["new_positions_used"] == 1
    ledger.close()


def test_process_scorecard_is_zero_rejection_rate_with_no_proposals(tmp_path):
    ledger = _ledger(tmp_path)
    scorecard = process_scorecard(ledger, IntradayConfig(), AS_OF)
    assert scorecard["rejection_rate"] == 0.0
    assert scorecard["rejection_reasons"] == []
    ledger.close()


def test_process_scorecard_computes_budget_discipline_against_configured_caps(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_session_open_equity(AS_OF, 100_000.0)
    ledger.record_proposal(AS_OF, "cycle_1", "NVDA", "open", "accepted", payload={"size_pct": 0.05})
    config = IntradayConfig(risk={"new_positions_per_day": 6, "daily_turnover_pct": 1.5})

    scorecard = process_scorecard(ledger, config, AS_OF)

    assert scorecard["new_positions_used"] == 1
    assert scorecard["new_positions_cap"] == 6
    assert scorecard["turnover_usd"] == pytest.approx(0.05 * 100_000.0)
    assert scorecard["turnover_cap_usd"] == pytest.approx(1.5 * 100_000.0)
    ledger.close()


def test_process_scorecard_budget_fields_are_none_with_no_session_open_equity(tmp_path):
    ledger = _ledger(tmp_path)
    scorecard = process_scorecard(ledger, IntradayConfig(), AS_OF)
    assert scorecard["turnover_usd"] is None
    assert scorecard["turnover_cap_usd"] is None
    ledger.close()


def test_process_scorecard_computes_invalidation_adherence_from_todays_reflections(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_reflection(AS_OF, "NVDA", {"invalidation_respected": True})
    ledger.record_reflection(AS_OF, "AMD", {"invalidation_respected": False})
    ledger.record_reflection(AS_OF, "TSM", {"invalidation_respected": None})  # excluded

    scorecard = process_scorecard(ledger, IntradayConfig(), AS_OF)

    assert scorecard["invalidation_adherence_rate"] == pytest.approx(0.5)
    ledger.close()


def test_process_scorecard_invalidation_adherence_is_none_with_no_closes_today(tmp_path):
    ledger = _ledger(tmp_path)
    scorecard = process_scorecard(ledger, IntradayConfig(), AS_OF)
    assert scorecard["invalidation_adherence_rate"] is None
    ledger.close()


def test_process_scorecard_includes_todays_incidents_only(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_incident("warning", "today's incident")
    today = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()

    scorecard = process_scorecard(ledger, IntradayConfig(), today)
    assert len(scorecard["incidents"]) == 1
    other_day_scorecard = process_scorecard(ledger, IntradayConfig(), pd.Timestamp("2000-01-01"))
    assert other_day_scorecard["incidents"] == []
    ledger.close()


# -- confidence_calibration -----------------------------------------------------------------


def test_confidence_calibration_buckets_win_rate_by_stated_confidence(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_reflection(AS_OF, "NVDA", {"confidence": 0.6, "pnl_net": 50.0})
    ledger.record_reflection(AS_OF, "AMD", {"confidence": 0.65, "pnl_net": -10.0})
    ledger.record_reflection(AS_OF, "TSM", {"confidence": 0.9, "pnl_net": 20.0})

    calibration = confidence_calibration(ledger)

    by_range = {tuple(b["confidence_range"]): b for b in calibration}
    assert by_range[(0.5, 0.75)]["n"] == 2
    assert by_range[(0.5, 0.75)]["win_rate"] == pytest.approx(0.5)
    assert by_range[(0.75, 1.0)]["n"] == 1
    assert by_range[(0.75, 1.0)]["win_rate"] == pytest.approx(1.0)
    ledger.close()


def test_confidence_calibration_excludes_reflections_with_no_stated_confidence(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_reflection(AS_OF, "NVDA", {"confidence": None, "pnl_net": 50.0})
    assert confidence_calibration(ledger) == []
    ledger.close()


def test_confidence_calibration_spans_every_trading_day(tmp_path):
    """A running curve, not a one-day snapshot -- `all_reflections`, not `reflections_for`."""
    ledger = _ledger(tmp_path)
    ledger.record_reflection(
        pd.Timestamp("2026-09-01"), "NVDA", {"confidence": 0.6, "pnl_net": 50.0}
    )
    ledger.record_reflection(AS_OF, "AMD", {"confidence": 0.6, "pnl_net": 10.0})

    calibration = confidence_calibration(ledger)

    assert calibration[0]["n"] == 2
    ledger.close()


# -- day_attribution --------------------------------------------------------------------


def test_day_attribution_splits_sector_from_stock_specific():
    reflections = [
        {
            "content": {
                "side": "buy",
                "qty": 10.0,
                "avg_entry_price": 100.0,
                "pnl_net": 150.0,
            }
        }
    ]
    result = day_attribution(reflections, soxx_return=0.01)

    # sector = 10 * 100 * 0.01 = 10.0; stock_specific = 150 - 10 = 140.0
    assert result["sector"] == pytest.approx(10.0)
    assert result["stock_specific"] == pytest.approx(140.0)
    assert result["options"] == 0.0


def test_day_attribution_signs_a_short_position_correctly():
    reflections = [
        {"content": {"side": "sell", "qty": 10.0, "avg_entry_price": 100.0, "pnl_net": 0.0}}
    ]
    result = day_attribution(reflections, soxx_return=0.01)
    # A short's sector exposure is negative: SOXX rallying hurts a short.
    assert result["sector"] == pytest.approx(-10.0)


def test_day_attribution_is_none_with_no_soxx_benchmark():
    reflections = [{"content": {"side": "buy", "qty": 10.0, "avg_entry_price": 100.0}}]
    result = day_attribution(reflections, soxx_return=None)
    assert result == {"sector": None, "stock_specific": None, "options": 0.0}


def test_day_attribution_is_none_with_no_reflections_today():
    result = day_attribution([], soxx_return=0.01)
    assert result == {"sector": None, "stock_specific": None, "options": 0.0}


# -- weekly_summary (Phase 10) ------------------------------------------------------------


def test_weekly_summary_windows_to_the_most_recent_five_trading_days(tmp_path):
    ledger = _ledger(tmp_path)
    days = pd.date_range("2026-09-14", periods=6, freq="D")  # 6 distinct days
    for day in days:
        ledger.record_reflection(day, "NVDA", {"pnl_gross": 10.0, "pnl_net": 8.0})

    summary = weekly_summary(ledger, IntradayConfig(), days[-1])

    assert len(summary["window_trading_days"]) == 5
    assert str(days[0].date()) not in summary["window_trading_days"]
    assert summary["pnl_gross"] == pytest.approx(50.0)  # 5 days, not all 6
    assert summary["pnl_net"] == pytest.approx(40.0)
    assert summary["total_trading_days"] == 6
    assert summary["total_reflections"] == 6
    ledger.close()


def test_weekly_summary_bounds_by_as_of_ignoring_later_activity(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_reflection(pd.Timestamp("2026-09-21"), "NVDA", {"pnl_net": 10.0})
    ledger.record_reflection(pd.Timestamp("2026-09-22"), "AMD", {"pnl_net": 100.0})  # after as_of

    summary = weekly_summary(ledger, IntradayConfig(), pd.Timestamp("2026-09-21"))

    assert summary["window_trading_days"] == ["2026-09-21"]
    assert summary["pnl_net"] == pytest.approx(10.0)
    assert summary["total_trading_days"] == 1
    ledger.close()


def test_weekly_summary_is_empty_with_no_activity(tmp_path):
    ledger = _ledger(tmp_path)
    summary = weekly_summary(ledger, IntradayConfig(), AS_OF)
    assert summary["window_trading_days"] == []
    assert summary["pnl_gross"] == 0.0
    assert summary["avg_rejection_rate"] == 0.0
    assert summary["total_trading_days"] == 0
    ledger.close()


# -- render_weekly_review_markdown --------------------------------------------------------


def _summary_stub() -> dict:
    return {
        "window_trading_days": ["2026-09-21"],
        "pnl_gross": 100.0,
        "pnl_net": 80.0,
        "avg_rejection_rate": 0.25,
        "incidents": 0,
        "calibration": [],
        "total_trading_days": 1,
        "total_reflections": 3,
    }


def test_render_weekly_review_markdown_lists_pending_proposals():
    text = render_weekly_review_markdown(
        _summary_stub(), [Path("proposals/2026-09-21.md")], [], AS_OF
    )
    assert "2026-09-21.md" in text
    assert "None pending." not in text


def test_render_weekly_review_markdown_says_none_pending_with_nothing_outstanding():
    text = render_weekly_review_markdown(_summary_stub(), [], [], AS_OF)
    assert "None pending." in text


def test_render_weekly_review_markdown_lists_entries_due_for_review():
    due = [{"id": 3, "review_date": "2026-09-01 00:00:00"}]
    text = render_weekly_review_markdown(_summary_stub(), [], due, AS_OF)
    assert "Entry 3" in text
    assert "None due." not in text


def test_render_weekly_review_markdown_never_crashes_on_an_empty_week():
    text = render_weekly_review_markdown(_summary_stub(), [], [], AS_OF)
    assert "None pending." in text
    assert "None due." in text

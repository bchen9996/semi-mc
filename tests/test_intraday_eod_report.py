"""`intraday/eod_report.py` (S4, Phase 9a) -- pure ledger-read report building, mirroring
`live/report.py`'s own test shape: build against a seeded ledger, confirm the numbers,
and confirm rendering never crashes on an empty day.
"""

from __future__ import annotations

import pandas as pd
import pytest

from semimc.intraday.eod_report import (
    DayBenchmarks,
    MonitorCadence,
    build_eod_report,
    open_to_close_return,
    render_eod_markdown,
    s0_day_return,
)
from semimc.intraday.ledger import IntradayLedger

AS_OF = pd.Timestamp("2026-09-21")


def _ledger(tmp_path) -> IntradayLedger:
    return IntradayLedger(tmp_path / "intraday.sqlite3")


def test_build_eod_report_is_empty_but_sane_with_no_activity(tmp_path):
    ledger = _ledger(tmp_path)
    report = build_eod_report(ledger, AS_OF)

    assert report.proposals == []
    assert report.reflections == []
    assert report.pnl_gross == 0.0
    assert report.pnl_net == 0.0
    assert report.pnl_pessimistic == 0.0
    assert report.benchmarks == DayBenchmarks()
    assert report.monitor_cadence == MonitorCadence(
        checks=0, avg_gap_minutes=None, max_gap_minutes=None
    )
    ledger.close()


def test_render_eod_markdown_never_crashes_on_an_empty_day(tmp_path):
    ledger = _ledger(tmp_path)
    report = build_eod_report(ledger, AS_OF)
    rendered = render_eod_markdown(report)

    assert "No proposals today." in rendered
    assert "No closed positions today." in rendered
    assert "not available" in rendered  # benchmarks with nothing supplied
    assert "Not enough checks today to measure a gap." in rendered
    # Everything the Reviewer's own call would fill in (Phase 9b's `run_eod_review`)
    # still renders an honest placeholder when `build_eod_report` is called directly,
    # bypassing that call entirely, the way this test does.
    assert "Not yet built (Phase 9b" in rendered
    ledger.close()


# -- monitor cadence (found 2026-09-23) ----------------------------------------------------


def test_build_eod_report_computes_the_average_and_max_gap_from_todays_checks(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_monitor_check(AS_OF, "cycle_1", pd.Timestamp("2026-09-21 13:30").to_pydatetime())
    ledger.record_monitor_check(AS_OF, "cycle_2", pd.Timestamp("2026-09-21 13:50").to_pydatetime())
    ledger.record_monitor_check(AS_OF, "cycle_3", pd.Timestamp("2026-09-21 15:50").to_pydatetime())

    report = build_eod_report(ledger, AS_OF)

    assert report.monitor_cadence.checks == 3
    # gaps: 20m, 120m -> avg 70m, max 120m
    assert report.monitor_cadence.avg_gap_minutes == pytest.approx(70.0)
    assert report.monitor_cadence.max_gap_minutes == pytest.approx(120.0)
    ledger.close()


def test_render_eod_markdown_reports_the_cadence_numbers(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_monitor_check(AS_OF, "cycle_1", pd.Timestamp("2026-09-21 13:30").to_pydatetime())
    ledger.record_monitor_check(AS_OF, "cycle_2", pd.Timestamp("2026-09-21 13:50").to_pydatetime())

    rendered = render_eod_markdown(build_eod_report(ledger, AS_OF))

    assert "## Monitor cadence" in rendered
    assert "Checks today: 2" in rendered
    assert "Average gap between checks: 20.0m" in rendered
    ledger.close()


def test_build_eod_report_sums_pnl_from_recorded_reflections(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_proposal(
        AS_OF, "cycle_1", "NVDA", "open", "accepted", payload={"action": "open", "symbol": "NVDA"}
    )
    ledger.record_proposal(
        AS_OF,
        "cycle_1",
        "AMD",
        "open",
        "rejected",
        payload={"action": "open", "symbol": "AMD"},
        reason="single_name_cap_pct",
    )
    ledger.record_reflection(
        AS_OF,
        "NVDA",
        {
            "pnl_gross": 100.0,
            "pnl_net": 98.32,
            "close_reason": "trader_close",
            "invalidation_respected": True,
        },
        client_order_id="exit-1",
    )
    ledger.record_reflection(
        AS_OF,
        "AMD",
        {
            "pnl_gross": -50.0,
            "pnl_net": -51.5,
            "close_reason": "invalidation",
            "invalidation_respected": True,
        },
        client_order_id="exit-2",
    )

    report = build_eod_report(
        ledger, AS_OF, benchmarks=DayBenchmarks(soxx_return=0.01, s0_return=0.005, placebo_pnl=12.5)
    )

    assert report.pnl_gross == 50.0
    assert report.pnl_net == pytest.approx(98.32 - 51.5)
    modeled_cost = report.pnl_gross - report.pnl_net
    assert report.pnl_pessimistic == pytest.approx(report.pnl_gross - 2 * modeled_cost)
    assert len(report.proposals) == 2
    assert len(report.reflections) == 2

    rendered = render_eod_markdown(report)
    assert "NVDA open [accepted]" in rendered
    assert "AMD open [rejected] -- single_name_cap_pct" in rendered
    assert "+1.00%" in rendered  # SOXX benchmark
    assert "12.50" in rendered  # placebo P&L
    ledger.close()


def test_s0_day_return_uses_the_most_recent_prior_and_today_snapshots():
    class _FakeLedger:
        def snapshots_for(self, sleeve):
            return [
                {"as_of": "2026-09-19", "equity": 100_000.0},
                {"as_of": "2026-09-20", "equity": 101_000.0},
                {"as_of": "2026-09-21", "equity": 100_500.0},
            ]

    result = s0_day_return(_FakeLedger(), AS_OF)

    assert result == pytest.approx((100_500.0 - 101_000.0) / 101_000.0)


def test_s0_day_return_is_none_with_no_snapshot_today():
    class _FakeLedger:
        def snapshots_for(self, sleeve):
            return [{"as_of": "2026-09-19", "equity": 100_000.0}]

    assert s0_day_return(_FakeLedger(), AS_OF) is None


# -- open_to_close_return (added 2026-10-19) ----------------------------------------------


class _Cache:
    def __init__(self, ohlc):
        self.ohlc = ohlc


def _bars(opens: list[float], closes: list[float]) -> dict[str, pd.DataFrame]:
    index = pd.bdate_range(end=AS_OF, periods=len(opens))
    return {
        "open": pd.DataFrame({"SOXX": opens}, index=index),
        "close": pd.DataFrame({"SOXX": closes}, index=index),
    }


def test_open_to_close_return_reads_todays_bar_alone():
    """2026-10-19: SOXX opened 511.30 and closed 501.93 (-1.83%), after a +3.63% gap."""
    cache = _Cache(_bars([490.0, 511.30], [493.39, 501.93]))
    assert open_to_close_return(cache, "SOXX", AS_OF) == pytest.approx(501.93 / 511.30 - 1)


def test_open_to_close_return_is_none_without_todays_bar_or_ohlc():
    assert (
        open_to_close_return(_Cache(_bars([490.0], [493.39])), "SOXX", AS_OF + pd.Timedelta(days=1))
        is None
    )
    assert open_to_close_return(_Cache({}), "SOXX", AS_OF) is None
    assert open_to_close_return(_Cache(_bars([490.0], [493.39])), "SMH", AS_OF) is None


def test_the_report_shows_soxx_from_the_prior_close_and_from_the_open(tmp_path):
    ledger = IntradayLedger(tmp_path / "intraday.sqlite3")
    report = build_eod_report(
        ledger,
        AS_OF,
        benchmarks=DayBenchmarks(soxx_return=0.0173, soxx_open_to_close=-0.0183),
    )

    text = render_eod_markdown(report)

    assert "- SOXX buy-and-hold, prior close to now: +1.73%" in text
    assert "- SOXX open to close (the session an intraday book can trade): -1.83%" in text
    ledger.close()

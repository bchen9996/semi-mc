"""T6's measure and verdict (`THESES.md`, rule set by the owner 2026-11-04; built 2026-11-06).

The measure: every exit since Stage 2 began that `hedge.py` could price, in basis points of
the exit's entry notional -- its P&L net of modeled costs (`reflect.py::trade_figures`
charges both legs) plus the measured SOXX hedge's P&L. The hedge is measured, not traded,
so its own modeled cost isn't charged. The statistic is the agent's mean minus the
placebo's. Each mean's standard error is clustered by trading day, since one day's exits
share that day's market; the difference's is the root of their sum of squares.

The verdicts fall on two dates, and `semimc intraday t6` refuses to run before the first,
so the statistic isn't looked at early. The tests use made-up exits only.

What the measure can't see is counted, not hidden: an exit `hedge.py` hasn't priced yet,
and an exit that never got a reflection at all (one whose proposal was lost with a failed
push, like QCOM's on 2026-11-05).
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from .ledger import IntradayLedger

# THESES.md's rule, fixed before the results it judges: not settings, so not in config.py.
FIRST_VERDICT = dt.date(2027, 1, 15)
FINAL_VERDICT = dt.date(2027, 4, 15)
THRESHOLD_SE = 2.0


def exit_bps(content: dict[str, Any]) -> float:
    """One priced exit's result, in basis points of its entry notional: net P&L plus the
    hedge's P&L. The hedge's cost is left out, since the hedge is never traded."""
    notional = content["qty"] * content["avg_entry_price"]
    return 10_000 * (content["pnl_net"] + content["hedge"]["pnl"]) / notional


def clustered_mean(
    values: Sequence[float], clusters: Sequence[Hashable]
) -> tuple[float | None, float | None]:
    """The mean of `values` and its standard error clustered by `clusters`.

    With n values in G clusters, residuals e_i = x_i - mean and cluster sums
    E_g = sum of e_i over cluster g, the error is sqrt(G / (G - 1) * sum_g E_g^2) / n: the
    usual small-sample correction, and what statsmodels' cluster covariance gives for a
    regression on a constant. With one value per cluster it is the ordinary s / sqrt(n).
    The error is `None` with fewer than two clusters; the mean is `None` with no values.
    """
    n = len(values)
    if n == 0:
        return None, None
    mean = sum(values) / n
    sums: dict[Hashable, float] = defaultdict(float)
    for value, cluster in zip(values, clusters, strict=True):
        sums[cluster] += value - mean
    g = len(sums)
    if g < 2:
        return mean, None
    return mean, math.sqrt(g / (g - 1) * sum(s * s for s in sums.values())) / n


def _day(value: Any) -> dt.date:
    return pd.Timestamp(value).date()


@dataclass(frozen=True)
class SleeveMeasure:
    """One sleeve's exits in the window, in basis points of entry notional."""

    exits: int
    days: int
    mean_bps: float | None
    stderr_bps: float | None
    # In the window, but left out: no hedge block yet (a later run may price them), or no
    # reflection at all (closed, but nothing to say what the trade was).
    unpriced: int
    unreflected: int


def measure_sleeve(ledger: IntradayLedger, since: dt.date, as_of: dt.date) -> SleeveMeasure:
    """`ledger`'s exits from `since` to `as_of`, both included, by the exit's trading day."""
    rows = [r for r in ledger.all_reflections() if since <= _day(r["as_of"]) <= as_of]
    priced = [r for r in rows if r["content"].get("hedge")]
    values = [exit_bps(r["content"]) for r in priced]
    days = [_day(r["as_of"]) for r in priced]
    mean, stderr = clustered_mean(values, days)
    unreflected = [
        o
        for o in ledger.unreflected_exits()
        if since <= _day(o["submitted_at"] or o["updated_at"]) <= as_of
    ]
    return SleeveMeasure(
        exits=len(priced),
        days=len(set(days)),
        mean_bps=mean,
        stderr_bps=stderr,
        unpriced=len(rows) - len(priced),
        unreflected=len(unreflected),
    )


@dataclass(frozen=True)
class T6Result:
    since: dt.date
    as_of: dt.date
    agent: SleeveMeasure
    placebo: SleeveMeasure

    @property
    def difference_bps(self) -> float | None:
        if self.agent.mean_bps is None or self.placebo.mean_bps is None:
            return None
        return self.agent.mean_bps - self.placebo.mean_bps

    @property
    def stderr_bps(self) -> float | None:
        if self.agent.stderr_bps is None or self.placebo.stderr_bps is None:
            return None
        return math.hypot(self.agent.stderr_bps, self.placebo.stderr_bps)

    @property
    def z(self) -> float | None:
        """The difference in standard errors; `None` if either is missing or the error is 0."""
        if self.difference_bps is None or not self.stderr_bps:
            return None
        return self.difference_bps / self.stderr_bps


def evaluate_t6(
    ledger: IntradayLedger, placebo_ledger: IntradayLedger, since: dt.date, as_of: dt.date
) -> T6Result:
    return T6Result(
        since=since,
        as_of=as_of,
        agent=measure_sleeve(ledger, since, as_of),
        placebo=measure_sleeve(placebo_ledger, since, as_of),
    )


def verdict(result: T6Result) -> str:
    """THESES.md's rule for the window ending `result.as_of`. A missing z (too few days to
    estimate an error) is neither ahead nor behind."""
    z = result.z
    ahead = z is not None and z >= THRESHOLD_SE
    behind = z is not None and z <= -THRESHOLD_SE
    stop = "S4 stops trading (`intraday.trading_enabled: false`)."
    if result.as_of < FIRST_VERDICT:
        return f"No verdict: the first is on {FIRST_VERDICT}."
    if result.as_of < FINAL_VERDICT:
        if ahead:
            return (
                "3-month verdict: the agent is ahead by 2 or more standard errors, so the loop "
                "adds something. S4 runs on to the 6-month verdict."
            )
        if behind:
            return f"3-month verdict: the agent is behind by 2 or more standard errors. {stop}"
        return "3-month verdict: within 2 standard errors. S4 runs on to the 6-month verdict."
    if ahead:
        return (
            "6-month verdict: the agent is ahead by 2 or more standard errors, a pass. It "
            "decides nothing further: more size or real money is a separate owner decision."
        )
    return f"6-month verdict: short of 2 standard errors ahead. {stop}"


def _bps(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1f}"


def _se(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def _sleeve_line(name: str, m: SleeveMeasure) -> str:
    return (
        f"| {name} | {m.exits} | {m.days} | {_bps(m.mean_bps)} | {_se(m.stderr_bps)} | "
        f"{m.unpriced} | {m.unreflected} |"
    )


def render_t6_markdown(result: T6Result) -> str:
    z = "n/a" if result.z is None else f"{result.z:+.2f}"
    lines = [
        f"# T6: {result.since} to {result.as_of}",
        "",
        f"**{verdict(result)}**",
        "",
        "| sleeve | exits | trading days | bps per exit, net | std error (by day) | "
        "not priced yet | never reflected |",
        "|---|---|---|---|---|---|---|",
        _sleeve_line("agent", result.agent),
        _sleeve_line("placebo", result.placebo),
        "",
        f"Agent minus placebo: {_bps(result.difference_bps)} bps per exit, standard error "
        f"{_se(result.stderr_bps)}: {z} standard errors.",
        "",
        "Each exit is its P&L net of modeled costs plus the measured SOXX hedge's P&L, in basis "
        "points of its entry notional; the hedge's own cost isn't charged, since it is never "
        "traded. Errors are clustered by trading day; the difference's is the root of the sum "
        "of their squares. The rule, and why it was set when it was: `THESES.md`, T6.",
        "",
        "Left out: an exit with no hedge block yet (a later run may price it), and one with no "
        "reflection at all, such as an exit whose proposal was lost with a failed push "
        "(`tasks/todo.md`, 2026-11-05).",
        "",
    ]
    return "\n".join(lines)

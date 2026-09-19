"""The ``report`` job: a plain read of the ledger, no broker involved. Unlike ``decide``/
``execute``/``mark``, this never touches the network -- it can run (and should keep
running) even when Alpaca or the market data feed is unavailable, since its entire purpose
is to say what happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .ledger import Ledger


@dataclass
class PaperReport:
    sleeve: str
    latest_snapshot: dict[str, Any] | None
    equity_history: pd.DataFrame
    recent_orders: list[dict[str, Any]] = field(default_factory=list)
    recent_fills: list[dict[str, Any]] = field(default_factory=list)
    open_incidents: list[dict[str, Any]] = field(default_factory=list)
    last_reconciliation: dict[str, Any] | None = None


def build_report(sleeve: str, ledger: Ledger, lookback_days: int = 30) -> PaperReport:
    """Everything ``render_markdown`` needs, gathered entirely through the ledger's own
    read methods -- this function never writes SQL itself, matching ``live/ledger.py``'s
    "nothing above this layer writes SQL directly" rule."""
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=lookback_days)

    snapshot_rows = ledger.snapshots_for(sleeve)
    equity_history = pd.DataFrame(
        [{"as_of": r["as_of"], "equity": r["equity"], "cash": r["cash"]} for r in snapshot_rows]
    )
    incident_rows = [
        row for row in ledger.incidents(sleeve) if pd.Timestamp(row["created_at"]) >= cutoff
    ]

    return PaperReport(
        sleeve=sleeve,
        latest_snapshot=ledger.latest_snapshot(sleeve),
        equity_history=equity_history,
        recent_orders=ledger.recent_orders(sleeve),
        recent_fills=ledger.recent_fills(sleeve),
        open_incidents=incident_rows,
        last_reconciliation=ledger.latest_reconciliation(sleeve),
    )


def render_markdown(report: PaperReport) -> str:
    lines = [f"# Paper trading report: {report.sleeve}", ""]

    if report.latest_snapshot is None:
        lines.append("No snapshot recorded yet.")
    else:
        snap = report.latest_snapshot
        lines.append(f"**As of**: {snap['as_of']}")
        lines.append(f"**Equity**: {snap['equity']:,.2f}")
        lines.append(f"**Cash**: {snap['cash']:,.2f}")
        lines.append("")
        lines.append("## Positions")
        if snap["positions"]:
            for symbol, position in sorted(snap["positions"].items()):
                lines.append(
                    f"- {symbol}: qty={position['qty']:.4f}, mv={position['market_value']:,.2f}"
                )
        else:
            lines.append("(flat)")

    lines.append("")
    lines.append("## Reconciliation")
    if report.last_reconciliation is None:
        lines.append("No reconciliation recorded yet.")
    else:
        rec = report.last_reconciliation
        lines.append(f"{rec['as_of']}: {rec['status']}")

    lines.append("")
    lines.append("## Open incidents (last 30 days)")
    if report.open_incidents:
        for incident in report.open_incidents:
            lines.append(
                f"- [{incident['severity']}] {incident['created_at']}: {incident['message']}"
            )
    else:
        lines.append("None.")

    lines.append("")
    lines.append("## Recent orders")
    if report.recent_orders:
        for order in report.recent_orders[:10]:
            lines.append(
                f"- {order['symbol']} {order['side']} qty={order['qty']} status={order['status']}"
            )
    else:
        lines.append("None.")

    return "\n".join(lines) + "\n"

"""T5 (`THESES.md`): after-the-fact execution cost analysis.

A cost accounting exercise, not a hypothesis test the way T2/T3 are -- no bootstrap here,
just signed slippage per fill against an independent arrival price
(``execution/market_data.py``, a source ``execution/orders.py`` never consults when
pricing an order in the first place).
"""

from __future__ import annotations

import pandas as pd

from ..execution.market_data import AlpacaMarketDataClient

_COLUMNS = [
    "client_order_id",
    "symbol",
    "side",
    "qty",
    "fill_price",
    "arrival_price",
    "slippage_bps",
    "filled_at",
]


def execution_cost_report(
    ledger,
    sleeve: str,
    data_client: AlpacaMarketDataClient,  # noqa: ANN001
) -> pd.DataFrame:
    """One row per fill: the fill price already in the ledger against the NBBO midpoint
    prevailing when the order was *submitted*, expressed as signed slippage in bps.

    Positive slippage is always a cost: a buy that filled above arrival, or a sell that
    filled below it. A fill with no recorded ``submitted_at``, or no arrival quote
    available for its timestamp, is skipped rather than guessed at -- a partial report is
    more honest than a fabricated number filling the gap.
    """
    records = []
    for row in ledger.fills_with_order_context(sleeve):
        submitted_at = row.get("submitted_at")
        if not submitted_at:
            continue
        arrival = data_client.arrival_price(
            row["symbol"], pd.Timestamp(submitted_at).to_pydatetime()
        )
        if arrival is None or arrival <= 0:
            continue

        fill_price = float(row["price"])
        sign = 1.0 if row["side"] == "buy" else -1.0
        slippage_bps = sign * (fill_price - arrival) / arrival * 10_000

        records.append(
            {
                "client_order_id": row["client_order_id"],
                "symbol": row["symbol"],
                "side": row["side"],
                "qty": row["qty"],
                "fill_price": fill_price,
                "arrival_price": arrival,
                "slippage_bps": slippage_bps,
                "filled_at": row["filled_at"],
            }
        )

    return pd.DataFrame.from_records(records, columns=_COLUMNS)

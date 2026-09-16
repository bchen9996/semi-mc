"""Customer-to-supplier lead-lag: a supplier's forward return regressed on its customers'
lagged returns.

The idea is standard in the supply-chain literature (Cohen & Frazzini 2008): if AAPL's
stock reacts to demand news before QCOM's does, QCOM's near-term return should carry some
predictable relationship to AAPL's *lagged* return. It needs prices for names outside the
semiconductor universe -- the customers -- which is why the universe those names come from
is deliberately never simulated or put in a portfolio; they exist only as a signal input.

The relationship graph is hand-maintained in ``data/external/supply_chain_links.csv``
(columns ``supplier,customer,source,as_of``) rather than inferred, per the addendum. Two
honesty requirements the addendum is explicit about, both enforced here rather than left to
the caller to remember:

**Survivorship in the link list itself.** ``AAPL`` being NVDA's largest customer today says
nothing about who was buying NVDA chips in 2016. Every link therefore carries the date the
relationship was recorded (``as_of``), and a link is usable at a forecast origin only if
that date is at or before the origin -- exactly the point-in-time rule the rest of the
signal store enforces, applied to the *existence of the relationship* rather than to a
data value.

**Missing customer prices fail loud, not quiet.** A supplier whose customers were never
fetched gets ``NaN`` for this signal, never a zero -- a zero would read to a regression as
"this supplier's customers are flat," which is a claim about the world this code has no
basis to make.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)

LINK_COLUMNS = ["supplier", "customer", "source", "as_of"]


class SupplyChainError(RuntimeError):
    """Raised when the supply-chain link file is missing or malformed."""


def load_supply_chain_links(path: str | Path) -> pd.DataFrame:
    """Read ``supply_chain_links.csv``. A missing file raises -- this is hand-curated data,
    never invented."""
    csv_path = Path(path)
    if not csv_path.exists():
        raise SupplyChainError(
            f"no supply-chain link file at {csv_path}. This is hand-maintained data (the "
            "addendum's signal 5) with no automatic source -- add rows with columns "
            "supplier,customer,source,as_of."
        )
    frame = pd.read_csv(csv_path, comment="#")
    missing = [c for c in LINK_COLUMNS if c not in frame.columns]
    if missing:
        raise SupplyChainError(f"{csv_path} is missing columns: {missing}")

    out = frame.copy()
    out["supplier"] = out["supplier"].astype(str).str.strip().str.upper()
    out["customer"] = out["customer"].astype(str).str.strip().str.upper()
    out["as_of"] = pd.to_datetime(out["as_of"], errors="coerce").dt.normalize()
    bad = out["as_of"].isna()
    if bad.any():
        LOGGER.warning(
            "dropping %d supply-chain links with an unparseable as_of date", int(bad.sum())
        )
        out = out[~bad]
    return out.loc[:, LINK_COLUMNS].reset_index(drop=True)


def links_known_at(links: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Links recorded at or before ``as_of`` -- the point-in-time view of the relationship
    graph itself, not just of a data value."""
    stamp = pd.Timestamp(as_of).normalize()
    return links[links["as_of"] <= stamp]


def customer_lead_lag_signal(
    links: pd.DataFrame,
    customer_returns: pd.DataFrame,
    suppliers: list[str],
    as_of: pd.Timestamp,
    lookback_days: int = 21,
) -> pd.Series:
    """Average lagged cumulative return of each supplier's known customers.

    Parameters
    ----------
    links
        Full link table from :func:`load_supply_chain_links` (filtered to ``as_of`` here,
        not by the caller).
    customer_returns
        Daily log returns for customer tickers (and, harmlessly, anything else); columns
        not present are simply unavailable, not an error.
    suppliers
        Tickers to compute the signal for (the semiconductor universe).
    as_of
        Forecast origin.
    lookback_days
        Trading days of customer return to average over, ending at ``as_of``.

    Returns
    -------
    pd.Series
        Index: supplier. ``NaN`` where no customer link is known yet, or where none of a
        supplier's known customers have return data.
    """
    known = links_known_at(links, as_of)
    stamp = pd.Timestamp(as_of).normalize()
    position = customer_returns.index.searchsorted(stamp, side="right") - 1
    start = position - lookback_days

    out: dict[str, float] = {}
    for supplier in suppliers:
        customers = known.loc[known["supplier"] == supplier, "customer"].unique().tolist()
        available = [c for c in customers if c in customer_returns.columns]
        if not customers:
            out[supplier] = np.nan
            continue
        if not available:
            LOGGER.debug(
                "%s: %d customer link(s) known but none have cached price data",
                supplier,
                len(customers),
            )
            out[supplier] = np.nan
            continue
        if start < 0 or position < 0:
            out[supplier] = np.nan
            continue
        window = customer_returns.iloc[start:position][available]
        per_customer = window.sum()
        out[supplier] = float(per_customer.mean()) if per_customer.notna().any() else np.nan

    return pd.Series(out, name="customer_lead_lag").reindex(suppliers)

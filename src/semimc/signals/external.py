"""Versioned external data: the addendum's ``data/external/`` CSVs.

Every row here is a fact ``value`` about a ``period`` (usually a month), plus the date that
fact became public (``release_date``) and where it came from (``source``). The schema is
deliberately the addendum's own wording, so a file written by hand needs no translation
layer to be read back.

The one series wired up by default is FRED's ``IPG3344S`` (industrial production:
semiconductor and other electronic component manufacturing), chosen over TSMC monthly
revenue and SIA global sales because those two have no free, key-free API -- getting them
means scraping a page, which `CLAUDE.md`'s boundaries put behind an explicit ask. FRED needs
neither a key nor a scraper, using the same public CSV endpoint `data/riskfree.py` already
reads.

The real cost of that choice: FRED serves the **current, revised** vintage of the series,
not the value as first published, and with no vintage history in the free endpoint there is
no way to recover what was actually known on a given day. Two things follow, and both are
surfaced rather than smoothed over. First, ``release_date`` here is an *approximation* --
``period_end + industry_publication_lag_days`` -- not a fact. Second, every value carries
some revision bias: a first estimate for semiconductor IP typically moves in later
vintages, and a signal built on the revised number will look more predictive in-sample than
it would have looked in real time. `MODEL_NOTES.md` states this as a limitation on the
signal, not just on this loader.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

LOGGER = logging.getLogger(__name__)

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

EXTERNAL_COLUMNS = ["period", "value", "release_date", "source", "revised"]


class ExternalSeriesError(RuntimeError):
    """Raised when an external-data CSV is missing or malformed."""


def load_external_series(path: str | Path) -> pd.DataFrame:
    """Read one ``data/external/`` CSV.

    Parameters
    ----------
    path
        CSV with columns ``period,value,release_date,source`` and an optional ``revised``
        boolean column (defaults to ``True`` when absent, since every series wired up here
        so far is revised-only).

    Returns
    -------
    pd.DataFrame
        Columns ``period``, ``value``, ``release_date``, ``source``, ``revised``; sorted by
        ``period``. Raises rather than silently coercing on a missing required column.
    """
    csv_path = Path(path)
    if not csv_path.exists():
        raise ExternalSeriesError(
            f"no external series at {csv_path}. Ask before scraping a new source; FRED-backed "
            "series can be seeded with `semimc fetch --industry`."
        )
    frame = pd.read_csv(csv_path, comment="#")
    missing = [c for c in ("period", "value", "release_date") if c not in frame.columns]
    if missing:
        raise ExternalSeriesError(f"{csv_path} is missing required columns: {missing}")

    out = pd.DataFrame(
        {
            "period": pd.to_datetime(frame["period"], errors="coerce").dt.normalize(),
            "value": pd.to_numeric(frame["value"], errors="coerce"),
            "release_date": pd.to_datetime(frame["release_date"], errors="coerce").dt.normalize(),
            "source": frame["source"].astype(str) if "source" in frame.columns else "unknown",
            "revised": frame["revised"].astype(bool) if "revised" in frame.columns else True,
        }
    )
    bad = out["period"].isna() | out["value"].isna() | out["release_date"].isna()
    if bad.any():
        LOGGER.warning("dropping %d unparseable rows from %s", int(bad.sum()), csv_path)
        out = out[~bad]
    return out.sort_values("period").reset_index(drop=True)


def as_of(series: pd.DataFrame, cutoff: pd.Timestamp) -> pd.Series:
    """Point-in-time view of an external series: value indexed by period, as known at
    ``cutoff``.

    A period is included only if its ``release_date <= cutoff``, and only its most recent
    such row is kept (in case a period has more than one vintage on file). This is the one
    function every consumer of external data is required to go through -- the equivalent
    of ``MarketData.training_window`` for signals that arrive on their own calendar rather
    than the trading calendar.
    """
    stamp = pd.Timestamp(cutoff).normalize()
    known = series[series["release_date"] <= stamp]
    if known.empty:
        return pd.Series(dtype=float, name="value")
    latest = known.sort_values("release_date").groupby("period", sort=True).tail(1)
    return latest.set_index("period")["value"].sort_index()


def fetch_fred_series(series_id: str) -> pd.DataFrame:
    """Download a monthly FRED series and cast it to the external-data schema.

    No API key needed -- the same public CSV endpoint ``data/riskfree.py`` uses. Release
    dates are **not** returned by this endpoint (see this module's docstring); the caller
    supplies the publication-lag approximation via :func:`with_approximate_release_dates`.
    """
    url = FRED_CSV_URL.format(series_id=series_id)
    frame = pd.read_csv(url)
    date_col, value_col = frame.columns[0], frame.columns[1]
    periods = pd.to_datetime(frame[date_col], errors="coerce")
    values = pd.to_numeric(frame[value_col], errors="coerce")
    out = pd.DataFrame({"period": periods, "value": values}).dropna()
    if out.empty:
        raise ExternalSeriesError(f"FRED returned no usable rows for series {series_id!r}")
    return out.sort_values("period").reset_index(drop=True)


def with_approximate_release_dates(
    raw: pd.DataFrame,
    series_id: str,
    publication_lag_days: int,
) -> pd.DataFrame:
    """Attach an approximate release date and source, completing the external-data schema.

    ``release_date = period + publication_lag_days``, treating ``period`` as the *end* of
    the reference month. This is stated as an approximation everywhere it is used --
    ``revised=True`` is set unconditionally, and callers surface it as a limitation rather
    than a fact.
    """
    out = raw.copy()
    out["release_date"] = out["period"] + pd.Timedelta(days=publication_lag_days)
    out["source"] = f"FRED:{series_id} (revised vintage; release date approximated)"
    out["revised"] = True
    return out.loc[:, EXTERNAL_COLUMNS]


def write_external_series(frame: pd.DataFrame, path: str | Path) -> Path:
    """Write an external-data frame to its CSV, with the header the addendum specifies."""
    csv_path = Path(path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# semimc external data series: period,value,release_date,source,revised\n"
        "# release_date is when this value became publicly knowable; a fit at origin t may\n"
        "# use a row only if release_date <= t. See signals/external.py for how this file\n"
        "# is produced and its limitations.\n"
    )
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(header)
        frame.loc[:, EXTERNAL_COLUMNS].to_csv(handle, index=False)
    return csv_path

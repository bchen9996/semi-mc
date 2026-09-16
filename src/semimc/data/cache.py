"""Parquet price cache.

One parquet file per ticker plus a JSON sidecar recording when each was fetched, so a
refresh only hits the network for symbols whose cache is stale or too short. The cache is
append-and-replace: a refetch overwrites the ticker's file wholesale rather than splicing
ranges, which keeps adjustment factors internally consistent (a dividend or split changes
*every* historical adjusted close, so a spliced series would mix adjustment vintages).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .provider import PRICE_COLUMNS, DataProvider, ProviderError

LOGGER = logging.getLogger(__name__)

META_FILENAME = "_cache_meta.json"

# Bumped when the cached column set changes, so an older cache is refetched instead of
# being read with columns that silently do not exist. v2 added adjusted open/high/low for
# the addendum's HAR variance proxy.
SCHEMA_VERSION = 2


@dataclass(frozen=True)
class CacheStatus:
    """Outcome of a fetch/refresh for one ticker."""

    ticker: str
    action: str  # "fetched" | "cached" | "failed"
    rows: int
    first_date: pd.Timestamp | None
    last_date: pd.Timestamp | None
    detail: str = ""


class PriceCache:
    """Parquet-backed cache of adjusted daily prices."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    # -- metadata -------------------------------------------------------------------

    @property
    def meta_path(self) -> Path:
        return self.directory / META_FILENAME

    def _read_meta(self) -> dict[str, dict]:
        if not self.meta_path.exists():
            return {}
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            LOGGER.warning(
                "cache metadata at %s is corrupt; treating cache as empty", self.meta_path
            )
            return {}

    def _write_meta(self, meta: dict[str, dict]) -> None:
        self.meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

    def _path_for(self, ticker: str) -> Path:
        # Symbols such as BRK.B contain characters that are awkward in filenames.
        safe = ticker.replace("/", "_").replace("\\", "_")
        return self.directory / f"{safe}.parquet"

    # -- reads ----------------------------------------------------------------------

    def has(self, ticker: str) -> bool:
        return self._path_for(ticker).exists()

    def read(self, ticker: str) -> pd.DataFrame:
        """Read one ticker's cached long frame. Raises ``KeyError`` when absent."""
        path = self._path_for(ticker)
        if not path.exists():
            raise KeyError(f"{ticker} is not cached at {path}")
        return pd.read_parquet(path)

    def read_many(self, tickers: list[str]) -> pd.DataFrame:
        """Read several tickers into one long frame, skipping any that are absent."""
        frames = [self.read(t) for t in tickers if self.has(t)]
        if not frames:
            raise KeyError(f"none of {tickers} are cached in {self.directory}")
        return pd.concat(frames, ignore_index=True).sort_values(["ticker", "date"])

    def field(self, tickers: list[str], column: str) -> pd.DataFrame:
        """Wide frame of one cached column (index: date, columns: ticker).

        Dates are the union of the tickers' trading days; cells before a ticker's first
        listed day, or after its last, stay ``NaN``. Nothing is forward-filled here --
        alignment decisions belong to :mod:`semimc.data.returns`, which logs them.

        A column absent from an older cache yields an all-``NaN`` frame rather than a
        ``KeyError``, so a stale cache degrades to "no intraday range available" instead
        of crashing every command.
        """
        long = self.read_many(tickers)
        if column not in long.columns:
            LOGGER.warning(
                "cached data has no %r column; refetch with `semimc fetch --force` to get it",
                column,
            )
            index = pd.DatetimeIndex(sorted(long["date"].unique()), name="date")
            return pd.DataFrame(float("nan"), index=index, columns=list(tickers))
        wide = long.pivot_table(index="date", columns="ticker", values=column)
        wide.index = pd.DatetimeIndex(wide.index, name="date")
        present = [t for t in tickers if t in wide.columns]
        return wide.loc[:, present].sort_index()

    def prices(self, tickers: list[str]) -> pd.DataFrame:
        """Wide adjusted-close frame."""
        return self.field(tickers, "adj_close")

    def volumes(self, tickers: list[str]) -> pd.DataFrame:
        """Wide volume frame, same shape conventions as :meth:`prices`."""
        return self.field(tickers, "volume")

    def ohlc(self, tickers: list[str]) -> dict[str, pd.DataFrame]:
        """Adjusted OHLC as four wide frames, keyed ``open``/``high``/``low``/``close``."""
        return {
            "open": self.field(tickers, "adj_open"),
            "high": self.field(tickers, "adj_high"),
            "low": self.field(tickers, "adj_low"),
            "close": self.field(tickers, "adj_close"),
        }

    def coverage(self) -> pd.DataFrame:
        """One row per cached ticker: row count, date span, and fetch timestamp."""
        meta = self._read_meta()
        rows = []
        for path in sorted(self.directory.glob("*.parquet")):
            ticker = path.stem
            frame = pd.read_parquet(path)
            rows.append(
                {
                    "ticker": ticker,
                    "rows": len(frame),
                    "first_date": frame["date"].min(),
                    "last_date": frame["date"].max(),
                    "fetched_at": meta.get(ticker, {}).get("fetched_at"),
                }
            )
        return pd.DataFrame(rows)

    # -- writes ---------------------------------------------------------------------

    def _is_stale(self, ticker: str, start: dt.date, max_staleness_days: int) -> tuple[bool, str]:
        if not self.has(ticker):
            return True, "not cached"
        meta = self._read_meta().get(ticker)
        if not meta:
            return True, "no metadata"
        try:
            fetched_at = dt.datetime.fromisoformat(meta["fetched_at"])
        except (KeyError, ValueError):
            return True, "unreadable fetch timestamp"

        if int(meta.get("schema_version", 1)) < SCHEMA_VERSION:
            return True, (
                f"cache schema v{meta.get('schema_version', 1)} predates v{SCHEMA_VERSION} "
                "(no adjusted open/high/low)"
            )

        age_days = (dt.datetime.now() - fetched_at).days
        if age_days > max_staleness_days:
            return True, f"cache is {age_days}d old (limit {max_staleness_days}d)"

        # Compare against the start date previously *requested*, not the first date
        # returned. A ticker that listed in 2023 will never have 2000 data no matter how
        # often it is refetched -- comparing against its first row would mark ARM and AVGO
        # stale on every single run and refetch them forever.
        requested_before = meta.get("requested_start")
        if requested_before is None:
            return True, "cache predates start-date tracking"
        if pd.Timestamp(start) < pd.Timestamp(requested_before) - pd.Timedelta(days=7):
            return True, (f"cache was fetched from {requested_before}, but {start} is earlier")
        return False, "fresh"

    def refresh(
        self,
        provider: DataProvider,
        tickers: list[str],
        start: dt.date,
        end: dt.date | None = None,
        max_staleness_days: int = 1,
        force: bool = False,
    ) -> list[CacheStatus]:
        """Fetch the tickers whose cache is stale and write them to parquet.

        Returns one :class:`CacheStatus` per requested ticker. Failures are reported,
        never silently swallowed: a ticker that the provider cannot return keeps whatever
        stale data it already had and is marked ``failed``.
        """
        meta = self._read_meta()
        statuses: list[CacheStatus] = []
        to_fetch: list[str] = []

        for ticker in tickers:
            stale, reason = self._is_stale(ticker, start, max_staleness_days)
            if force or stale:
                to_fetch.append(ticker)
                LOGGER.info("refetching %s: %s", ticker, "forced" if force else reason)
            else:
                frame = self.read(ticker)
                statuses.append(
                    CacheStatus(
                        ticker,
                        "cached",
                        len(frame),
                        frame["date"].min(),
                        frame["date"].max(),
                        reason,
                    )
                )

        if to_fetch:
            try:
                fetched = provider.fetch(to_fetch, start=start, end=end)
            except ProviderError as exc:
                LOGGER.error("provider failed for the whole batch: %s", exc)
                fetched = pd.DataFrame(columns=PRICE_COLUMNS)

            now = dt.datetime.now().isoformat(timespec="seconds")
            for ticker in to_fetch:
                subset = fetched[fetched["ticker"] == ticker] if not fetched.empty else fetched
                if subset.empty:
                    detail = "provider returned no rows"
                    LOGGER.warning("%s: %s", ticker, detail)
                    statuses.append(CacheStatus(ticker, "failed", 0, None, None, detail))
                    continue
                subset = subset.sort_values("date").reset_index(drop=True)
                subset.to_parquet(self._path_for(ticker), index=False)
                meta[ticker] = {
                    "fetched_at": now,
                    "schema_version": SCHEMA_VERSION,
                    "requested_start": str(pd.Timestamp(start).date()),
                    "first_date": str(pd.Timestamp(subset["date"].min()).date()),
                    "last_date": str(pd.Timestamp(subset["date"].max()).date()),
                    "rows": int(len(subset)),
                    "provider": getattr(provider, "name", "unknown"),
                }
                statuses.append(
                    CacheStatus(
                        ticker,
                        "fetched",
                        len(subset),
                        subset["date"].min(),
                        subset["date"].max(),
                    )
                )
            self._write_meta(meta)

        order = {t: i for i, t in enumerate(tickers)}
        return sorted(statuses, key=lambda s: order.get(s.ticker, 1_000_000))

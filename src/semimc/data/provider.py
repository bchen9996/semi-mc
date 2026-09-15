"""Price data providers.

Everything downstream talks to :class:`DataProvider`, never to yfinance directly, so a
paid API or a CSV drop can be swapped in by changing ``data.provider`` in the config.

All providers return the same *long* frame:

===========  =========================================================
column       meaning
===========  =========================================================
``date``     trading date (``datetime64[ns]``, tz-naive, normalized)
``ticker``   symbol as requested
``adj_close``  split- and dividend-adjusted close, in USD
``volume``   shares traded (used only for the stale/zero-volume check)
===========  =========================================================

Adjusted closes are mandatory: NVDA and AVGO both split 10-for-1 in 2024, and an
unadjusted series would read those splits as a 90% crash.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd

LOGGER = logging.getLogger(__name__)

PRICE_COLUMNS = ["date", "ticker", "adj_close", "volume"]


class ProviderError(RuntimeError):
    """Raised when a provider cannot return usable data for any requested ticker."""


@runtime_checkable
class DataProvider(Protocol):
    """Minimal interface every price source must satisfy."""

    name: str

    def fetch(
        self,
        tickers: list[str],
        start: dt.date,
        end: dt.date | None = None,
    ) -> pd.DataFrame:
        """Return the long price frame described in this module's docstring.

        Missing tickers are omitted from the result rather than filled; the caller
        compares the returned ticker set against the request and reports the gap.
        """
        ...


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.Series(dtype="datetime64[ns]"),
            "ticker": pd.Series(dtype="object"),
            "adj_close": pd.Series(dtype="float64"),
            "volume": pd.Series(dtype="float64"),
        }
    )


def _tidy(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize dtypes and ordering of a long price frame."""
    if frame.empty:
        return _empty_frame()
    out = frame.loc[:, PRICE_COLUMNS].copy()
    out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None).dt.normalize()
    out["ticker"] = out["ticker"].astype(str)
    out["adj_close"] = pd.to_numeric(out["adj_close"], errors="coerce").astype("float64")
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").astype("float64")
    out = out.dropna(subset=["adj_close"])
    out = out[out["adj_close"] > 0]
    return out.sort_values(["ticker", "date"]).reset_index(drop=True)


class YFinanceProvider:
    """Price provider backed by ``yfinance``.

    Uses ``auto_adjust=True`` so the returned close is already split- and
    dividend-adjusted. Tickers are fetched one at a time: batch downloads return a
    ragged MultiIndex frame in which a single failed symbol is hard to attribute, and
    the per-ticker loop lets a delisted symbol be reported by name.
    """

    name = "yfinance"

    def __init__(self, pause_seconds: float = 0.0) -> None:
        self.pause_seconds = pause_seconds

    def fetch(
        self,
        tickers: list[str],
        start: dt.date,
        end: dt.date | None = None,
    ) -> pd.DataFrame:
        import time

        import yfinance as yf

        frames: list[pd.DataFrame] = []
        failures: dict[str, str] = {}
        for ticker in tickers:
            try:
                raw = yf.download(
                    ticker,
                    start=pd.Timestamp(start).strftime("%Y-%m-%d"),
                    end=pd.Timestamp(end).strftime("%Y-%m-%d") if end else None,
                    auto_adjust=True,
                    progress=False,
                    threads=False,
                    actions=False,
                )
            except Exception as exc:  # noqa: BLE001 - provider faults are reported, not raised
                failures[ticker] = f"{type(exc).__name__}: {exc}"
                continue

            if raw is None or raw.empty:
                failures[ticker] = "no rows returned"
                continue

            # yfinance returns MultiIndex columns (field, ticker) even for one symbol.
            if isinstance(raw.columns, pd.MultiIndex):
                raw = raw.droplevel(1, axis=1)

            frame = pd.DataFrame(
                {
                    "date": raw.index,
                    "ticker": ticker,
                    "adj_close": raw["Close"].to_numpy(dtype="float64"),
                    "volume": raw["Volume"].to_numpy(dtype="float64")
                    if "Volume" in raw
                    else float("nan"),
                }
            )
            frames.append(frame)
            if self.pause_seconds:
                time.sleep(self.pause_seconds)

        if failures:
            for ticker, reason in failures.items():
                LOGGER.warning("provider %s could not fetch %s: %s", self.name, ticker, reason)
        if not frames:
            raise ProviderError(
                f"yfinance returned no data for any of {tickers}. Reasons: {failures}"
            )
        return _tidy(pd.concat(frames, ignore_index=True))


class CSVProvider:
    """Price provider that reads ``<directory>/<TICKER>.csv``.

    The CSV must contain a date column and an adjusted close column; ``volume`` is
    optional. Column names are matched case-insensitively against a few common
    spellings so exports from other tools load without editing.
    """

    name = "csv"

    _DATE_NAMES = ("date", "datetime", "timestamp")
    _PRICE_NAMES = ("adj_close", "adjclose", "adj close", "adjusted_close", "close")
    _VOLUME_NAMES = ("volume", "vol")

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    @classmethod
    def _pick(cls, columns: list[str], candidates: tuple[str, ...]) -> str | None:
        lowered = {c.strip().lower(): c for c in columns}
        for candidate in candidates:
            if candidate in lowered:
                return lowered[candidate]
        return None

    def fetch(
        self,
        tickers: list[str],
        start: dt.date,
        end: dt.date | None = None,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for ticker in tickers:
            path = self.directory / f"{ticker}.csv"
            if not path.exists():
                LOGGER.warning("CSV provider: %s not found", path)
                continue
            raw = pd.read_csv(path)
            columns = list(raw.columns)
            date_col = self._pick(columns, self._DATE_NAMES)
            price_col = self._pick(columns, self._PRICE_NAMES)
            if date_col is None or price_col is None:
                LOGGER.warning("CSV provider: %s lacks a date or adjusted-close column", path)
                continue
            volume_col = self._pick(columns, self._VOLUME_NAMES)
            frames.append(
                pd.DataFrame(
                    {
                        "date": raw[date_col],
                        "ticker": ticker,
                        "adj_close": raw[price_col],
                        "volume": raw[volume_col] if volume_col else float("nan"),
                    }
                )
            )
        if not frames:
            raise ProviderError(f"no CSV files found for {tickers} in {self.directory}")

        out = _tidy(pd.concat(frames, ignore_index=True))
        mask = out["date"] >= pd.Timestamp(start)
        if end is not None:
            mask &= out["date"] <= pd.Timestamp(end)
        return out.loc[mask].reset_index(drop=True)


class InMemoryProvider:
    """Provider over a frame held in memory. Used by tests so they never touch the network."""

    name = "memory"

    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = _tidy(frame)

    def fetch(
        self,
        tickers: list[str],
        start: dt.date,
        end: dt.date | None = None,
    ) -> pd.DataFrame:
        out = self._frame[self._frame["ticker"].isin(tickers)]
        out = out[out["date"] >= pd.Timestamp(start)]
        if end is not None:
            out = out[out["date"] <= pd.Timestamp(end)]
        if out.empty:
            raise ProviderError(f"in-memory provider holds no rows for {tickers}")
        return out.reset_index(drop=True)


def build_provider(config) -> DataProvider:  # noqa: ANN001 - avoids a circular import
    """Instantiate the provider named by ``config.data.provider``."""
    if config.data.provider == "yfinance":
        return YFinanceProvider()
    if config.data.provider == "csv":
        return CSVProvider(config.path(config.data.csv_dir))
    raise ValueError(f"unknown data provider {config.data.provider!r}")

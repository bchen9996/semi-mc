"""Price data providers.

Everything downstream talks to :class:`DataProvider`, never to yfinance directly, so a
paid API or a CSV drop can be swapped in by changing ``data.provider`` in the config.

All providers return the same *long* frame:

=================  ===================================================
column             meaning
=================  ===================================================
``date``           trading date (``datetime64[ns]``, tz-naive, normalized)
``ticker``         symbol as requested
``adj_close``      split- and dividend-adjusted close, in USD
``adj_open``       adjusted open (may be ``NaN``)
``adj_high``       adjusted session high (may be ``NaN``)
``adj_low``        adjusted session low (may be ``NaN``)
``volume``         shares traded (used only for the stale/zero-volume check)
=================  ===================================================

Adjusted closes are mandatory: NVDA and AVGO both split 10-for-1 in 2024, and an
unadjusted series would read those splits as a 90% crash.

Open, high and low arrived with the addendum's HAR model, which needs an intraday range.
They are **optional**: a source that supplies only closes still loads, and the models that
need a range say so rather than silently substituting one. When they are present they must
be adjusted on the same basis as the close -- yfinance's ``auto_adjust=True`` scales every
OHLC field by the same factor, which is what makes ``low <= close <= high`` a usable check
that the adjustment bases did not get mixed.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)

PRICE_COLUMNS = ["date", "ticker", "adj_close", "adj_open", "adj_high", "adj_low", "volume"]
OHLC_COLUMNS = ["adj_open", "adj_high", "adj_low"]


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
            "adj_open": pd.Series(dtype="float64"),
            "adj_high": pd.Series(dtype="float64"),
            "adj_low": pd.Series(dtype="float64"),
            "volume": pd.Series(dtype="float64"),
        }
    )


def _column(frame: pd.DataFrame, name: str) -> np.ndarray | float:
    """One column of a provider response as a float array, or ``NaN`` when absent."""
    return frame[name].to_numpy(dtype="float64") if name in frame else float("nan")


def _tidy(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize dtypes and ordering of a long price frame."""
    if frame.empty:
        return _empty_frame()
    out = frame.copy()
    for column in PRICE_COLUMNS:
        if column not in out.columns:
            out[column] = float("nan")
    out = out.loc[:, PRICE_COLUMNS]
    out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None).dt.normalize()
    out["ticker"] = out["ticker"].astype(str)
    for column in ["adj_close", *OHLC_COLUMNS, "volume"]:
        out[column] = pd.to_numeric(out[column], errors="coerce").astype("float64")
    out = out.dropna(subset=["adj_close"])
    out = out[out["adj_close"] > 0]
    # A non-positive open/high/low is not a price; blank it rather than letting a zero
    # reach a log in the variance proxy.
    for column in OHLC_COLUMNS:
        out.loc[out[column] <= 0, column] = float("nan")
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

            # auto_adjust=True scales Open/High/Low/Close by the same factor, so all four
            # share one adjustment basis. That is what the low <= close <= high check in
            # validation.py is actually testing.
            frame = pd.DataFrame(
                {
                    "date": raw.index,
                    "ticker": ticker,
                    "adj_close": raw["Close"].to_numpy(dtype="float64"),
                    "adj_open": _column(raw, "Open"),
                    "adj_high": _column(raw, "High"),
                    "adj_low": _column(raw, "Low"),
                    "volume": _column(raw, "Volume"),
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
    _OPEN_NAMES = ("adj_open", "adjopen", "adj open", "adjusted_open", "open")
    _HIGH_NAMES = ("adj_high", "adjhigh", "adj high", "adjusted_high", "high")
    _LOW_NAMES = ("adj_low", "adjlow", "adj low", "adjusted_low", "low")

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
            open_col = self._pick(columns, self._OPEN_NAMES)
            high_col = self._pick(columns, self._HIGH_NAMES)
            low_col = self._pick(columns, self._LOW_NAMES)
            frames.append(
                pd.DataFrame(
                    {
                        "date": raw[date_col],
                        "ticker": ticker,
                        "adj_close": raw[price_col],
                        "adj_open": raw[open_col] if open_col else float("nan"),
                        "adj_high": raw[high_col] if high_col else float("nan"),
                        "adj_low": raw[low_col] if low_col else float("nan"),
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

"""Data access facade.

:class:`MarketData` is the single object the CLI, the engine and the backtester read from.
It owns the wide price panel and the derived log returns, and -- importantly -- it is the
only place a *training window* is cut. Keeping that logic in one place is what makes the
point-in-time guarantee checkable: every fit goes through :meth:`MarketData.training_window`
and :meth:`MarketData.context`, both of which take an ``as_of`` and refuse to look past it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from ..context import FitContext
from .cache import PriceCache
from .earnings import EarningsCalendar, load_calendar
from .provider import build_provider
from .returns import log_returns as compute_log_returns
from .riskfree import RiskFreeCurve, build_curve

LOGGER = logging.getLogger(__name__)

__all__ = [
    "EarningsCalendar",
    "MarketData",
    "PriceCache",
    "RiskFreeCurve",
    "load_market_data",
    "refresh_cache",
]


@dataclass
class MarketData:
    """Prices, returns and the point-in-time slicing rules that go with them."""

    prices: pd.DataFrame
    volumes: pd.DataFrame
    config: object
    risk_free: RiskFreeCurve
    earnings: EarningsCalendar | None = None

    def __post_init__(self) -> None:
        self.log_returns = compute_log_returns(self.prices)

    # -- views ----------------------------------------------------------------------

    @property
    def tickers(self) -> list[str]:
        return list(self.prices.columns)

    @property
    def last_date(self) -> pd.Timestamp:
        return pd.Timestamp(self.prices.index.max())

    def spot(
        self, tickers: list[str] | None = None, as_of: pd.Timestamp | None = None
    ) -> pd.Series:
        """Last observed price at or before ``as_of``, per ticker.

        Uses each ticker's own last observation rather than a single row, so a ticker
        that did not trade on the origin date still gets its true most recent price
        instead of a ``NaN``.
        """
        frame = self.prices if as_of is None else self.prices.loc[: pd.Timestamp(as_of)]
        columns = tickers if tickers is not None else self.tickers
        available = [t for t in columns if t in frame.columns]
        return frame.loc[:, available].ffill().iloc[-1] if len(frame) else pd.Series(dtype=float)

    def benchmark_returns(self, as_of: pd.Timestamp | None = None) -> pd.DataFrame:
        """Benchmark log returns up to ``as_of``."""
        names = [t for t in self.config.benchmarks.tickers if t in self.log_returns.columns]
        frame = self.log_returns.loc[:, names]
        return frame if as_of is None else frame.loc[: pd.Timestamp(as_of)]

    # -- point-in-time slicing -------------------------------------------------------

    def training_window(
        self,
        tickers: list[str],
        as_of: pd.Timestamp,
        window_days: int | None = None,
        min_observations: int | None = None,
        require_complete: bool = True,
    ) -> pd.DataFrame:
        """Return the fit window for ``tickers`` ending at ``as_of``.

        Parameters
        ----------
        tickers
            Symbols to include.
        as_of
            Forecast origin. Nothing after this date is ever returned -- this is the
            single choke point the leakage test exercises.
        window_days
            Calendar-day length of the window. Defaults to the configured fit window.
        min_observations
            Minimum rows required; a shorter window raises.
        require_complete
            Drop dates on which any requested ticker is missing, so models receive a
            rectangular panel.

        Returns
        -------
        pd.DataFrame
            Daily log returns. Index: dates. Columns: the requested tickers, in order.
        """
        stamp = pd.Timestamp(as_of).normalize()
        days = window_days if window_days is not None else self.config.fit.window_days
        minimum = (
            min_observations if min_observations is not None else self.config.fit.min_observations
        )

        available = [t for t in tickers if t in self.log_returns.columns]
        missing = [t for t in tickers if t not in self.log_returns.columns]
        if missing:
            raise KeyError(f"no return data for {missing}")

        window = self.log_returns.loc[stamp - pd.Timedelta(days=days) : stamp, available]
        available_rows = window.notna().sum()
        window = window.dropna(how="any" if require_complete else "all")

        # Complete-case alignment is silent by default, and one short history can cost
        # everyone years of data: ARM listed in September 2023, so asking for the whole
        # universe collapses a five-year window to whatever ARM has. Say so.
        if require_complete and len(available_rows) > 1 and len(window):
            longest = int(available_rows.max())
            if len(window) < 0.8 * longest:
                limiting = available_rows.idxmin()
                LOGGER.warning(
                    "aligning %d tickers on common dates leaves %d rows, down from %d; "
                    "%s has the shortest history (%d rows) and is the binding constraint",
                    len(available_rows),
                    len(window),
                    longest,
                    limiting,
                    int(available_rows.min()),
                )

        if len(window) < minimum:
            raise ValueError(
                f"only {len(window)} observations for {available} in the window ending "
                f"{stamp.date()}; {minimum} required"
            )
        return window

    def context(self, as_of: pd.Timestamp, for_backtest: bool = False) -> FitContext:
        """Build the :class:`FitContext` for a forecast origin.

        Benchmark returns and the risk-free curve are truncated to ``as_of``. The earnings
        calendar deliberately is not: a company confirms its next report date weeks ahead,
        so the forward *schedule* is genuinely known at the origin, while the *reaction*
        to each report is what the model has to forecast. Truncating it here would delete
        every future date and silently reduce the earnings overlay to its own base model.
        Models estimate the jump distribution from ``calendar.truncated(as_of)``, so past
        reactions stay strictly point-in-time. See MODEL_NOTES section 4.
        """
        stamp = pd.Timestamp(as_of).normalize()
        curve = self.risk_free.truncated(stamp) if for_backtest else self.risk_free
        return FitContext(
            as_of=stamp,
            risk_free_rate=curve.rate_at(stamp),
            benchmark_returns=self.benchmark_returns(stamp),
            market_ticker=self.config.benchmarks.market,
            sector_ticker=self.config.benchmarks.sector,
            trading_days_per_year=self.config.simulation.trading_days_per_year,
            drift_policy=self.config.drift.policy,
            equity_risk_premium=self.config.drift.equity_risk_premium,
            shrinkage_weight=self.config.drift.shrinkage_weight,
            manual_annual_return=dict(self.config.drift.manual_annual_return),
            earnings=self.earnings,  # schedule stays known; see the docstring above
        )


def refresh_cache(config, tickers: list[str] | None = None, force: bool = False):  # noqa: ANN001
    """Fetch or refresh the parquet cache for the configured universe."""
    cache = PriceCache(config.cache_path)
    provider = build_provider(config)
    symbols = tickers if tickers is not None else config.all_tickers
    return cache.refresh(
        provider=provider,
        tickers=symbols,
        start=config.data.start,
        max_staleness_days=config.data.max_staleness_days,
        force=force,
    )


def load_market_data(
    config,  # noqa: ANN001
    tickers: list[str] | None = None,
    for_backtest: bool = False,
) -> MarketData:
    """Load cached prices into a :class:`MarketData`.

    Raises a clear instruction to run ``semimc fetch`` rather than silently fetching:
    a command that quietly hits the network on first use makes test isolation and run
    reproducibility much harder to reason about.
    """
    cache = PriceCache(config.cache_path)
    symbols = tickers if tickers is not None else config.all_tickers
    cached = [t for t in symbols if cache.has(t)]
    if not cached:
        raise FileNotFoundError(
            f"no cached prices for {symbols} in {config.cache_path}. Run `semimc fetch` first."
        )
    absent = [t for t in symbols if t not in cached]
    if absent:
        LOGGER.warning("not cached, continuing without: %s", ", ".join(absent))

    return MarketData(
        prices=cache.prices(cached),
        volumes=cache.volumes(cached),
        config=config,
        risk_free=build_curve(config, for_backtest=for_backtest),
        earnings=load_calendar(config.path(config.data.earnings_calendar)),
    )

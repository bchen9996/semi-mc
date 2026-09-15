"""Chunked simulation runner.

A full path array gets large quickly: 10,000 paths x 252 days x 15 tickers in float64 is
about 300 MB, and a backtest builds one of those per origin. So paths are simulated in
chunks and reduced on the fly -- terminal values, maximum drawdown and threshold hits are
accumulated per chunk and the chunk is discarded. Only the series actually being
fan-charted keep their full price paths, in float32.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..models.base import ReturnModel

LOGGER = logging.getLogger(__name__)

REBALANCE_STEPS = {"none": 0, "monthly": 21, "quarterly": 63}


@dataclass
class SimulationResult:
    """Reduced output of a simulation run.

    Attributes
    ----------
    tickers
        Column order of every per-ticker array.
    terminal_log_returns
        Cumulative log return at the horizon, shape ``(n_paths, n_tickers)``.
    max_drawdown
        Most negative peak-to-trough simple return along each path, shape
        ``(n_paths, n_tickers)``. Measured from the starting price, so a path that never
        rises above spot still reports a real drawdown.
    tracked_paths
        Full price paths for the series named in ``track``, shape
        ``(n_paths, horizon + 1)`` including the starting price at column 0.
    portfolio_value
        Portfolio value paths (starting value 1.0) when weights were supplied.
    portfolio_max_drawdown
        Per-path maximum drawdown of the portfolio value.
    """

    tickers: list[str]
    horizon: int
    n_paths: int
    spot: pd.Series
    terminal_log_returns: np.ndarray
    max_drawdown: np.ndarray
    tracked_paths: dict[str, np.ndarray] = field(default_factory=dict)
    portfolio_value: np.ndarray | None = None
    portfolio_max_drawdown: np.ndarray | None = None
    portfolio_weights: pd.Series | None = None
    model_name: str = ""
    seed: int | None = None
    as_of: pd.Timestamp | None = None

    @property
    def terminal_prices(self) -> pd.DataFrame:
        """Terminal price per path and ticker."""
        prices = self.spot.reindex(self.tickers).to_numpy(dtype=float) * np.exp(
            self.terminal_log_returns
        )
        return pd.DataFrame(prices, columns=self.tickers)

    def terminal_simple_returns(self) -> pd.DataFrame:
        """Terminal simple return per path and ticker."""
        return pd.DataFrame(np.expm1(self.terminal_log_returns), columns=self.tickers)

    def terminal_for(self, ticker: str) -> np.ndarray:
        """Terminal log returns for one ticker."""
        return self.terminal_log_returns[:, self.tickers.index(ticker)]


def portfolio_value_paths(
    log_returns: np.ndarray,
    weights: np.ndarray,
    rebalance_every: int = 0,
) -> np.ndarray:
    """Portfolio value paths from simulated per-ticker log returns.

    Holdings are aggregated in **simple** returns, never log returns: the log return of a
    portfolio is not the weighted average of its constituents' log returns, and treating
    it as one understates diversification and misprices every risk number downstream.

    Parameters
    ----------
    log_returns
        Simulated daily log returns, shape ``(n_paths, horizon, n_tickers)``.
    weights
        Target weights, shape ``(n_tickers,)``. Need not sum to 1; they are used as given.
    rebalance_every
        0 for buy-and-hold (weights drift with performance); otherwise the number of
        trading days between resets to the target weights.

    Returns
    -------
    np.ndarray
        Value paths including the starting value of 1.0, shape ``(n_paths, horizon + 1)``.
    """
    n_paths, horizon, _ = log_returns.shape
    cumulative = np.cumsum(log_returns, axis=1)
    growth = np.exp(cumulative)  # price relative to the start of the current block

    if rebalance_every <= 0:
        values = growth @ weights
        return np.concatenate([np.full((n_paths, 1), weights.sum()), values], axis=1)

    values = np.empty((n_paths, horizon + 1), dtype=float)
    values[:, 0] = weights.sum()
    block_start = 0
    level = np.full(n_paths, weights.sum(), dtype=float)
    while block_start < horizon:
        block_end = min(block_start + rebalance_every, horizon)
        # Growth relative to the first day of this block, so weights reset at its start.
        block = np.exp(
            cumulative[:, block_start:block_end, :]
            - (cumulative[:, block_start - 1, :][:, None, :] if block_start > 0 else 0.0)
        )
        relative = block @ (weights / weights.sum())
        values[:, block_start + 1 : block_end + 1] = level[:, None] * relative
        level = values[:, block_end]
        block_start = block_end
    return values


def _max_drawdown_from_prices(prices: np.ndarray) -> np.ndarray:
    """Most negative peak-to-trough simple return per path.

    Parameters
    ----------
    prices
        Price paths including the starting price, shape ``(n_paths, horizon + 1)`` or
        ``(n_paths, horizon + 1, n_tickers)``.

    Returns
    -------
    np.ndarray
        Maximum drawdown, negative or zero, shape ``(n_paths,)`` or
        ``(n_paths, n_tickers)``.
    """
    running_peak = np.maximum.accumulate(prices, axis=1)
    return np.min(prices / running_peak - 1.0, axis=1)


def run_simulation(
    model: ReturnModel,
    spot: pd.Series,
    n_paths: int,
    horizon: int,
    rng: np.random.Generator,
    chunk_size: int = 2000,
    dtype: str = "float32",
    track: list[str] | None = None,
    portfolio_weights: pd.Series | None = None,
    rebalance: str = "none",
) -> SimulationResult:
    """Simulate in chunks and reduce on the fly.

    Parameters
    ----------
    model
        A fitted model exposing ``simulate``.
    spot
        Starting prices indexed by ticker; must cover every ticker the model simulates.
    n_paths, horizon
        Path count and forecast length in trading days.
    rng
        The single generator for the run. Passed explicitly -- never seeded globally --
        so a run is reproducible from its recorded seed.
    chunk_size
        Paths per chunk. Peak memory is roughly
        ``chunk_size * horizon * n_tickers * itemsize``.
    dtype
        Storage dtype for retained arrays. float32 halves memory at a precision cost far
        below Monte Carlo error.
    track
        Tickers whose full price paths to retain for fan charts. ``None`` retains none.
    portfolio_weights
        Optional weights; when given, portfolio value paths and drawdowns are produced.
    rebalance
        ``none``, ``monthly``, or ``quarterly``.

    Returns
    -------
    SimulationResult
    """
    tickers = list(getattr(model, "tickers", []))
    if not tickers:
        raise ValueError("model exposes no tickers; was it fitted?")
    missing = [t for t in tickers if t not in spot.index]
    if missing:
        raise ValueError(f"no starting price for {missing}")

    if rebalance not in REBALANCE_STEPS:
        raise ValueError(f"unknown rebalance policy {rebalance!r}")
    rebalance_every = REBALANCE_STEPS[rebalance]

    store = np.dtype(dtype)
    spot_values = spot.reindex(tickers).to_numpy(dtype=float)
    track = [t for t in (track or []) if t in tickers]

    weights_vector = None
    if portfolio_weights is not None:
        weights_vector = portfolio_weights.reindex(tickers).fillna(0.0).to_numpy(dtype=float)
        if weights_vector.sum() <= 0:
            raise ValueError("portfolio weights must sum to a positive number")

    terminal_chunks: list[np.ndarray] = []
    drawdown_chunks: list[np.ndarray] = []
    tracked_chunks: dict[str, list[np.ndarray]] = {t: [] for t in track}
    portfolio_chunks: list[np.ndarray] = []
    portfolio_dd_chunks: list[np.ndarray] = []

    remaining = n_paths
    while remaining > 0:
        size = min(chunk_size, remaining)
        remaining -= size

        simulated = np.asarray(model.simulate(size, horizon, rng), dtype=float)
        cumulative = np.cumsum(simulated, axis=1)
        prices = spot_values.reshape(1, 1, -1) * np.exp(cumulative)
        with_start = np.concatenate(
            [np.broadcast_to(spot_values, (size, 1, len(tickers))), prices], axis=1
        )

        terminal_chunks.append(cumulative[:, -1, :].astype(store))
        drawdown_chunks.append(_max_drawdown_from_prices(with_start).astype(store))

        for ticker in track:
            tracked_chunks[ticker].append(with_start[:, :, tickers.index(ticker)].astype(store))

        if weights_vector is not None:
            values = portfolio_value_paths(simulated, weights_vector, rebalance_every)
            values = values / values[:, 0:1]  # normalize to a starting value of 1.0
            portfolio_chunks.append(values[:, -1].astype(store))
            portfolio_dd_chunks.append(_max_drawdown_from_prices(values).astype(store))

        del simulated, cumulative, prices, with_start

    result = SimulationResult(
        tickers=tickers,
        horizon=horizon,
        n_paths=n_paths,
        spot=spot.reindex(tickers),
        terminal_log_returns=np.concatenate(terminal_chunks, axis=0),
        max_drawdown=np.concatenate(drawdown_chunks, axis=0),
        tracked_paths={t: np.concatenate(v, axis=0) for t, v in tracked_chunks.items()},
        model_name=getattr(model, "name", ""),
    )
    if weights_vector is not None:
        result.portfolio_value = np.concatenate(portfolio_chunks, axis=0)
        result.portfolio_max_drawdown = np.concatenate(portfolio_dd_chunks, axis=0)
        result.portfolio_weights = pd.Series(weights_vector, index=tickers)
    return result


def simulate_terminal_log_returns(
    model: ReturnModel,
    n_paths: int,
    horizon: int,
    rng: np.random.Generator,
    chunk_size: int = 2000,
) -> np.ndarray:
    """Terminal cumulative log returns only, shape ``(n_paths, n_tickers)``.

    The backtester's hot path: it needs the terminal distribution at each origin and
    nothing else, so nothing but the terminal sum is ever materialized.
    """
    chunks: list[np.ndarray] = []
    remaining = n_paths
    while remaining > 0:
        size = min(chunk_size, remaining)
        remaining -= size
        simulated = np.asarray(model.simulate(size, horizon, rng), dtype=float)
        chunks.append(simulated.sum(axis=1))
    return np.concatenate(chunks, axis=0)

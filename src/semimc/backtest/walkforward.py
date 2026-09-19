"""Walk-forward backtester -- the scoreboard.

At each forecast origin: fit on data up to that date only, simulate every horizon, and
score the simulated distribution against what actually happened. Nothing else in this
package is allowed to claim a model is good; this is where that gets decided.

Two design points are worth stating because they are easy to get wrong.

**One fit, every horizon.** A model is fitted once per origin and simulated out to the
longest horizon; the terminal distribution for a shorter horizon is the cumulative sum at
that step of the *same* paths. This is not an approximation -- it is the same stochastic
process observed earlier -- and it makes the backtest roughly four times cheaper.

**Overlapping windows are tracked, not ignored.** Monthly origins with a 252-day horizon
overlap by eleven twelfths, so their errors are strongly dependent. Every per-origin row is
kept, but a non-overlapping subset is marked, and every significance test -- Kupiec,
Christoffersen, Diebold-Mariano -- runs on that subset alone.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..analytics.risk import conditional_value_at_risk, value_at_risk
from ..analytics.scoring import coverage, crps_from_samples, pit_value
from ..data import LookAheadError, MarketData, assert_point_in_time
from ..engine.runner import fit_at_origin

LOGGER = logging.getLogger(__name__)

# LookAheadError/assert_point_in_time now live in data/__init__.py (next to the
# training_window/context cuts they guard) and are re-exported here so existing imports
# from this module -- e.g. tests/test_backtest.py -- keep working unchanged.
__all__ = [
    "BacktestResult",
    "LookAheadError",
    "assert_point_in_time",
    "generate_origins",
    "run_walk_forward",
    "select_non_overlapping",
]


def generate_origins(
    trading_days: pd.DatetimeIndex,
    start: pd.Timestamp,
    frequency: str = "ME",
    end: pd.Timestamp | None = None,
) -> pd.DatetimeIndex:
    """Forecast origins: the last trading day of each period at or after ``start``.

    Parameters
    ----------
    trading_days
        The available sessions.
    start
        Earliest origin.
    frequency
        A pandas offset alias; ``ME`` (month end) is the default.
    end
        Latest origin. Defaults to the last session.

    Returns
    -------
    pd.DatetimeIndex
        Actual trading days, never synthetic period-end dates that markets were shut on.
    """
    index = pd.DatetimeIndex(trading_days).sort_values()
    lower = pd.Timestamp(start).normalize()
    upper = pd.Timestamp(end).normalize() if end is not None else index.max()
    window = index[(index >= lower) & (index <= upper)]
    if len(window) == 0:
        return pd.DatetimeIndex([], name="origin")
    grouped = pd.Series(window, index=window).groupby(pd.Grouper(freq=frequency)).last()
    return pd.DatetimeIndex(grouped.dropna().to_numpy(), name="origin")


def select_non_overlapping(origins: pd.DatetimeIndex, horizon_days: int) -> np.ndarray:
    """Boolean mask marking a greedy maximal set of origins at least ``horizon_days`` apart.

    Trading days are approximated from calendar days with the 252/365.25 ratio, which is
    accurate to a day or two over a year -- ample, since the point is only to stop forecast
    windows overlapping.
    """
    mask = np.zeros(len(origins), dtype=bool)
    if len(origins) == 0:
        return mask
    required = pd.Timedelta(days=int(round(horizon_days * 365.25 / 252)))
    last_kept: pd.Timestamp | None = None
    for position, origin in enumerate(origins):
        if last_kept is None or (origin - last_kept) >= required:
            mask[position] = True
            last_kept = origin
    return mask


@dataclass
class BacktestResult:
    """Per-origin scores plus the bookkeeping needed to interpret them."""

    rows: pd.DataFrame
    failures: pd.DataFrame
    origins: pd.DatetimeIndex
    models: list[str]
    tickers: list[str]
    horizons: list[int]
    baseline: str
    n_paths: int
    seed: int
    elapsed_seconds: float = 0.0
    fit_counts: dict[str, int] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return self.rows.empty


def _realized_outcome(
    prices: pd.Series, origin: pd.Timestamp, horizon: int
) -> tuple[float, float, pd.Timestamp] | None:
    """Realized log return from ``origin`` to ``horizon`` trading days later.

    Returns ``None`` when the price history ends before the horizon does, so an
    incomplete final window is dropped rather than scored against a truncated outcome.
    """
    series = prices.dropna()
    position = series.index.searchsorted(pd.Timestamp(origin), side="right") - 1
    if position < 0 or position + horizon >= len(series):
        return None
    start_price = float(series.iloc[position])
    end_price = float(series.iloc[position + horizon])
    if start_price <= 0 or end_price <= 0:
        return None
    return float(np.log(end_price / start_price)), start_price, series.index[position + horizon]


def run_walk_forward(
    market: MarketData,
    config,  # noqa: ANN001
    models: list[str] | None = None,
    tickers: list[str] | None = None,
    horizons: list[int] | None = None,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    n_paths: int | None = None,
    seed: int | None = None,
    progress: bool = True,
) -> BacktestResult:
    """Run the walk-forward backtest.

    Parameters
    ----------
    market
        Loaded market data. Must have been built with ``for_backtest=True`` so the
        risk-free curve is the historical series.
    config
        Validated configuration.
    models, tickers, horizons
        Defaults come from the config.
    start, end
        Bounds on the forecast origins.
    n_paths
        Paths per origin. Defaults to ``backtest.n_paths`` (fewer than a live forecast,
        as the brief allows).
    seed
        Base seed. Each (origin, model, ticker) draws from an independent child stream, so
        results do not depend on the order cells happen to be evaluated in.
    progress
        Log a line per origin.

    Returns
    -------
    BacktestResult
    """
    started = time.perf_counter()
    model_names = list(models or config.backtest.models)
    symbols = list(tickers or config.universe.tickers)
    horizon_list = sorted(set(horizons or config.horizons))
    paths = int(n_paths or config.backtest.n_paths)
    base_seed = int(seed if seed is not None else config.simulation.seed)
    max_horizon = max(horizon_list)

    available = [t for t in symbols if t in market.prices.columns]
    if not available:
        raise ValueError(f"none of {symbols} are present in the loaded price data")

    origins = generate_origins(
        market.prices.index,
        start=start or config.backtest.start,
        frequency=config.backtest.origin_frequency,
        end=end,
    )
    if len(origins) == 0:
        raise ValueError("no forecast origins in the requested range")

    LOGGER.info(
        "walk-forward: %d origins x %d tickers x %d models, horizons %s, %d paths",
        len(origins),
        len(available),
        len(model_names),
        horizon_list,
        paths,
    )

    records: list[dict] = []
    failures: list[dict] = []
    fit_counts: dict[str, int] = {name: 0 for name in model_names}
    seed_sequence = np.random.SeedSequence(base_seed)

    for origin_index, origin in enumerate(origins):
        context = market.context(origin, for_backtest=True)
        assert_point_in_time(context.benchmark_returns, origin, "benchmark returns")
        # The earnings calendar is deliberately NOT asserted point-in-time: the forward
        # schedule is known at the origin in reality, and models estimate past reactions
        # from `calendar.truncated(as_of)`. See FitContext.truncated for the reasoning.

        for ticker_index, ticker in enumerate(available):
            # Outcomes first: if nothing can be scored at this cell, skip the fit entirely.
            outcomes: dict[int, tuple[float, float, pd.Timestamp]] = {}
            for horizon in horizon_list:
                outcome = _realized_outcome(market.prices[ticker], origin, horizon)
                if outcome is not None:
                    outcomes[horizon] = outcome
            if not outcomes:
                continue

            for model_index, model_name in enumerate(model_names):
                stream = np.random.SeedSequence(
                    entropy=seed_sequence.entropy,
                    spawn_key=(origin_index, ticker_index, model_index),
                )
                rng = np.random.default_rng(stream)

                try:
                    model, _training = fit_at_origin(
                        market, config, model_name, [ticker], origin, context
                    )
                    fit_counts[model_name] += 1
                except Exception as exc:  # noqa: BLE001 - one bad cell must not end the run
                    failures.append(
                        {
                            "origin": origin,
                            "ticker": ticker,
                            "model": model_name,
                            "stage": "fit",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue

                try:
                    simulated = np.asarray(model.simulate(paths, max_horizon, rng), dtype=float)[
                        :, :, 0
                    ]
                    cumulative = np.cumsum(simulated, axis=1)
                except Exception as exc:  # noqa: BLE001
                    failures.append(
                        {
                            "origin": origin,
                            "ticker": ticker,
                            "model": model_name,
                            "stage": "simulate",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue

                for horizon in horizon_list:
                    if horizon not in outcomes:
                        continue
                    realized_log, start_price, end_date = outcomes[horizon]
                    samples_log = cumulative[:, horizon - 1]
                    samples_simple = np.expm1(samples_log)
                    realized_simple = float(np.expm1(realized_log))

                    row: dict = {
                        "origin": origin,
                        "ticker": ticker,
                        "model": model_name,
                        "horizon": horizon,
                        "end_date": end_date,
                        "start_price": start_price,
                        "realized_log_return": realized_log,
                        "realized_simple_return": realized_simple,
                        "median_forecast": float(np.median(samples_simple)),
                        "crps": crps_from_samples(samples_simple, realized_simple),
                        "pit": pit_value(samples_simple, realized_simple, rng),
                        "n_paths": paths,
                    }
                    for level in config.backtest.coverage_levels:
                        hit, lower, upper = coverage(samples_simple, realized_simple, level)
                        tag = f"{int(round(level * 100))}"
                        row[f"hit_{tag}"] = bool(hit)
                        row[f"lower_{tag}"] = lower
                        row[f"upper_{tag}"] = upper
                    for level in config.risk.var_levels:
                        tag = f"{int(round(level * 100))}"
                        var = value_at_risk(samples_simple, level)
                        row[f"var_{tag}"] = var
                        row[f"cvar_{tag}"] = conditional_value_at_risk(samples_simple, level)
                        row[f"exception_{tag}"] = bool(realized_simple < -var)
                    records.append(row)

        if progress and (origin_index % 12 == 0 or origin_index == len(origins) - 1):
            LOGGER.info(
                "  origin %d/%d (%s): %d rows so far",
                origin_index + 1,
                len(origins),
                pd.Timestamp(origin).date(),
                len(records),
            )

    frame = pd.DataFrame(records)
    if not frame.empty:
        frame = frame.sort_values(["model", "ticker", "horizon", "origin"]).reset_index(drop=True)
        marks = []
        for (_, _, horizon), group in frame.groupby(["model", "ticker", "horizon"], sort=False):
            group_origins = pd.DatetimeIndex(group["origin"])
            marks.append(
                pd.Series(select_non_overlapping(group_origins, int(horizon)), index=group.index)
            )
        frame["non_overlapping"] = pd.concat(marks).sort_index()

    return BacktestResult(
        rows=frame,
        failures=pd.DataFrame(failures),
        origins=origins,
        models=model_names,
        tickers=available,
        horizons=horizon_list,
        baseline=config.backtest.baseline,
        n_paths=paths,
        seed=base_seed,
        elapsed_seconds=time.perf_counter() - started,
        fit_counts=fit_counts,
    )

"""Shared test fixtures.

Everything here reads committed CSVs from ``tests/fixtures``. No test touches the network:
the config used by tests sets ``data.provider: csv`` pointed at that directory, so even a
test that accidentally called ``fetch`` would read local files.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from semimc.config import load_config
from semimc.data import MarketData
from semimc.data.cache import PriceCache
from semimc.data.earnings import load_calendar
from semimc.data.provider import CSVProvider
from semimc.data.riskfree import RiskFreeCurve, load_risk_free_series

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_TICKERS = ["NVDA", "AMD", "SPY", "SOXX"]


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def fixture_provider() -> CSVProvider:
    return CSVProvider(FIXTURES)


@pytest.fixture
def config(tmp_path: Path):
    """A config wired to the fixtures, with thresholds scaled to their 2,000 rows."""
    return load_config(
        overrides={
            "data.provider": "csv",
            "data.csv_dir": str(FIXTURES),
            "data.cache_dir": str(tmp_path / "cache"),
            "data.earnings_calendar": str(FIXTURES / "earnings_calendar.csv"),
            "backtest.risk_free_series": str(FIXTURES / "risk_free_3m.csv"),
            "reporting.runs_dir": str(tmp_path / "runs"),
            "universe.groups": {"fixture": ["NVDA", "AMD"]},
            "fit.min_observations": 300,
            "fit.window_years": 4,
            "simulation.n_paths": 500,
            "simulation.dtype": "float64",
            "backtest.n_paths": 400,
            "backtest.models": ["gbm", "bootstrap"],
        }
    )


@pytest.fixture
def prices() -> pd.DataFrame:
    """Wide adjusted-close frame for the fixture universe."""
    frames = {}
    for ticker in FIXTURE_TICKERS:
        frame = pd.read_csv(FIXTURES / f"{ticker}.csv", parse_dates=["date"])
        frames[ticker] = frame.set_index("date")["adj_close"]
    return pd.DataFrame(frames)


@pytest.fixture
def volumes() -> pd.DataFrame:
    frames = {}
    for ticker in FIXTURE_TICKERS:
        frame = pd.read_csv(FIXTURES / f"{ticker}.csv", parse_dates=["date"])
        frames[ticker] = frame.set_index("date")["volume"]
    return pd.DataFrame(frames)


@pytest.fixture
def ohlc() -> dict[str, pd.DataFrame]:
    """Adjusted OHLC frames for the fixture universe, keyed like ``PriceCache.ohlc``."""
    columns = {"open": "adj_open", "high": "adj_high", "low": "adj_low", "close": "adj_close"}
    out: dict[str, pd.DataFrame] = {}
    for key, column in columns.items():
        frames = {}
        for ticker in FIXTURE_TICKERS:
            frame = pd.read_csv(FIXTURES / f"{ticker}.csv", parse_dates=["date"])
            frames[ticker] = frame.set_index("date")[column]
        out[key] = pd.DataFrame(frames)
    return out


@pytest.fixture
def market(config, prices, volumes, ohlc) -> MarketData:
    """A :class:`MarketData` built straight from the fixture frames."""
    return MarketData(
        prices=prices,
        volumes=volumes,
        config=config,
        risk_free=RiskFreeCurve(
            load_risk_free_series(FIXTURES / "risk_free_3m.csv"), 0.042, expect_point_in_time=True
        ),
        earnings=load_calendar(FIXTURES / "earnings_calendar.csv"),
        ohlc=ohlc,
    )


@pytest.fixture
def populated_cache(config, fixture_provider, tmp_path) -> PriceCache:
    """A parquet cache populated from the CSV fixtures."""
    cache = PriceCache(tmp_path / "cache")
    cache.refresh(fixture_provider, FIXTURE_TICKERS, start=pd.Timestamp("2000-01-01").date())
    return cache


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(12345)

"""Configuration schema and loader.

The YAML in ``config/default.yaml`` is validated into the :class:`Config` tree below.
Validation is strict (``extra="forbid"``) so a typo in a key is an error rather than a
silently ignored setting -- a misspelled ``drift.policy`` that quietly fell back to a
default would invalidate every downstream result.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UniverseConfig(_Base):
    groups: dict[str, list[str]]

    @property
    def tickers(self) -> list[str]:
        """Flat, de-duplicated ticker list in declaration order."""
        seen: dict[str, None] = {}
        for members in self.groups.values():
            for ticker in members:
                seen.setdefault(ticker, None)
        return list(seen)

    def group_of(self, ticker: str) -> str:
        for name, members in self.groups.items():
            if ticker in members:
                return name
        return "unclassified"


class BenchmarkConfig(_Base):
    market: str = "SPY"
    sector: str = "SOXX"

    @property
    def tickers(self) -> list[str]:
        return [self.market, self.sector]


class SimulationConfig(_Base):
    n_paths: int = Field(10_000, gt=0)
    seed: int = 42
    chunk_size: int = Field(2_000, gt=0)
    dtype: Literal["float32", "float64"] = "float32"
    trading_days_per_year: int = Field(252, gt=0)


class FitConfig(_Base):
    window_years: float = Field(5.0, gt=0)
    min_observations: int = Field(750, gt=0)
    vol_estimator: Literal["realized", "ewma"] = "realized"
    vol_window_days: int = Field(252, gt=1)
    ewma_lambda: float = Field(0.94, gt=0, lt=1)

    @property
    def window_days(self) -> int:
        """Fit window expressed in calendar days (used to slice the price history)."""
        return int(round(self.window_years * 365.25))


class DriftConfig(_Base):
    policy: Literal["capm", "risk_free", "shrunk_historical", "manual"] = "capm"
    equity_risk_premium: float = 0.05
    risk_free_rate: float = 0.042
    shrinkage_weight: float = Field(0.25, ge=0, le=1)
    manual_annual_return: dict[str, float] = Field(default_factory=dict)
    beta_benchmark: Literal["market", "sector"] = "market"


class RiskConfig(_Base):
    var_levels: list[float] = Field(default_factory=lambda: [0.95, 0.99])
    loss_threshold: float = Field(0.20, gt=0, lt=1)

    @field_validator("var_levels")
    @classmethod
    def _check_levels(cls, value: list[float]) -> list[float]:
        if not value or any(not 0 < v < 1 for v in value):
            raise ValueError("var_levels must be strictly between 0 and 1")
        return value


class DataConfig(_Base):
    provider: Literal["yfinance", "csv"] = "yfinance"
    cache_dir: str = "data_cache"
    csv_dir: str = "data_csv"
    start: dt.date = dt.date(2000, 1, 1)
    max_staleness_days: int = Field(1, ge=0)
    price_field: Literal["adj_close"] = "adj_close"
    outlier_sigma: float = Field(8.0, gt=0)
    max_gap_days: int = Field(5, gt=0)
    earnings_calendar: str = "config/earnings_calendar.csv"


class BacktestConfig(_Base):
    origin_frequency: str = "ME"
    n_paths: int = Field(2_000, gt=0)
    start: dt.date = dt.date(2012, 1, 1)
    models: list[str] = Field(default_factory=lambda: ["gbm", "bootstrap", "garch_t"])
    baseline: str = "gbm"
    coverage_levels: list[float] = Field(default_factory=lambda: [0.50, 0.90])
    pit_bins: int = Field(10, gt=1)
    cache_fits: bool = True
    n_dependence_bootstraps: int = Field(2_000, gt=0)
    risk_free_series: str = "config/risk_free_3m.csv"

    @model_validator(mode="after")
    def _baseline_in_models(self) -> BacktestConfig:
        if self.baseline not in self.models:
            raise ValueError(
                f"backtest.baseline {self.baseline!r} must be one of backtest.models {self.models}"
            )
        return self


class BootstrapModelConfig(_Base):
    block_length: int | Literal["auto"] = "auto"


class GarchModelConfig(_Base):
    p: int = Field(1, ge=1)
    o: int = Field(1, ge=0)
    q: int = Field(1, ge=1)
    rescale_factor: float = Field(100.0, gt=0)


class EarningsModelConfig(_Base):
    base_model: str = "garch_t"
    min_earnings_observations: int = Field(8, ge=2)
    strip_from_training: bool = True


class FactorModelConfig(_Base):
    idio_model: str = "garch_t"


class FHSModelConfig(_Base):
    block_length: int = Field(1, ge=1)


class MertonModelConfig(_Base):
    max_jumps_per_day: int = Field(3, ge=1, le=10)
    exclude_earnings_days: bool = True


class HARModelConfig(_Base):
    horizons: list[int] = Field(default_factory=lambda: [1, 21, 63, 126, 252])
    estimator: Literal["garman_klass", "rogers_satchell"] = "garman_klass"
    innovations: Literal["bootstrap", "normal"] = "bootstrap"
    exclude_earnings_variance: bool = True


class RegimeModelConfig(_Base):
    search_reps: int = Field(20, ge=1)
    switching_mean: bool = False
    min_expected_duration_days: float = Field(15.0, gt=0)


class BlendModelConfig(_Base):
    components: list[str] = Field(default_factory=lambda: ["gbm", "garch_t"])
    weights: dict[str, float] = Field(default_factory=dict)  # empty = equal weight


class ModelsConfig(_Base):
    bootstrap: BootstrapModelConfig = Field(default_factory=BootstrapModelConfig)
    garch_t: GarchModelConfig = Field(default_factory=GarchModelConfig)
    earnings_jumps: EarningsModelConfig = Field(default_factory=EarningsModelConfig)
    factor: FactorModelConfig = Field(default_factory=FactorModelConfig)
    fhs: FHSModelConfig = Field(default_factory=FHSModelConfig)
    merton: MertonModelConfig = Field(default_factory=MertonModelConfig)
    har: HARModelConfig = Field(default_factory=HARModelConfig)
    regime: RegimeModelConfig = Field(default_factory=RegimeModelConfig)
    blend: BlendModelConfig = Field(default_factory=BlendModelConfig)


class SignalsConfig(_Base):
    """Addendum Phase 4B: point-in-time signals and the regression-based drift tilt.

    Defaults follow Section 3 of the addendum, with two decisions made explicitly rather
    than silently: the industry-cycle series is FRED's ``IPG3344S`` (semiconductor and
    related electronic component manufacturing, industrial production index) rather than
    TSMC/SIA, because those need scraping (an ask-first boundary) and FRED needs neither a
    key nor a scraper; and the holdout, while still 24 months as specified, reports its
    headline verdict at 21 days only -- the addendum's own numbers imply roughly 8
    non-overlapping 63-day observations in 24 months, too few to carry a conclusion. The
    63-day holdout cell is still computed and written out, flagged ``low_power``.
    """

    horizons: list[int] = Field(default_factory=lambda: [21, 63])
    holdout_months: int = Field(24, gt=0)
    holdout_headline_horizon: int = 21
    shrinkage_lambda: float = Field(0.5, ge=0, le=1)
    tilt_cap_annual: float = Field(0.10, gt=0, le=2)
    n_coefficient_bootstraps: int = Field(500, gt=0)
    momentum_lookback_days: int = Field(252, gt=0)  # ~12 months
    momentum_skip_days: int = Field(21, gt=0)  # most recent month, used separately as reversal
    relative_strength_lookback_days: int = Field(126, gt=0)  # ~6 months, vs. sector benchmark
    earnings_drift_active_days: int = Field(60, gt=0)
    industry_series_id: str = "IPG3344S"
    industry_publication_lag_days: int = Field(45, ge=0)
    customer_tickers: list[str] = Field(
        default_factory=lambda: [
            "AAPL",
            "MSFT",
            "GOOGL",
            "AMZN",
            "META",
            "TSLA",
            "DELL",
            "HPQ",
            "CSCO",
        ]
    )
    supply_chain_links: str = "data/external/supply_chain_links.csv"
    external_dir: str = "data/external"
    driscoll_kraay_lag_per_horizon: float = Field(1.0, gt=0)  # maxlags = ceil(lag * H / 21)
    min_regression_observations: int = Field(60, gt=0)
    cv_n_folds: int = Field(5, ge=2)
    cv_gap_periods: int = Field(1, ge=1)
    ridge_lasso_alphas: list[float] = Field(
        default_factory=lambda: [0.03, 0.1, 0.3, 1.0, 3.0, 10.0, 30.0]
    )
    quantile_levels: list[float] = Field(default_factory=lambda: [0.05, 0.25, 0.5, 0.75, 0.95])
    # Resolved via Config.path(), same as every other config-relative path, so tests can
    # redirect it with an absolute -s override instead of writing into the real repo.
    research_log: str = "research_log.md"


class PortfolioConfig(_Base):
    weights: dict[str, float] = Field(default_factory=dict)
    rebalance: Literal["none", "monthly", "quarterly"] = "none"


class OptionsConfig(_Base):
    """Brief §6 stretch item: `simulate --implied-vol`. Live-only -- see
    `data/options.py`'s module docstring for why this has no backtest counterpart."""

    moneyness_band: float = Field(0.10, gt=0, le=1)  # strikes within +/- this of spot
    max_expiries: int = Field(6, gt=0)  # nearest listed expiries to fetch and consider
    min_quotes_per_expiry: int = Field(2, ge=1)  # below this, an expiry's vol is NaN
    dividend_yield: float = Field(0.0, ge=0, le=1)  # continuous yield used in Black-Scholes


class ReportingConfig(_Base):
    fan_percentiles: list[int] = Field(default_factory=lambda: [5, 25, 50, 75, 95])
    figure_dpi: int = Field(120, gt=0)
    runs_dir: str = "runs"


class Config(_Base):
    universe: UniverseConfig
    benchmarks: BenchmarkConfig = Field(default_factory=BenchmarkConfig)
    horizons: list[int] = Field(default_factory=lambda: [21, 63, 126, 252])
    simulation: SimulationConfig = Field(default_factory=SimulationConfig)
    fit: FitConfig = Field(default_factory=FitConfig)
    drift: DriftConfig = Field(default_factory=DriftConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    signals: SignalsConfig = Field(default_factory=SignalsConfig)
    portfolio: PortfolioConfig = Field(default_factory=PortfolioConfig)
    options: OptionsConfig = Field(default_factory=OptionsConfig)
    scenarios: dict[str, dict[str, Any]] = Field(default_factory=dict)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)

    project_root: Path = Field(default=PROJECT_ROOT, exclude=True)

    @field_validator("horizons")
    @classmethod
    def _positive_horizons(cls, value: list[int]) -> list[int]:
        if not value or any(h <= 0 for h in value):
            raise ValueError("horizons must be positive trading-day counts")
        return sorted(set(value))

    @model_validator(mode="after")
    def _manual_drift_complete(self) -> Config:
        if self.drift.policy == "manual":
            missing = [t for t in self.universe.tickers if t not in self.drift.manual_annual_return]
            if missing:
                raise ValueError(
                    "drift.policy = manual requires drift.manual_annual_return for every "
                    f"ticker; missing: {', '.join(missing)}"
                )
        return self

    # -- convenience ----------------------------------------------------------------

    @property
    def all_tickers(self) -> list[str]:
        """Universe plus benchmarks, de-duplicated, universe first."""
        out = list(self.universe.tickers)
        for ticker in self.benchmarks.tickers:
            if ticker not in out:
                out.append(ticker)
        return out

    def path(self, relative: str) -> Path:
        """Resolve a config-relative path against the project root."""
        candidate = Path(relative)
        return candidate if candidate.is_absolute() else self.project_root / candidate

    @property
    def cache_path(self) -> Path:
        return self.path(self.data.cache_dir)

    @property
    def runs_path(self) -> Path:
        return self.path(self.reporting.runs_dir)

    def to_dict(self) -> dict[str, Any]:
        """JSON/YAML-safe snapshot, used for run manifests."""
        return self.model_dump(mode="json")


def _coerce_scalar(text: str) -> Any:
    """Parse a CLI override value with YAML rules (so ``true``/``3``/``[1,2]`` work)."""
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def _apply_override(tree: dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = dotted_key.split(".")
    node = tree
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def load_config(
    path: str | Path | None = None,
    overrides: dict[str, Any] | list[str] | None = None,
    project_root: Path | None = None,
) -> Config:
    """Load and validate configuration.

    Parameters
    ----------
    path
        YAML file to read. Defaults to ``config/default.yaml``.
    overrides
        Either a mapping of dotted keys to values, or a list of ``key.path=value``
        strings as passed by the CLI's ``--set`` flag.
    project_root
        Root that relative paths in the config resolve against. Defaults to the
        repository root.

    Returns
    -------
    Config
        Validated, frozen configuration tree.
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    if overrides:
        items = (
            {k: _coerce_scalar(v) for k, v in (s.split("=", 1) for s in overrides)}
            if isinstance(overrides, list)
            else overrides
        )
        for key, value in items.items():
            _apply_override(raw, key, value)

    raw["project_root"] = project_root or config_path.resolve().parent.parent
    return Config.model_validate(raw)

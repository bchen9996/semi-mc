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


class JournalVariant(_Base):
    """One (model, drift policy) pair the forecast journal records."""

    model: str
    drift_policy: Literal["capm", "risk_free", "shrunk_historical"]

    @property
    def label(self) -> str:
        return f"{self.model}/{self.drift_policy}"


def _default_journal_variants() -> list[JournalVariant]:
    return [
        JournalVariant(model="gbm", drift_policy="shrunk_historical"),
        JournalVariant(model="bootstrap", drift_policy="shrunk_historical"),
        JournalVariant(model="garch_t", drift_policy="shrunk_historical"),
        JournalVariant(model="garch_t", drift_policy="capm"),
        JournalVariant(model="garch_t", drift_policy="risk_free"),
    ]


class JournalConfig(_Base):
    """The forward forecast journal (`journal.py`, added 2026-09-27): distributions
    recorded *before* their outcomes exist, scored once the horizon passes -- the only
    evidence about these models that is not contaminated by the 2012-2026 backtest
    `drift.policy` was chosen on (MODEL_NOTES section 7), and the Phase 4B holdout is
    already spent.

    The default variants put the drift question to a clean test: garch_t under all three
    non-manual policies, plus gbm (the scoring baseline, as in `backtest.baseline`) and
    bootstrap under the configured default. `manual` is excluded -- it has no
    out-of-sample meaning. No staleness setting: `journal.py` refuses any record run
    after the next session could have opened, the exact contamination test.
    """

    path: str = "journal/forecasts.csv"
    scores_dir: str = "journal/scores"
    variants: list[JournalVariant] = Field(default_factory=_default_journal_variants)
    baseline: JournalVariant = Field(
        default_factory=lambda: JournalVariant(model="gbm", drift_policy="shrunk_historical")
    )
    n_paths: int = Field(5_000, gt=0)

    @model_validator(mode="after")
    def _baseline_in_variants(self) -> JournalConfig:
        if self.baseline not in self.variants:
            raise ValueError(
                f"journal.baseline {self.baseline.label!r} must be one of journal.variants"
            )
        labels = [v.label for v in self.variants]
        if len(labels) != len(set(labels)):
            raise ValueError(f"journal.variants has duplicates: {labels}")
        return self


class BootstrapModelConfig(_Base):
    block_length: int | Literal["auto"] = "auto"


class GarchModelConfig(_Base):
    p: int = Field(1, ge=1)
    o: int = Field(1, ge=0)
    q: int = Field(1, ge=1)
    rescale_factor: float = Field(100.0, gt=0)
    # Caps each simulated conditional variance at this multiple of the fit's highest
    # in-sample conditional variance; 0 disables. Added 2026-09-27 after explosive
    # long-horizon paths (nu < 4, or persistence at 1) -- MODEL_NOTES section 3. The 2.0
    # was fixed before any backtest was run with it, not tuned on one.
    variance_cap_multiple: float = Field(2.0, ge=0)


class GarchParamUncertaintyConfig(_Base):
    """`garch_t_pu` (MODEL_NOTES section 18): `garch_t` with each path drawing its own
    parameters from the fit's asymptotic covariance. Orders and rescaling are shared
    with `models.garch_t`. `covariance_scale` multiplies that covariance (0 reproduces
    `garch_t` exactly); `max_redraws` bounds the rejection of invalid draws."""

    covariance_scale: float = Field(1.0, ge=0)
    max_redraws: int = Field(10, ge=0)


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
    garch_t_pu: GarchParamUncertaintyConfig = Field(default_factory=GarchParamUncertaintyConfig)
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
    # Pandas offset alias for panel origins ("ME", "W-FRI", ...). Mirrors
    # BacktestConfig.origin_frequency exactly -- same untyped str, same reliance on pandas
    # itself to reject a bad alias. Monthly by default; a shorter/higher-frequency
    # evaluation is opt-in via --set, never a changed default.
    origin_frequency: str = "ME"
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


class PaperSleeveConfig(_Base):
    """One sleeve's live-trading switch. Defaults off, per the paper-trading brief's own
    rule: every sleeve starts disabled and is turned on deliberately, never by default."""

    trading_enabled: bool = False
    # Pandas offset alias, same convention as BacktestConfig.origin_frequency/
    # SignalsConfig.origin_frequency. live/decide.py::run_decide uses this to no-op a
    # decide call that lands inside a period this sleeve has already decided, so a daily
    # schedule can safely drive a monthly-rebalance sleeve without cron needing to land on
    # exactly the right trading day.
    rebalance_frequency: str = "ME"


class PaperExecutionConfig(_Base):
    start_time: dt.time = dt.time(10, 0)
    cutoff_time: dt.time = dt.time(15, 30)
    timezone: str = "America/New_York"
    collar_bps: float = Field(20.0, ge=0)
    reprice_interval_minutes: int = Field(15, gt=0)


class PaperReconciliationConfig(_Base):
    position_tolerance_shares: float = Field(1e-6, ge=0)
    cash_tolerance_usd: float = Field(1.0, ge=0)


class PaperEvaluationConfig(_Base):
    """`analytics/paper_evaluation.py` (Phase 6, T2/T3): the paired block-bootstrap this
    project uses everywhere daily observations are serially dependent -- same reasoning as
    `signals.n_coefficient_bootstraps`/`backtest.n_dependence_bootstraps`, a separate
    section since it resamples calendar-day blocks of a single equity curve, not
    cross-sectional origins."""

    n_bootstrap: int = Field(2000, gt=0)
    # ~1 trading month -- the same cadence this project already treats as a natural unit
    # elsewhere (origin_frequency's monthly default), long enough to catch a few days of
    # serial correlation in daily returns without shrinking the effective sample size too
    # far for the history paper trading will realistically have in its first year.
    block_length_days: int = Field(21, gt=0)


class PaperConfig(_Base):
    """Paper-trading defaults (`semis_paper_trading_brief.md`).

    Scoped for now to what S0 (equal-weight control) and S3 (vol-targeted control) need --
    the brief's S1/S2 sleeves have no code path yet: the addendum's Phase 4B holdout came
    back null (research_log.md, 2026-09-16), and "paper trading can't rescue a signal that
    failed it" is the brief's own rule. Extending this schema for S1/S2 is future work, not
    a gap in this one.
    """

    sleeves: dict[str, PaperSleeveConfig] = Field(
        default_factory=lambda: {"s0": PaperSleeveConfig(), "s3": PaperSleeveConfig()}
    )
    execution: PaperExecutionConfig = Field(default_factory=PaperExecutionConfig)
    reconciliation: PaperReconciliationConfig = Field(default_factory=PaperReconciliationConfig)
    evaluation: PaperEvaluationConfig = Field(default_factory=PaperEvaluationConfig)
    minimum_trade_pct: float = Field(0.005, ge=0, lt=1)
    s3_target_vol: float = Field(0.25, gt=0)
    s3_max_leverage: float = Field(1.0, gt=0)
    # T4 (shadow sector-timing, strategy/decide.py::decide_sector_shadow): fraction of
    # gross exposure the sector-tilt regression's forecast can add or remove at its most
    # extreme (a one-residual-standard-deviation forecast), capped by s3_max_leverage the
    # same way S3 is. Shadow-only -- no sleeve config exists to make this tradeable.
    sector_tilt_strength: float = Field(0.15, ge=0, le=1)
    review_months: list[int] = Field(default_factory=lambda: [3, 6, 12])
    # Paper-only guard: at startup, the account number in use must be in this list. Empty
    # by default -- nothing is a registered trading account until deliberately added.
    registered_accounts: list[str] = Field(default_factory=list)
    # execution/pretrade.py::check_kill_switch. The file's mere existence halts all
    # trading; resolved like every other config-relative path (Config.path()) rather than
    # hand-rolled off project_root -- see CLAUDE.md's research_log gotcha for why.
    kill_switch_path: str = "runs/paper/KILL_SWITCH"
    # live/ledger.py::Ledger. Deliberately NOT under runs/ (reporting.runs_dir) -- runs/ is
    # entirely gitignored and git cannot re-include a file inside an excluded parent
    # directory, but the ledger is the one piece of state that must survive a stateless
    # CI runner between scheduled jobs. Resolved via Config.path() like every other
    # config-relative path.
    ledger_path: str = "ledger/paper.sqlite3"


class IntradayBudgetConfig(_Base):
    """Per-call and per-day hard stops on LLM spend (BRIEF_INTRADAY.md).

    ``max_cost_usd_per_day`` is a placeholder, not a researched figure -- the brief's own
    instruction is to recalibrate it from real shadow-mode logging (`intraday/budget.py`)
    rather than guess a number up front.
    """

    max_cost_usd_per_call: float = Field(1.0, gt=0)
    max_cost_usd_per_day: float = Field(10.0, gt=0)


class IntradayRiskConfig(_Base):
    """Trading budget and risk limits (BRIEF_INTRADAY.md's "Risk limits and trading
    budget" and "Defaults" tables) -- kept in code, never adjustable by the agent itself.
    """

    new_positions_per_day: int = Field(6, gt=0)
    round_trips_per_symbol_per_day: int = Field(2, gt=0)
    daily_turnover_pct: float = Field(1.50, gt=0)
    risk_per_trade_pct: float = Field(0.0075, gt=0)
    single_name_cap_pct: float = Field(0.15, gt=0)
    gross_cap_pct: float = Field(1.50, gt=0)
    net_long_cap_pct: float = Field(1.00, gt=0)
    daily_loss_soft_pct: float = Field(0.02, gt=0)
    daily_loss_hard_pct: float = Field(0.03, gt=0)
    cooldown_minutes_after_stopout: int = Field(30, ge=0)
    overnight_gross_cap_pct: float = Field(1.00, gt=0)
    min_edge_multiple: float = Field(2.0, gt=0)
    equity_cost_bps_per_side: float = Field(8.0, ge=0)
    # Phase 5 (jobs.py): "default to two tranches for a full-size position" (brief).
    entry_tranches: int = Field(2, ge=1)
    # Phase 5's spread/staleness gate (intraday/pretrade.py::check_spread_gate). The
    # brief names both as configurable but gives no numeric default -- these are a
    # starting guess, not a researched figure, same caveat as max_cost_usd_per_day.
    max_spread_bps: float = Field(50.0, gt=0)
    max_quote_age_seconds: float = Field(30.0, gt=0)


class IntradayScheduleConfig(_Base):
    timezone: str = "America/Chicago"
    cycle_interval_minutes: int = Field(20, gt=0)


class IntradayOptionsConfig(_Base):
    """Both gates (account level, data feed) are unresolved -- see docs/alpaca_notes.md.
    `enabled` stays False through the equities-only rollout stages regardless of either
    gate -- real order submission is Stage 5, owner-approval-only (BRIEF_INTRADAY.md's
    "Ask first: enabling options").

    `shadow_pricing` is Stage 4's own, independent switch: the Trader may propose options
    and they get priced, gated, and logged, but `intraday/jobs.py::run_cycle` never
    reaches order submission for one while `enabled` is False, regardless of gate outcome
    -- the same `trading_enabled`/`shadow_mode` two-flag shape Phase 11 already uses for
    "may this run at all" vs. "does this run touch the real broker."
    """

    enabled: bool = False
    shadow_pricing: bool = False
    # BRIEF_INTRADAY.md's Defaults table gives these two numbers directly.
    premium_at_risk_pct_per_position: float = Field(0.005, gt=0)
    premium_at_risk_pct_total: float = Field(0.02, gt=0)
    min_dte: int = Field(7, ge=0)
    max_dte: int = Field(60, gt=0)
    close_at_dte: int = Field(2, ge=0)
    # The brief states these narratively (liquidity filters, a net delta cap, a daily
    # theta cap) but gives no numeric default for any of them -- a starting guess, not a
    # researched figure, same caveat as IntradayRiskConfig.max_spread_bps. Recalibrate
    # from real Stage-4 shadow-pricing logging.
    min_open_interest: int = Field(50, ge=0)
    min_volume: int = Field(10, ge=0)
    max_spread_pct_of_mid: float = Field(0.15, gt=0)
    net_delta_cap: float = Field(0.30, gt=0)
    daily_theta_cap: float = Field(0.005, gt=0)
    # "only buy premium when forecast volatility exceeds implied by a margin" -- the
    # margin itself (in annualized vol points, e.g. 0.05 = 5 vol points) has no brief-given
    # number either. Applies to long_call/long_put only; debit_vertical is the brief's own
    # accepted way to trade richer implied vol, never gated on this.
    min_forecast_vol_premium_over_iv: float = Field(0.05, ge=0)
    # "half the spread plus 5 bps for options" (Defaults table), vs.
    # IntradayRiskConfig.equity_cost_bps_per_side's flat 8bps for equities.
    cost_model_spread_fraction: float = Field(0.5, gt=0)
    cost_model_extra_bps: float = Field(5.0, ge=0)


class IntradayModelsConfig(_Base):
    """Which model each role calls, and the call-shape caps passed to `claude -p --bare`
    (`intraday/llm.py`). Plain `str`, not a `Literal`, for the model fields -- Claude Code
    resolves aliases like "sonnet"/"haiku" to whatever the latest model in that family is,
    and a `Literal` here would need a code change every time a new model ships. `trader`
    defaults to the cheapest model (`~20 calls/day`); `researcher`/`reviewer` default to a
    stronger model since they run only twice a day -- matching the cost estimate already
    given for S4 (BRIEF_INTRADAY.md's "Agent runtime and cost control": "pick models per
    role accordingly").
    """

    researcher: str = "sonnet"
    trader: str = "haiku"
    reviewer: str = "sonnet"
    max_turns: int = Field(6, gt=0)
    # A wall-clock backstop alongside --max-turns/--max-budget-usd, since an unattended
    # GH Actions job has no one to notice a hung call.
    timeout_seconds: int = Field(300, gt=0)


class IntradayResearchConfig(_Base):
    """Knobs for the Researcher's overnight news pull and `preopen`'s headline refresh
    (Phase 6, BRIEF_INTRADAY.md's `research`/`preopen` rows). Numeric defaults are starting
    guesses, not researched figures -- same caveat as `IntradayRiskConfig.max_spread_bps`.
    A flat lookback window rather than exact calendar-derived "prior close" arithmetic is a
    deliberate simplification: `IntradayLedger.headline_already_seen` dedup makes
    overlapping windows across calls harmless, the same "fixed offset, documented drift,
    safe by construction" reasoning Phase 5 already used for cron timing.
    """

    news_lookback_hours: float = Field(16.0, gt=0)
    preopen_lookback_hours: float = Field(2.0, gt=0)
    earnings_lookahead_days: int = Field(5, gt=0)
    max_headlines_per_symbol: int = Field(5, gt=0)


class IntradaySessionDataConfig(_Base):
    """The Trader's `session` section (`intraday/session.py`, added 2026-09-30): bar
    size, how long the opening range runs, and how many earlier sessions set "normal"
    volume for the time of day. Starting choices, not researched figures, same caveat as
    `IntradayRiskConfig.max_spread_bps`. 30 minutes matches the Researcher's own habit of
    writing "after the first 15-30 minutes" triggers; 10 sessions is two weeks.
    """

    bar_minutes: int = Field(5, gt=0)
    opening_range_minutes: int = Field(30, gt=0)
    volume_lookback_sessions: int = Field(10, gt=0)


class IntradayPlaybookConfig(_Base):
    """`PLAYBOOK.md`'s accept/reject/retire knobs (`BRIEF_INTRADAY.md`'s "Self-improvement
    rules" and its "Playbook change threshold" default), Phase 10.
    `evidence_min_trading_days`/`evidence_min_trades` are the brief's own named default
    (10 trading days or 20 relevant trades); `review_after_days` has no brief-given
    number -- a placeholder, not a researched figure, same caveat as
    `IntradayRiskConfig.max_spread_bps`. Calendar days, not trading-session-precise, per
    `intraday/playbook.py`'s own disclosed simplification.
    """

    evidence_min_trading_days: int = Field(10, gt=0)
    evidence_min_trades: int = Field(20, gt=0)
    review_after_days: int = Field(30, gt=0)


class IntradayPlaceboConfig(_Base):
    """T6's control group (`THESES.md`, `BRIEF_INTRADAY.md`'s "Placebo agent"), Phase 7:
    identical sizing, execution, and risk controls as the real S4 loop, but entries drawn
    at random from the morning watchlist instead of the Trader's judgment. `
    stop_distance_pct`/`time_stop_minutes` stand in for what a real thesis would otherwise
    supply per candidate -- placeholders, not researched figures, same caveat as
    `IntradayRiskConfig.max_spread_bps`. `starting_equity` seeds the shadow book once;
    every equity/cash figure after that is derived
    (`IntradayLedger.total_realized_cash_flow`), never itself persisted.
    """

    # 0.02: at the default risk_per_trade_pct (0.0075) and single_name_cap_pct (0.15),
    # this is the smallest stop distance where risk_per_trade_pct / stop_distance_pct
    # alone doesn't already exceed single_name_cap_pct -- placebo.py's own sizing caps
    # against both up front, rather than relying on validate_proposal to reject an
    # over-concentrated size every single cycle.
    stop_distance_pct: float = Field(0.02, gt=0)
    time_stop_minutes: int = Field(120, gt=0)
    starting_equity: float = Field(100_000.0, gt=0)


class IntradayReadinessConfig(_Base):
    """The Stage 1 -> Stage 2 "clean trading day" rule (`intraday/readiness.py`), an owner
    decision of 2026-09-29 that replaced the brief's fixed "shadow, 2 weeks": promote after
    `clean_days_required` consecutive clean days if the streak includes accepted
    proposals, else after `clean_days_if_sparse`. Stage 2 is still paper money, so the
    shadow stage's job is proving the plumbing, not the strategy (tasks/todo.md).

    The minute thresholds follow the schedule, not research: cycles run every 20 minutes,
    so a 30-minute gap means one missed trigger; the open batch fires at 08:32 CT, so 10
    minutes of grace covers runner setup. `excluded_days` is the owner's written record of
    days that neither count nor reset the streak -- market holidays (this module has no
    market calendar) and outages the owner judged outside the project's control.
    """

    max_cycle_gap_minutes: float = Field(30.0, gt=0)
    open_batch_grace_minutes: float = Field(10.0, ge=0)
    session_edge_minutes: float = Field(30.0, ge=0)
    clean_days_required: int = Field(3, ge=1)
    clean_days_if_sparse: int = Field(5, ge=1)
    excluded_days: list[dt.date] = Field(default_factory=list)


class IntradayConfig(_Base):
    """S4: the LLM-driven intraday agent sleeve (BRIEF_INTRADAY.md). Deliberately not a
    `PaperConfig.sleeves` entry -- its LLM-proposed trades don't fit `Decision.weights`'s
    closed-form target-weight shape (`strategy/decide.py`), and it trades its own,
    separate Alpaca paper account rather than the one `paper.registered_accounts` guards.
    """

    trading_enabled: bool = False
    # S4's OWN paper account number -- never the same account paper.registered_accounts
    # guards. execution/alpaca_broker.py::AlpacaPaperBroker.from_env reads a distinct
    # ALPACA_S4_API_KEY/ALPACA_S4_SECRET_KEY pair for this sleeve.
    registered_account: str | None = None
    # Rollout Stage 1 (Phase 11, BRIEF_INTRADAY.md's "Shadow, 2 weeks"): `True` (the
    # safe default, matching every other go-live flag here) means the real loop may run
    # and propose/validate for real, but fills against a `FakeBroker` (cli.py) rather
    # than submitting to Alpaca -- never flip this off in the same action as
    # `trading_enabled`; they're deliberately independent switches (see
    # `intraday/pretrade.py::check_intraday_pretrade`'s own `shadow` parameter).
    shadow_mode: bool = True
    # The real loop's own Stage-1 shadow book size -- the same role
    # `IntradayPlaceboConfig.starting_equity` plays for the placebo's book. A separate
    # field, not reused, since the two shadow books are logically distinct even though
    # both happen to default to the same figure.
    shadow_starting_equity: float = 100_000.0
    budget: IntradayBudgetConfig = Field(default_factory=IntradayBudgetConfig)
    risk: IntradayRiskConfig = Field(default_factory=IntradayRiskConfig)
    schedule: IntradayScheduleConfig = Field(default_factory=IntradayScheduleConfig)
    options: IntradayOptionsConfig = Field(default_factory=IntradayOptionsConfig)
    models: IntradayModelsConfig = Field(default_factory=IntradayModelsConfig)
    research: IntradayResearchConfig = Field(default_factory=IntradayResearchConfig)
    session_data: IntradaySessionDataConfig = Field(default_factory=IntradaySessionDataConfig)
    placebo: IntradayPlaceboConfig = Field(default_factory=IntradayPlaceboConfig)
    playbook: IntradayPlaybookConfig = Field(default_factory=IntradayPlaybookConfig)
    readiness: IntradayReadinessConfig = Field(default_factory=IntradayReadinessConfig)
    # Separate sentinel from paper.kill_switch_path -- S4 can be halted independently of
    # s0/s3 and vice versa.
    kill_switch_path: str = "runs/intraday/KILL_SWITCH"
    # Separate ledger file from paper.ledger_path: S4 writes roughly every 20 minutes all
    # day, versus s0/s3's monthly-decide-plus-half-hourly-mark cadence. Sharing one file
    # would mean every S4 cycle's git commit contends with s0/s3's commits on the exact
    # same file -- the push-race class of bug Phase 8 already had to fix once, at a much
    # lower write frequency than S4's.
    ledger_path: str = "ledger/intraday.sqlite3"
    # The placebo's own ledger (Phase 7) -- a second file, for the same reason ledger_path
    # is already separate from paper.ledger_path: its own git commits must never contend
    # with the real loop's commits on the same file.
    shadow_ledger_path: str = "ledger/intraday_shadow.sqlite3"
    # `eod_review`'s report output (Phase 9a) -- config-relative, resolved via
    # `Config.path()`, same as every path above. Deliberately not a hardcoded
    # `"reports/..."` literal in cli.py: CLAUDE.md's own "Gotchas" already documents the
    # exact failure this avoids -- `signals --holdout` once wrote into the real repo's
    # research_log.md during a fixture-based CLI test because it resolved a path off
    # `project_root` directly instead of through a `--set`-overridable config field.
    reports_dir: str = "reports"
    # The Reviewer's own current playbook and its proposed changes (Phase 9b) -- same
    # config-relative-via-`Config.path()` reasoning as `reports_dir` just above, not a
    # `"PLAYBOOK.md"`/`"proposals"` literal in jobs.py.
    playbook_path: str = "PLAYBOOK.md"
    proposals_dir: str = "proposals"


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
    journal: JournalConfig = Field(default_factory=JournalConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    signals: SignalsConfig = Field(default_factory=SignalsConfig)
    portfolio: PortfolioConfig = Field(default_factory=PortfolioConfig)
    options: OptionsConfig = Field(default_factory=OptionsConfig)
    scenarios: dict[str, dict[str, Any]] = Field(default_factory=dict)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)
    paper: PaperConfig = Field(default_factory=PaperConfig)
    intraday: IntradayConfig = Field(default_factory=IntradayConfig)

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

"""Wiring the signal store and the regressions into one thing a CLI command can call.

Two entry points share everything below them:

``semimc signals`` (evaluation) builds the full panel across historical monthly origins,
fits the pooled and Fama-MacBeth regressions pre-holdout, and reports the metrics the
addendum's evaluation section asks for -- OOS R^2, Clark-West, rank IC, effect size,
stability -- plus, only if ``--holdout`` is passed, the one-time holdout result.

``semimc simulate --tilt`` (forecasting) fits the same regression on everything available
up to the live origin and turns it into a :class:`~semimc.context.DriftTilt` a model can
actually be simulated with.

Both go through :func:`build_full_panel`, so a bug in the point-in-time construction gets
caught in exactly one place rather than two slightly different reimplementations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..context import DriftTilt
from .panel import (
    PanelRegressionResult,
    build_regression_row,
    fit_driscoll_kraay,
    standardize_cross_sectionally,
    trailing_betas,
    training_rows,
)
from .tilt import bootstrap_panel_tilt_draws, build_drift_tilt

LOGGER = logging.getLogger(__name__)

RELATIVE_SIGNAL_COLUMNS = ["momentum", "reversal", "relative_strength", "earnings_drift"]


def monthly_origins(
    trading_days: pd.DatetimeIndex,
    start: pd.Timestamp,
    end: pd.Timestamp | None = None,
    freq: str = "ME",
) -> pd.DatetimeIndex:
    """Real trading days, one per ``freq`` period, between ``start`` and ``end`` -- reusing
    the same "actual trading day, not a synthetic period end" rule the backtester's own
    origin generator (``backtest.walkforward.generate_origins``) uses. Monthly (``"ME"``)
    by default, matching the addendum's own sampling grid; any pandas offset alias works
    (e.g. ``"W-FRI"`` for weekly), for a higher-frequency search over the same signals."""
    index = pd.DatetimeIndex(trading_days).sort_values()
    lower, upper = pd.Timestamp(start).normalize(), pd.Timestamp(end or index.max()).normalize()
    window = index[(index >= lower) & (index <= upper)]
    if len(window) == 0:
        return pd.DatetimeIndex([], name="origin")
    grouped = pd.Series(window, index=window).groupby(pd.Grouper(freq=freq)).last()
    return pd.DatetimeIndex(grouped.dropna().to_numpy(), name="origin")


def build_full_panel(
    market,  # noqa: ANN001 - MarketData; untyped to dodge an import cycle
    tickers: list[str],
    horizon_days: int,
    start: pd.Timestamp,
    end: pd.Timestamp | None = None,
    supply_chain_links: pd.DataFrame | None = None,
    customer_returns: pd.DataFrame | None = None,
    momentum_lookback_days: int = 252,
    momentum_skip_days: int = 21,
    relative_strength_lookback_days: int = 126,
    earnings_drift_active_days: int = 60,
    origin_freq: str = "ME",
) -> pd.DataFrame:
    """One row per (origin, ticker) across every origin in range, cross-sectionally
    standardized. Origins are sampled monthly by default (``origin_freq="ME"``); any
    pandas offset alias works.

    This is deliberately the *only* place ``build_regression_row`` is called in a loop --
    both the evaluation command and the live tilt fit call this, so they can never drift
    out of sync on how a row is built.
    """
    origins = monthly_origins(market.prices.index, start, end, freq=origin_freq)
    market_ticker = market.config.benchmarks.market
    sector_ticker = market.config.benchmarks.sector
    rows = []
    for origin in origins:
        context_row = build_regression_row(
            tickers,
            market.log_returns.loc[:, [*tickers, market_ticker, sector_ticker]],
            market.log_returns[market_ticker],
            market.log_returns[sector_ticker],
            market.earnings,
            origin,
            horizon_days,
            supply_chain_links=supply_chain_links,
            customer_returns=customer_returns,
            momentum_lookback_days=momentum_lookback_days,
            momentum_skip_days=momentum_skip_days,
            relative_strength_lookback_days=relative_strength_lookback_days,
            earnings_drift_active_days=earnings_drift_active_days,
        )
        rows.append(context_row.reset_index().rename(columns={"index": "ticker"}))
    if not rows:
        return pd.DataFrame()
    panel = pd.concat(rows, ignore_index=True)
    return standardize_cross_sectionally(panel, RELATIVE_SIGNAL_COLUMNS)


@dataclass
class TiltFitOutcome:
    """What :func:`fit_live_tilt` produced, plus enough to explain why it might be empty."""

    tilt: DriftTilt | None
    relative_result: PanelRegressionResult | None
    feature_columns: list[str]
    n_training_rows: int
    n_origins: int
    reason: str = ""
    diagnostics: dict = field(default_factory=dict)


def fit_live_tilt(
    market,  # noqa: ANN001
    config,  # noqa: ANN001
    tickers: list[str],
    as_of: pd.Timestamp,
    horizon_days: int,
    supply_chain_links: pd.DataFrame | None = None,
    customer_returns: pd.DataFrame | None = None,
    rng: np.random.Generator | None = None,
) -> TiltFitOutcome:
    """Fit the relative-tilt regression on everything available up to ``as_of`` and turn it
    into a :class:`DriftTilt` ready to hand to a model via ``context.with_tilt``.

    The sector tilt is included only when ``supply_chain_links``/``customer_returns`` are
    not needed for it (it uses the industry series instead) -- callers that also want the
    sector half should fit it separately with ``forecasting.sector`` and combine with
    ``forecasting.tilt.build_drift_tilt`` directly; this function covers the always-available
    relative tilt, which is the piece every live forecast can compute with no extra data.
    """
    rng = rng or np.random.default_rng(config.simulation.seed)
    panel = build_full_panel(
        market,
        tickers,
        horizon_days,
        start=config.backtest.start,
        end=as_of,
        supply_chain_links=supply_chain_links,
        customer_returns=customer_returns,
        momentum_lookback_days=config.signals.momentum_lookback_days,
        momentum_skip_days=config.signals.momentum_skip_days,
        relative_strength_lookback_days=config.signals.relative_strength_lookback_days,
        earnings_drift_active_days=config.signals.earnings_drift_active_days,
    )
    if panel.empty:
        return TiltFitOutcome(None, None, [], 0, 0, reason="no origins in range")

    n_dates = panel["date"].nunique()
    feature_columns = [c for c in RELATIVE_SIGNAL_COLUMNS if panel[c].notna().mean() > 0.5]
    if not feature_columns:
        return TiltFitOutcome(None, None, [], 0, n_dates, reason="no usable signal columns")

    train = training_rows(panel, as_of).dropna(subset=[*feature_columns, "target"])
    if len(train) < config.signals.min_regression_observations:
        needed = config.signals.min_regression_observations
        return TiltFitOutcome(
            None,
            None,
            feature_columns,
            len(train),
            n_dates,
            reason=f"only {len(train)} training rows (need >= {needed})",
        )

    maxlags = max(1, round(config.signals.driscoll_kraay_lag_per_horizon * horizon_days / 21))
    try:
        result = fit_driscoll_kraay(train, feature_columns, maxlags=maxlags)
    except ValueError as exc:
        return TiltFitOutcome(None, None, feature_columns, len(train), n_dates, reason=str(exc))

    latest_origin = panel["date"].max()
    latest_features = panel[panel["date"] == latest_origin].set_index("ticker")
    latest_features = latest_features.reindex(tickers)
    if latest_features[feature_columns].isna().all(axis=None):
        return TiltFitOutcome(
            None,
            result,
            feature_columns,
            len(train),
            n_dates,
            reason="no usable signal values at the live origin",
        )
    # A missing signal at the live origin contributes no tilt for that column, not a
    # dropped ticker -- the regression still uses the ticker's other signals. Filling only
    # the feature columns (not the whole frame, which also holds non-numeric columns like
    # target_end_date) avoids a pandas downcasting warning on the untouched columns.
    latest_features = latest_features.copy()
    latest_features[feature_columns] = latest_features[feature_columns].fillna(0.0)

    relative_point_h = result.predict(latest_features[feature_columns])

    block_length = max(1, round(21 / 21))  # one month per block, in sampling-period units
    draws = bootstrap_panel_tilt_draws(
        train,
        feature_columns,
        latest_features[feature_columns],
        n_bootstraps=config.signals.n_coefficient_bootstraps,
        block_length=block_length,
        rng=rng,
    )
    draws = np.nan_to_num(draws, nan=0.0)

    sector_factor_returns = (
        market.log_returns[config.benchmarks.sector] - market.log_returns[config.benchmarks.market]
    )
    beta_sector = pd.Series(
        {
            t: trailing_betas(
                market.log_returns[t],
                market.log_returns[config.benchmarks.market],
                sector_factor_returns,
                as_of,
            )[1]
            for t in tickers
        }
    )

    tilt = build_drift_tilt(
        tickers=tickers,
        relative_point_h=relative_point_h,
        relative_draws_h=draws,
        sector_point_h=0.0,
        sector_draws_h=np.zeros(draws.shape[0]),
        beta_sector=beta_sector,
        shrinkage_lambda=config.signals.shrinkage_lambda,
        tilt_cap_annual=config.signals.tilt_cap_annual,
        horizon_days=horizon_days,
        trading_days_per_year=config.simulation.trading_days_per_year,
        label=f"relative:{'+'.join(feature_columns)}",
    )

    return TiltFitOutcome(
        tilt=tilt,
        relative_result=result,
        feature_columns=feature_columns,
        n_training_rows=len(train),
        n_origins=panel["date"].nunique(),
        diagnostics={
            "maxlags": maxlags,
            "latest_origin": str(latest_origin.date()),
            "coefficients": result.coefficients.to_dict(),
            "t_stats": result.t_stats.to_dict(),
            "r_squared": result.r_squared,
        },
    )


def load_supply_chain_links_if_present(path: str | Path):  # noqa: ANN201
    """``load_supply_chain_links`` if the file exists, else ``None`` -- used by callers that
    treat the lead-lag signal as optional rather than a hard requirement."""
    from ..signals.supply_chain import SupplyChainError, load_supply_chain_links

    if not Path(path).exists():
        return None
    try:
        return load_supply_chain_links(path)
    except SupplyChainError as exc:
        LOGGER.warning("supply-chain links present but unreadable: %s", exc)
        return None

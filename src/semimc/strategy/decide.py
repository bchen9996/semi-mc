"""``Decision``: a frozen, auditable snapshot of one sleeve's target weights.

The brief's "freeze decisions before trading" rule needs something to freeze -- this is
it. A later phase's ``live/decide.py`` job wraps :func:`decide` with data-vintage/git-
commit bookkeeping and writes the result to the ledger; here, ``Decision`` and its
``snapshot_hash`` are the auditable core that job hashes, not yet wired to any storage.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..data import MarketData
from ..forecasting import (
    build_sector_regression_row,
    fit_sector_tilt,
    industry_yoy_growth,
    sector_factor,
    training_rows,
)
from ..forecasting.pipeline import monthly_origins
from ..signals.external import ExternalSeriesError, load_external_series
from .forecasts import portfolio_vol_forecast, trailing_portfolio_vol
from .weights import equal_weight, scale_to_target_vol

SLEEVES = ("s0", "s3", "vol_twin_shadow", "sector_shadow")


@dataclass(frozen=True)
class Decision:
    """One sleeve's target weights at one as-of date, plus how they were arrived at.

    ``weights`` need not sum to 1 -- the remainder is implicit cash (see
    :func:`semimc.strategy.weights.scale_to_target_vol`). ``diagnostics`` carries whatever
    inputs produced the weights, so the decision is self-explanatory without re-deriving
    it, matching the brief's requirement to store "full forecasts, signal values, and data
    vintages" alongside a decision.
    """

    as_of: pd.Timestamp
    sleeve: str
    weights: pd.Series
    diagnostics: dict = field(default_factory=dict)

    @property
    def snapshot_hash(self) -> str:
        """SHA-256 of the decision's economically meaningful content.

        Deliberately excludes ``diagnostics`` -- two runs that land on the same weights
        for slightly different documented reasons are still the same decision to freeze
        and trade; the hash exists to catch a *decision* changing, not a log message.
        """
        payload = {
            "as_of": str(pd.Timestamp(self.as_of)),
            "sleeve": self.sleeve,
            "weights": {k: float(v) for k, v in self.weights.sort_index().items()},
        }
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def decide_s0(tickers: list[str], as_of: pd.Timestamp) -> Decision:
    """S0: equal-weight control. No data beyond the universe itself."""
    weights = equal_weight(tickers)
    return Decision(
        as_of=pd.Timestamp(as_of),
        sleeve="s0",
        weights=weights,
        diagnostics={"kind": "equal_weight"},
    )


def decide_s3(
    market: MarketData,
    config,  # noqa: ANN001
    tickers: list[str],
    as_of: pd.Timestamp,
    rng: np.random.Generator,
    horizon: int = 21,
    n_paths: int | None = None,
    model_name: str = "blend",
) -> Decision:
    """S3: S0 scaled toward ``config.paper.s3_target_vol`` using ``model_name``'s own
    forecast, capped at ``config.paper.s3_max_leverage`` (no leverage by default)."""
    base_weights = equal_weight(tickers)
    paths = n_paths or config.simulation.n_paths
    forecast_vol, fitted = portfolio_vol_forecast(
        market, config, tickers, base_weights, as_of, horizon, paths, rng, model_name=model_name
    )
    scaled = scale_to_target_vol(
        base_weights, forecast_vol, config.paper.s3_target_vol, config.paper.s3_max_leverage
    )
    scale_factor = float(scaled.sum() / base_weights.sum()) if base_weights.sum() else 0.0
    return Decision(
        as_of=pd.Timestamp(as_of),
        sleeve="s3",
        weights=scaled,
        diagnostics={
            "kind": "vol_target",
            "model": model_name,
            "horizon": horizon,
            "n_paths": paths,
            "forecast_annual_vol": forecast_vol,
            "target_vol": config.paper.s3_target_vol,
            "max_leverage": config.paper.s3_max_leverage,
            "scale": scale_factor,
            "model_describe": fitted.describe() if hasattr(fitted, "describe") else None,
        },
    )


def decide_vol_twin_shadow(
    market: MarketData,
    config,  # noqa: ANN001
    tickers: list[str],
    as_of: pd.Timestamp,
    window_days: int = 252,
) -> Decision:
    """S3's shadow twin (T3): identical target-vol scaling, sized off trailing realized
    volatility instead of a model forecast. Simulated only -- never a traded sleeve."""
    base_weights = equal_weight(tickers)
    # window_days is a trading-day count; training_window's own window_days is calendar
    # days, so ask for generous calendar coverage and let trailing_portfolio_vol's own
    # `.iloc[-window_days:]` take the exact trailing trading-day count off the result.
    calendar_days = int(round((window_days + 30) * 365.25 / 252))
    training = market.training_window(
        tickers, as_of, window_days=calendar_days, min_observations=window_days
    )
    trailing_vol = trailing_portfolio_vol(
        training,
        base_weights,
        window_days=window_days,
        trading_days_per_year=config.simulation.trading_days_per_year,
    )
    scaled = scale_to_target_vol(
        base_weights, trailing_vol, config.paper.s3_target_vol, config.paper.s3_max_leverage
    )
    return Decision(
        as_of=pd.Timestamp(as_of),
        sleeve="vol_twin_shadow",
        weights=scaled,
        diagnostics={
            "kind": "vol_target_trailing",
            "window_days": window_days,
            "trailing_annual_vol": trailing_vol,
            "target_vol": config.paper.s3_target_vol,
            "max_leverage": config.paper.s3_max_leverage,
        },
    )


def decide_sector_shadow(
    market: MarketData,
    config,  # noqa: ANN001
    tickers: list[str],
    as_of: pd.Timestamp,
    horizon: int = 21,
    min_observations: int = 12,
) -> Decision:
    """T4: shadow-only sector-timing overlay on S0. Never a traded sleeve -- the brief
    withholds any verdict on it until the 12-month review, so this exists only to record
    what it *would* have done, for that later comparison.

    Wraps ``forecasting/sector.py``'s industry-cycle -> sector-factor regression: refit it
    on every fully realized (origin, outcome) pair up to ``as_of`` (``training_rows``
    enforces the same "target already happened" rule the panel regression uses), then use
    it to forecast today's sector-factor return from the industry-cycle reading already
    known at ``as_of`` (``industry_yoy_growth`` is point-in-time by construction). That
    forecast is expressed in units of the regression's own historical target std, clipped
    to +/-1 std, then scales S0's gross exposure by up to
    ``config.paper.sector_tilt_strength`` -- bounded the same no-leverage, never-short way
    S3 is. A missing/insufficient industry series degrades to plain S0 weights rather than
    raising, since a shadow sleeve losing its input is not a reason to break a decision
    job.
    """
    base_weights = equal_weight(tickers)
    as_of = pd.Timestamp(as_of)
    diagnostics: dict = {"kind": "sector_tilt_shadow", "base_kind": "equal_weight"}

    industry_path = (
        config.path(config.signals.external_dir) / f"{config.signals.industry_series_id}.csv"
    )
    try:
        external = load_external_series(industry_path)
    except ExternalSeriesError as exc:
        diagnostics.update({"tilt_applied": False, "reason": f"unavailable: {exc}"})
        return Decision(
            as_of=as_of, sleeve="sector_shadow", weights=base_weights, diagnostics=diagnostics
        )

    sector_returns = sector_factor(
        market.log_returns[config.benchmarks.market], market.log_returns[config.benchmarks.sector]
    )
    origins = monthly_origins(market.prices.index, config.backtest.start, as_of)
    rows = pd.DataFrame(
        [build_sector_regression_row(sector_returns, external, o, horizon) for o in origins]
    )
    usable = (
        training_rows(rows, as_of).dropna(subset=["industry_yoy", "target"])
        if not rows.empty
        else rows
    )
    today_growth = industry_yoy_growth(external, as_of)

    if len(usable) < min_observations or not np.isfinite(today_growth):
        diagnostics.update(
            {
                "tilt_applied": False,
                "reason": "insufficient data",
                "n_observations": int(len(usable)),
            }
        )
        return Decision(
            as_of=as_of, sleeve="sector_shadow", weights=base_weights, diagnostics=diagnostics
        )

    result = fit_sector_tilt(usable)
    predicted = float(
        result.coefficients["const"] + result.coefficients["industry_yoy"] * today_growth
    )
    residual_scale = float(usable["target"].std(ddof=1))
    z = predicted / residual_scale if residual_scale > 0 else 0.0
    tilt_strength = config.paper.sector_tilt_strength
    multiplier = 1.0 + tilt_strength * float(np.clip(z, -1.0, 1.0))
    multiplier = float(np.clip(multiplier, 0.0, config.paper.s3_max_leverage))
    diagnostics.update(
        {
            "tilt_applied": True,
            "n_observations": int(len(usable)),
            "coefficient": float(result.coefficients["industry_yoy"]),
            "t_stat": float(result.t_stats["industry_yoy"]),
            "r_squared": result.r_squared,
            "industry_yoy_today": today_growth,
            "predicted_sector_return": predicted,
            "residual_scale": residual_scale,
            "gross_multiplier": multiplier,
            "tilt_strength": tilt_strength,
        }
    )
    return Decision(
        as_of=as_of,
        sleeve="sector_shadow",
        weights=base_weights * multiplier,
        diagnostics=diagnostics,
    )


def decide(
    sleeve: str,
    market: MarketData,
    config,  # noqa: ANN001
    tickers: list[str],
    as_of: pd.Timestamp,
    rng: np.random.Generator,
) -> Decision:
    """Dispatch to the named sleeve. The one entry point a later phase's
    ``live/decide.py`` job, the shadow book, and tests all call -- never construct a
    ``Decision`` by hand outside this module."""
    if sleeve == "s0":
        return decide_s0(tickers, as_of)
    if sleeve == "s3":
        return decide_s3(market, config, tickers, as_of, rng)
    if sleeve == "vol_twin_shadow":
        return decide_vol_twin_shadow(market, config, tickers, as_of)
    if sleeve == "sector_shadow":
        return decide_sector_shadow(market, config, tickers, as_of)
    raise ValueError(f"unknown sleeve {sleeve!r}; must be one of {SLEEVES}")

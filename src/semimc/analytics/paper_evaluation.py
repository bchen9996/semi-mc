"""T2 and T3 (`THESES.md`): comparing two sleeves' realized performance.

Two ways a sleeve's equity history reaches this module: a *real* one (S0, S3) reads
straight off the ledger's broker-confirmed snapshots (:func:`equity_curve_from_snapshots`);
a *shadow* one (`vol_twin_shadow`, which never places an order) has its decision history
replayed against realized prices instead (:func:`replay_shadow_equity_curve`). Both paths
feed :func:`compare_sleeves`, which is the actual T2/T3 comparison and does not care which
kind of curve it was given.

Everything here is descriptive once history exists; `THESES.md`'s minimum-history caveat
(tied to `paper.review_months[0]`) is applied by the caller (`cli.py`'s `paper evaluate`),
not enforced here -- this module always computes what it can from whatever history it is
given, the same as `analytics/power.py` always returns a number rather than refusing.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..data import MarketData
from .risk import conditional_value_at_risk, value_at_risk
from .scoring import block_bootstrap_origins


@dataclass(frozen=True)
class SleeveStats:
    """Descriptive stats for one sleeve's own equity curve -- never a comparison."""

    label: str
    n_days: int
    start: pd.Timestamp | None
    end: pd.Timestamp | None
    annualized_return: float
    annualized_vol: float
    sharpe: float
    max_drawdown: float
    daily_var_95: float
    daily_cvar_95: float


@dataclass(frozen=True)
class BootstrapDifference:
    """A block-bootstrap point estimate and 95% percentile interval for one statistic's
    difference between two paired, aligned return series."""

    point_estimate: float
    ci_low: float
    ci_high: float
    n_bootstrap: int


@dataclass(frozen=True)
class ComparisonResult:
    control: SleeveStats
    candidate: SleeveStats
    annualized_return_difference: BootstrapDifference
    annualized_vol_difference: BootstrapDifference
    n_overlapping_days: int
    overlap_calendar_days: int


def replay_shadow_equity_curve(
    decisions: list[dict],
    market: MarketData,
    tickers: list[str],
    starting_equity: float = 100_000.0,
) -> pd.Series:
    """Compound a shadow sleeve's own recorded decisions against realized prices.

    ``market.log_returns.loc[d]`` is the return *ending* at ``d``'s close -- realized
    while whatever was held coming into day ``d`` was still in place, since a decision
    frozen and executed on day ``d`` only starts affecting positions from that day's
    trading onward. So a decision's weights apply to return-dates in the half-open
    interval ``(as_of, next_as_of]`` -- the rebalance day's own return is attributed to
    the *outgoing* decision, and the incoming decision's weights first earn a return the
    day after. Getting this backwards (excluding the rebalance day from both sides) would
    silently drop that day's return from the replay entirely. The last decision's weights
    run through the most recent realized date in ``market.log_returns``.

    Simple returns are aggregated across tickers by weight, never log returns (CLAUDE.md's
    convention; the same rule ``strategy/forecasts.py::trailing_portfolio_vol`` already
    follows). Using data that occurred *after* each decision was already frozen to score
    it is a realized-outcome replay, not a point-in-time violation -- the decision itself
    was made with only what was known at its own ``as_of``.
    """
    if not decisions:
        return pd.Series(dtype=float, name="equity")

    ordered = sorted(decisions, key=lambda d: pd.Timestamp(d["as_of"]))
    simple_returns = np.expm1(market.log_returns.reindex(columns=tickers))

    dates: list[pd.Timestamp] = []
    values: list[float] = []
    equity = starting_equity

    for i, decision in enumerate(ordered):
        as_of = pd.Timestamp(decision["as_of"])
        weights = pd.Series(decision["weights"], dtype=float).reindex(tickers).fillna(0.0)
        weights_vector = weights.to_numpy()
        period_end = pd.Timestamp(ordered[i + 1]["as_of"]) if i + 1 < len(ordered) else None

        mask = simple_returns.index > as_of
        if period_end is not None:
            mask &= simple_returns.index <= period_end
        period_returns = simple_returns.loc[mask]

        for date, row in period_returns.iterrows():
            equity *= 1.0 + float(np.dot(row.to_numpy(), weights_vector))
            dates.append(date)
            values.append(equity)

    return pd.Series(values, index=pd.DatetimeIndex(dates, name="date"), name="equity")


def equity_curve_from_snapshots(ledger, sleeve: str) -> pd.Series:  # noqa: ANN001
    """A real sleeve's broker-confirmed equity history, straight off the ledger."""
    rows = ledger.snapshots_for(sleeve)
    if not rows:
        return pd.Series(dtype=float, name="equity")
    index = pd.DatetimeIndex([pd.Timestamp(r["as_of"]) for r in rows], name="date")
    return pd.Series([r["equity"] for r in rows], index=index, name="equity").sort_index()


def _max_drawdown(equity: pd.Series) -> float:
    running_peak = equity.cummax()
    return float((equity / running_peak - 1.0).min())


def _sleeve_stats(
    label: str, equity: pd.Series, market: MarketData, trading_days_per_year: int
) -> SleeveStats:
    if len(equity) < 2:
        start = equity.index.min() if len(equity) else None
        end = equity.index.max() if len(equity) else None
        return SleeveStats(label, len(equity), start, end, *([float("nan")] * 6))

    returns = equity.pct_change().dropna()
    n_days = len(returns)
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    annualized_return = (1.0 + total_return) ** (trading_days_per_year / n_days) - 1.0
    annualized_vol = float(returns.std(ddof=1) * np.sqrt(trading_days_per_year))
    avg_risk_free = float(np.mean([market.risk_free.rate_at(d) for d in returns.index]))
    sharpe = (
        (annualized_return - avg_risk_free) / annualized_vol if annualized_vol > 0 else float("nan")
    )
    return SleeveStats(
        label=label,
        n_days=n_days,
        start=equity.index.min(),
        end=equity.index.max(),
        annualized_return=annualized_return,
        annualized_vol=annualized_vol,
        sharpe=sharpe,
        max_drawdown=_max_drawdown(equity),
        daily_var_95=value_at_risk(returns.to_numpy(), 0.95),
        daily_cvar_95=conditional_value_at_risk(returns.to_numpy(), 0.95),
    )


def paired_block_bootstrap(
    control: pd.Series,
    candidate: pd.Series,
    statistic_fn,  # noqa: ANN001 -- Callable[[pd.DataFrame], float]
    block_length: int,
    n_bootstrap: int,
    rng: np.random.Generator,
) -> BootstrapDifference:
    """95% percentile CI for a paired statistic, resampled by contiguous calendar-day
    blocks rather than individual days.

    Reuses :func:`~semimc.analytics.scoring.block_bootstrap_origins` -- built there to
    resample whole *origins* (a shared calendar date across tickers) so cross-sectional
    dependence survives resampling intact. Here the "origin" is instead a contiguous block
    of trading days, which is exactly a moving-block bootstrap for *serially* dependent
    daily returns -- the same resampling mechanism, a different grouping.
    """
    aligned = pd.DataFrame({"control": control, "candidate": candidate}).dropna()
    if aligned.empty:
        return BootstrapDifference(float("nan"), float("nan"), float("nan"), 0)

    aligned = aligned.reset_index(drop=True)
    aligned["block"] = aligned.index // max(block_length, 1)
    observed = statistic_fn(aligned)
    replicates = block_bootstrap_origins(aligned, statistic_fn, n_bootstrap, rng, "block")
    if len(replicates) == 0:
        return BootstrapDifference(observed, float("nan"), float("nan"), 0)
    ci_low, ci_high = np.percentile(replicates, [2.5, 97.5])
    return BootstrapDifference(float(observed), float(ci_low), float(ci_high), int(n_bootstrap))


def compare_sleeves(
    control_equity: pd.Series,
    control_label: str,
    candidate_equity: pd.Series,
    candidate_label: str,
    market: MarketData,
    config,  # noqa: ANN001
    rng: np.random.Generator,
) -> ComparisonResult:
    """The actual T2/T3 comparison: each sleeve's own descriptive stats, plus a paired
    block-bootstrap CI on the *difference* in annualized return and annualized vol over
    whatever history the two curves overlap on. ``rng`` is never defaulted here -- this
    project's own rule is one explicit ``np.random.Generator`` per caller, never a hidden
    seed (CLAUDE.md)."""
    trading_days_per_year = config.simulation.trading_days_per_year
    control_stats = _sleeve_stats(control_label, control_equity, market, trading_days_per_year)
    candidate_stats = _sleeve_stats(
        candidate_label, candidate_equity, market, trading_days_per_year
    )

    control_returns = control_equity.pct_change().dropna()
    candidate_returns = candidate_equity.pct_change().dropna()
    overlap = control_returns.index.intersection(candidate_returns.index)
    aligned_control = control_returns.reindex(overlap)
    aligned_candidate = candidate_returns.reindex(overlap)

    block_length = config.paper.evaluation.block_length_days
    n_bootstrap = config.paper.evaluation.n_bootstrap

    return_difference = paired_block_bootstrap(
        aligned_control,
        aligned_candidate,
        lambda f: float((f["candidate"] - f["control"]).mean() * trading_days_per_year),
        block_length,
        n_bootstrap,
        rng,
    )
    vol_difference = paired_block_bootstrap(
        aligned_control,
        aligned_candidate,
        lambda f: float(
            f["candidate"].std(ddof=1) * np.sqrt(trading_days_per_year)
            - f["control"].std(ddof=1) * np.sqrt(trading_days_per_year)
        ),
        block_length,
        n_bootstrap,
        rng,
    )

    overlap_calendar_days = int((overlap.max() - overlap.min()).days) + 1 if len(overlap) > 0 else 0
    return ComparisonResult(
        control=control_stats,
        candidate=candidate_stats,
        annualized_return_difference=return_difference,
        annualized_vol_difference=vol_difference,
        n_overlapping_days=len(overlap),
        overlap_calendar_days=overlap_calendar_days,
    )


def evaluate_t2(ledger, market: MarketData, config) -> ComparisonResult:  # noqa: ANN001
    """T2: does S3 (vol-targeted) improve on S0 (equal-weight control)? Both are real,
    broker-confirmed equity curves -- no replay needed."""
    s0_equity = equity_curve_from_snapshots(ledger, "s0")
    s3_equity = equity_curve_from_snapshots(ledger, "s3")
    rng = np.random.default_rng(config.simulation.seed)
    return compare_sleeves(s0_equity, "s0", s3_equity, "s3", market, config, rng)


def evaluate_t3(ledger, market: MarketData, config, tickers: list[str]) -> ComparisonResult:  # noqa: ANN001
    """T3: does S3's model-based vol forecast beat its trailing-realized-vol shadow twin?
    S3 is real; ``vol_twin_shadow`` never trades, so its side of the comparison is a
    synthetic replay of its own recorded decisions."""
    s3_equity = equity_curve_from_snapshots(ledger, "s3")
    shadow_decisions = ledger.decisions_for("vol_twin_shadow")
    shadow_equity = replay_shadow_equity_curve(shadow_decisions, market, tickers)
    rng = np.random.default_rng(config.simulation.seed)
    return compare_sleeves(shadow_equity, "vol_twin_shadow", s3_equity, "s3", market, config, rng)

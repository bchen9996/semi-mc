"""The forward forecast journal: forecast distributions written down *before* their
outcomes exist, and scored once each horizon has passed (added 2026-09-27).

Why this exists. Every favourable number in this project's backtest is at least mildly
contaminated: the default drift policy (``shrunk_historical``) was chosen by backtest
over the same 2012-2026 window its results are reported on (MODEL_NOTES section 7), the
models were designed knowing 2020 and 2022 happened (section 8), and the Phase 4B
holdout is already spent. The only clean evidence left is the future. This module
records what the models say today and scores it later, with git as the notary: the
journal is an append-only CSV, so each forecast's commit timestamp predates its outcome.

**Recording** (:func:`record_forecasts`) fits each configured (model, drift policy)
variant on the latest available trading day -- never an earlier one; backdating would
reopen exactly the contamination this exists to avoid -- through the same
:func:`~semimc.engine.runner.fit_at_origin` choke point as the backtester and live
decisions, with the same live context ``semimc simulate`` uses. Each distribution is
stored as 99 quantiles (1%..99%) of the *simple* return per horizon, not raw paths:
diffable, ~100x smaller, and enough for CRPS, PIT and coverage. An origin that is
already in the journal is skipped, never overwritten. A record run after the next
session could have opened (the next weekday's 09:30 New York time) is refused: by then
part of the outcome window was public when the forecast was written down. That is the
exact contamination test, where a calendar-day age limit is only a proxy for it.

**Month-end only** (``month_end_only=True``, what the scheduled workflow uses): records
only when no weekday remains in the origin's month, so origins land on the same
month-end grid as ``backtest.origin_frequency`` and the timing of each entry is fixed by
the calendar, never chosen. With no market calendar in this project, a month whose last
weekday is an exchange holiday (Good Friday on March 30/31, Memorial Day on May 31 -- a
few times a decade) is skipped rather than recorded a session late.

**Scoring** (:func:`score_journal`) uses the backtester's own outcome definition
(``backtest.walkforward._realized_outcome``: log return from the origin's close to the
close ``horizon`` trading days later), computed entirely from today's adjusted prices --
a ratio of two prices from the same series is invariant to the proportional
back-adjustment a later dividend or split applies, where the start price stored at
record time is not. It reports mean CRPS (quantile-grid approximation, see
:func:`~semimc.analytics.scoring.crps_from_quantiles`), CRPS skill against the baseline
variant on matched forecasts, mean PIT, and coverage.

No significance tests yet, deliberately: forecasts across tickers on one origin are
strongly dependent, and overlapping horizons more so, so the effective sample is
roughly the number of non-overlapping origins -- reported alongside every row. The
backtester's block-bootstrap tests become worth running once that count reaches a
couple of dozen; until then the report says so rather than printing a p-value that
would mislead.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .analytics.scoring import crps_from_quantiles, pit_from_quantiles
from .backtest.walkforward import _realized_outcome, select_non_overlapping
from .data import MarketData
from .engine.runner import fit_at_origin

# The exchange whose next open ends the record window. NYSE/Nasdaq, for this universe.
_EXCHANGE_TZ = "America/New_York"
_EXCHANGE_OPEN = dt.time(9, 30)

LOGGER = logging.getLogger(__name__)

QUANTILE_LEVELS = np.round(np.arange(1, 100) / 100.0, 2)
QUANTILE_COLUMNS = [f"q{int(round(level * 100)):02d}" for level in QUANTILE_LEVELS]
META_COLUMNS = [
    "recorded_at",
    "origin",
    "ticker",
    "model",
    "drift_policy",
    "horizon",
    "start_price",
    "n_paths",
    "seed",
    "git_commit",
]
JOURNAL_COLUMNS = META_COLUMNS + QUANTILE_COLUMNS


@dataclass
class RecordResult:
    origin: pd.Timestamp | None
    rows: pd.DataFrame
    failures: list[dict] = field(default_factory=list)
    skipped_reason: str | None = None


def read_journal(path: Path) -> pd.DataFrame:
    """The whole journal, or an empty frame with the right columns if none exists."""
    if not path.exists():
        return pd.DataFrame(columns=JOURNAL_COLUMNS)
    frame = pd.read_csv(path, parse_dates=["origin"])
    missing = [c for c in JOURNAL_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"{path} is missing journal columns: {missing}")
    return frame


def append_journal(path: Path, rows: pd.DataFrame) -> None:
    """Appends ``rows`` without rewriting anything already there -- the append-only
    property is what makes each git commit a timestamped pre-registration."""
    path.parent.mkdir(parents=True, exist_ok=True)
    out = rows[JOURNAL_COLUMNS].copy()
    out["origin"] = pd.DatetimeIndex(out["origin"]).strftime("%Y-%m-%d")
    new_file = not path.exists()
    out.to_csv(
        path, mode="w" if new_file else "a", header=new_file, index=False, float_format="%.6g"
    )


def next_open_after(origin: pd.Timestamp) -> dt.datetime:
    """The first weekday after ``origin`` at the exchange open, as aware UTC -- the
    moment information after ``origin`` could start to exist. A holiday on that weekday
    makes this conservative (earlier than the real next open), never lenient."""
    from zoneinfo import ZoneInfo

    next_day = (pd.Timestamp(origin).normalize() + pd.offsets.BDay(1)).date()
    local = dt.datetime.combine(next_day, _EXCHANGE_OPEN, tzinfo=ZoneInfo(_EXCHANGE_TZ))
    return local.astimezone(dt.UTC)


def is_last_weekday_of_month(origin: pd.Timestamp) -> bool:
    stamp = pd.Timestamp(origin).normalize()
    return (stamp + pd.offsets.BDay(1)).month != stamp.month


def record_forecasts(
    market: MarketData,
    config,  # noqa: ANN001
    recorded_at: dt.datetime,
    git_commit: str | None = None,
    existing: pd.DataFrame | None = None,
    month_end_only: bool = False,
) -> RecordResult:
    """Fits every configured variant for every universe ticker at the latest trading day
    in ``market`` and returns the journal rows (the caller appends them).

    Parameters
    ----------
    recorded_at
        Aware UTC wall-clock time of this record run -- written to every row, and
        checked against :func:`next_open_after` so no forecast is written after its
        outcome window started.
    month_end_only
        Record only when ``origin`` is the last weekday of its month (see the module
        docstring).
    existing
        The journal so far (``read_journal``); an origin already present is skipped.

    Seeds: one child stream per (origin, ticker, variant), spawned from
    ``simulation.seed`` with ``spawn_key=(origin ordinal, ticker index, variant index)``,
    so any row can be regenerated from the config, the git commit and the data vintage.
    """
    journal_cfg = config.journal
    origin = pd.Timestamp(market.prices.index.max()).normalize()
    empty = pd.DataFrame(columns=JOURNAL_COLUMNS)

    window_closes = next_open_after(origin)
    if recorded_at >= window_closes:
        return RecordResult(
            origin,
            empty,
            skipped_reason=(
                f"latest price is {origin.date()}, but the next session could have opened at "
                f"{window_closes.isoformat(timespec='minutes')} -- part of the outcome window "
                "is already public; run `semimc fetch` first"
            ),
        )
    if month_end_only and not is_last_weekday_of_month(origin):
        return RecordResult(
            origin, empty, skipped_reason=f"{origin.date()} is not the last weekday of its month"
        )
    if existing is not None and not existing.empty:
        recorded = set(pd.DatetimeIndex(existing["origin"]).normalize())
        if origin in recorded:
            return RecordResult(
                origin, empty, skipped_reason=f"{origin.date()} is already in the journal"
            )

    horizons = sorted(config.horizons)
    max_horizon = max(horizons)
    base_context = market.context(origin)
    seed = int(config.simulation.seed)
    tickers = [t for t in config.universe.tickers if t in market.prices.columns]

    records: list[dict] = []
    failures: list[dict] = []
    for ticker_index, ticker in enumerate(tickers):
        series = market.prices[ticker].dropna()
        if series.empty or series.index.max() < origin:
            failures.append({"ticker": ticker, "variant": "*", "error": "no price at origin"})
            continue
        start_price = float(series.loc[:origin].iloc[-1])

        for variant_index, variant in enumerate(journal_cfg.variants):
            context = dataclasses.replace(base_context, drift_policy=variant.drift_policy)
            rng = np.random.default_rng(
                np.random.SeedSequence(
                    entropy=seed,
                    spawn_key=(origin.toordinal(), ticker_index, variant_index),
                )
            )
            try:
                model, _ = fit_at_origin(market, config, variant.model, [ticker], origin, context)
                simulated = np.asarray(
                    model.simulate(journal_cfg.n_paths, max_horizon, rng), dtype=float
                )[:, :, 0]
            except Exception as exc:  # noqa: BLE001 - one bad cell must not end the run
                failures.append(
                    {
                        "ticker": ticker,
                        "variant": variant.label,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            cumulative = np.cumsum(simulated, axis=1)

            for horizon in horizons:
                quantiles = np.quantile(np.expm1(cumulative[:, horizon - 1]), QUANTILE_LEVELS)
                row = {
                    "recorded_at": recorded_at.isoformat(timespec="seconds"),
                    "origin": origin,
                    "ticker": ticker,
                    "model": variant.model,
                    "drift_policy": variant.drift_policy,
                    "horizon": horizon,
                    "start_price": start_price,
                    "n_paths": journal_cfg.n_paths,
                    "seed": seed,
                    "git_commit": git_commit or "",
                }
                row.update(dict(zip(QUANTILE_COLUMNS, quantiles, strict=True)))
                records.append(row)

    rows = pd.DataFrame(records, columns=JOURNAL_COLUMNS) if records else empty
    return RecordResult(origin, rows, failures)


# -- scoring ------------------------------------------------------------------------------


@dataclass
class ScoreResult:
    as_of: pd.Timestamp
    scored: pd.DataFrame
    summary: pd.DataFrame
    n_pending: int
    baseline_label: str
    coverage_levels: list[float]


def _coverage_indices(level: float) -> tuple[int, int]:
    lower_tau = round((1.0 - level) / 2.0, 6)
    matches = np.flatnonzero(np.isclose(QUANTILE_LEVELS, lower_tau))
    upper = np.flatnonzero(np.isclose(QUANTILE_LEVELS, 1.0 - lower_tau))
    if not len(matches) or not len(upper):
        raise ValueError(
            f"coverage level {level} does not fall on the journal's 1%..99% quantile grid"
        )
    return int(matches[0]), int(upper[0])


def score_journal(journal: pd.DataFrame, market: MarketData, config) -> ScoreResult:  # noqa: ANN001
    """Scores every journal row whose horizon has elapsed in ``market``'s prices; the
    rest are counted as pending."""
    levels = list(config.backtest.coverage_levels)
    bounds = {level: _coverage_indices(level) for level in levels}
    baseline = config.journal.baseline
    as_of = pd.Timestamp(market.prices.index.max()).normalize()

    records: list[dict] = []
    pending = 0
    for row in journal.itertuples(index=False):
        row = row._asdict()
        if row["ticker"] not in market.prices.columns:
            pending += 1
            continue
        outcome = _realized_outcome(
            market.prices[row["ticker"]], pd.Timestamp(row["origin"]), int(row["horizon"])
        )
        if outcome is None:
            pending += 1
            continue
        realized = float(np.expm1(outcome[0]))
        quantiles = np.array([row[c] for c in QUANTILE_COLUMNS], dtype=float)
        scored = {
            "origin": pd.Timestamp(row["origin"]),
            "ticker": row["ticker"],
            "model": row["model"],
            "drift_policy": row["drift_policy"],
            "horizon": int(row["horizon"]),
            "realized_simple_return": realized,
            "median_forecast": float(quantiles[QUANTILE_COLUMNS.index("q50")]),
            "crps": crps_from_quantiles(quantiles, QUANTILE_LEVELS, realized),
            "pit": pit_from_quantiles(quantiles, QUANTILE_LEVELS, realized),
        }
        for level, (lo, hi) in bounds.items():
            scored[f"hit_{int(round(level * 100))}"] = bool(
                quantiles[lo] <= realized <= quantiles[hi]
            )
        records.append(scored)

    scored_frame = pd.DataFrame(records)
    summary = _summarize(scored_frame, baseline.model, baseline.drift_policy, levels)
    return ScoreResult(as_of, scored_frame, summary, pending, baseline.label, levels)


def _summarize(
    scored: pd.DataFrame, baseline_model: str, baseline_policy: str, levels: list[float]
) -> pd.DataFrame:
    if scored.empty:
        return pd.DataFrame()
    keys = ["origin", "ticker", "horizon"]
    base = scored[
        (scored["model"] == baseline_model) & (scored["drift_policy"] == baseline_policy)
    ][keys + ["crps"]].rename(columns={"crps": "baseline_crps"})

    out = []
    for (model, policy, horizon), group in scored.groupby(
        ["model", "drift_policy", "horizon"], sort=True
    ):
        origins = pd.DatetimeIndex(sorted(group["origin"].unique()))
        matched = group.merge(base, on=keys, how="inner")
        skill = (
            1.0 - matched["crps"].mean() / matched["baseline_crps"].mean()
            if not matched.empty and matched["baseline_crps"].mean() > 0
            else np.nan
        )
        record = {
            "model": model,
            "drift_policy": policy,
            "horizon": int(horizon),
            "n_forecasts": len(group),
            "n_origins": len(origins),
            "n_independent_origins": int(select_non_overlapping(origins, int(horizon)).sum()),
            "mean_crps": group["crps"].mean(),
            "crps_skill_vs_baseline": skill,
            "mean_pit": group["pit"].mean(),
            "median_forecast": group["median_forecast"].median(),
            "median_realized": group["realized_simple_return"].median(),
        }
        for level in levels:
            tag = int(round(level * 100))
            record[f"coverage_{tag}"] = group[f"hit_{tag}"].mean()
        out.append(record)
    return pd.DataFrame(out)


def render_score_markdown(result: ScoreResult) -> str:
    lines = [
        f"# Forecast journal score: {result.as_of.date()}",
        "",
        "Forecasts recorded before their outcomes existed (journal/forecasts.csv, git-",
        "timestamped), scored against what happened. Distributions of the simple return.",
        "",
        f"- Scored forecasts: {len(result.scored)}",
        f"- Pending (horizon not reached yet): {result.n_pending}",
        f"- CRPS skill baseline: {result.baseline_label} (positive = better than baseline)",
        "",
    ]
    if result.summary.empty:
        lines.append("Nothing to score yet -- every recorded horizon is still open.")
        return "\n".join(lines) + "\n"

    tags = [int(round(level * 100)) for level in result.coverage_levels]
    header = (
        "| horizon | variant | forecasts | origins | independent origins | mean CRPS | "
        "skill vs baseline | mean PIT | "
        + " | ".join(f"{t}% coverage" for t in tags)
        + " | median forecast | median realized |"
    )
    lines.append(header)
    lines.append("|" + "---|" * (10 + len(tags)))
    for row in result.summary.sort_values(["horizon", "model", "drift_policy"]).itertuples():
        row = row._asdict()
        skill = row["crps_skill_vs_baseline"]
        coverage_cells = " | ".join(f"{row[f'coverage_{t}']:.0%}" for t in tags)
        lines.append(
            f"| {row['horizon']}d | {row['model']}/{row['drift_policy']} | "
            f"{row['n_forecasts']} | {row['n_origins']} | {row['n_independent_origins']} | "
            f"{row['mean_crps']:.4f} | {'-' if np.isnan(skill) else f'{skill:+.1%}'} | "
            f"{row['mean_pit']:.2f} | {coverage_cells} | {row['median_forecast']:+.1%} | "
            f"{row['median_realized']:+.1%} |"
        )

    min_independent = int(result.summary["n_independent_origins"].min())
    lines += [
        "",
        "How to read this: a calibrated forecast has mean PIT near 0.50 (above means outcomes",
        "keep landing in the upper part of the distribution -- drift too low; below, too",
        "high) and coverage near its nominal level. Forecasts on one origin are strongly",
        "dependent across tickers, so the effective sample is closer to the independent-",
        "origin count than the forecast count.",
        "",
        f"No significance tests: the smallest independent-origin count is {min_independent}. "
        "Treat any difference as anecdotal until that reaches a couple of dozen.",
    ]
    return "\n".join(lines) + "\n"

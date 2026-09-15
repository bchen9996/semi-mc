"""Scorecard: per-origin rows in, an honest verdict out.

The aggregation rules, and why:

* **Coverage and PIT** use every origin. They are descriptive statistics, and dropping
  seven eighths of the data to decorrelate them would leave too little to see anything.
* **Significance tests** (Kupiec, Christoffersen, Diebold-Mariano) use only the
  non-overlapping subset. They assume independent observations, and overlapping forecast
  windows violate that badly enough to turn a 5% test into something closer to a 30% one.
* **CRPS skill** is reported against the configured baseline, per ticker and horizon, and
  averaged only within a horizon -- CRPS scales with volatility, so averaging a 21-day
  score with a 252-day one would be meaningless.

Every cell carries both ``n`` and ``n_independent`` so a reader can see immediately when a
number rests on twelve observations.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..analytics.scoring import (
    christoffersen_conditional_coverage,
    christoffersen_independence,
    crps_skill_score,
    diebold_mariano,
    kupiec_pof,
    pit_histogram,
)

LOW_POWER_THRESHOLD = 20  # independent observations below which a cell is flagged


def coverage_table(rows: pd.DataFrame, levels: list[float]) -> pd.DataFrame:
    """Realized coverage of each central interval, per model, ticker and horizon."""
    if rows.empty:
        return pd.DataFrame()
    groups = rows.groupby(["model", "ticker", "horizon"], sort=False)
    records = []
    for (model, ticker, horizon), group in groups:
        record = {
            "model": model,
            "ticker": ticker,
            "horizon": int(horizon),
            "n": int(len(group)),
            "n_independent": int(group["non_overlapping"].sum()),
        }
        for level in levels:
            tag = f"{int(round(level * 100))}"
            column = f"hit_{tag}"
            if column in group:
                record[f"coverage_{tag}"] = float(group[column].mean())
                record[f"nominal_{tag}"] = float(level)
                record[f"coverage_error_{tag}"] = float(group[column].mean() - level)
        records.append(record)
    return pd.DataFrame(records)


def crps_table(rows: pd.DataFrame, baseline: str) -> pd.DataFrame:
    """Mean CRPS and skill score against the baseline model."""
    if rows.empty:
        return pd.DataFrame()

    mean_crps = (
        rows.groupby(["model", "ticker", "horizon"], sort=False)
        .agg(
            crps=("crps", "mean"),
            n=("crps", "size"),
            n_independent=("non_overlapping", "sum"),
        )
        .reset_index()
    )

    baseline_frame = (
        mean_crps[mean_crps["model"] == baseline]
        .set_index(["ticker", "horizon"])["crps"]
        .rename("baseline_crps")
    )
    merged = mean_crps.join(baseline_frame, on=["ticker", "horizon"])
    merged["crps_skill"] = [
        crps_skill_score(m, b) for m, b in zip(merged["crps"], merged["baseline_crps"], strict=True)
    ]
    merged["n_independent"] = merged["n_independent"].astype(int)
    return merged


def var_test_table(rows: pd.DataFrame, levels: list[float]) -> pd.DataFrame:
    """Kupiec and Christoffersen tests on non-overlapping origins only."""
    if rows.empty:
        return pd.DataFrame()
    independent = rows[rows["non_overlapping"]]
    if independent.empty:
        return pd.DataFrame()

    records = []
    for (model, ticker, horizon), group in independent.groupby(
        ["model", "ticker", "horizon"], sort=False
    ):
        ordered = group.sort_values("origin")
        record = {
            "model": model,
            "ticker": ticker,
            "horizon": int(horizon),
            "n_independent": int(len(ordered)),
        }
        for level in levels:
            tag = f"{int(round(level * 100))}"
            column = f"exception_{tag}"
            if column not in ordered:
                continue
            flags = ordered[column].to_numpy(dtype=int)
            expected = 1.0 - level
            pof = kupiec_pof(len(flags), int(flags.sum()), expected)
            ind = christoffersen_independence(flags)
            cc = christoffersen_conditional_coverage(flags, expected)
            record.update(
                {
                    f"exceptions_{tag}": int(flags.sum()),
                    f"exception_rate_{tag}": pof["observed_rate"],
                    f"expected_rate_{tag}": expected,
                    f"kupiec_p_{tag}": pof["p_value"],
                    f"christoffersen_ind_p_{tag}": ind["p_value"],
                    f"christoffersen_cc_p_{tag}": cc["p_value"],
                }
            )
        records.append(record)
    return pd.DataFrame(records)


def pit_tables(rows: pd.DataFrame, bins: int = 10) -> dict[str, pd.DataFrame]:
    """PIT histograms keyed by ``model|horizon``, pooled across tickers.

    Pooling is deliberate: a single ticker at a 252-day horizon has too few observations
    for a histogram to say anything, while the pooled version answers the question that
    actually matters -- is this *model* calibrated at this horizon?
    """
    if rows.empty:
        return {}
    out: dict[str, pd.DataFrame] = {}
    for (model, horizon), group in rows.groupby(["model", "horizon"], sort=False):
        out[f"{model}|{int(horizon)}"] = pit_histogram(group["pit"].to_numpy(), bins=bins)
    return out


def dm_table(rows: pd.DataFrame, baseline: str) -> pd.DataFrame:
    """Diebold-Mariano tests of each model against the baseline, per horizon.

    Paired on the exact (ticker, origin) cells both models scored, using non-overlapping
    origins only. A negative statistic favours the model over the baseline.
    """
    if rows.empty:
        return pd.DataFrame()
    independent = rows[rows["non_overlapping"]]
    if independent.empty:
        return pd.DataFrame()

    pivot = independent.pivot_table(
        index=["ticker", "horizon", "origin"], columns="model", values="crps"
    )
    if baseline not in pivot.columns:
        return pd.DataFrame()

    records = []
    for model in pivot.columns:
        if model == baseline:
            continue
        for horizon, group in pivot.groupby(level="horizon"):
            paired = group[[model, baseline]].dropna()
            if paired.empty:
                continue
            test = diebold_mariano(paired[model].to_numpy(), paired[baseline].to_numpy())
            records.append(
                {
                    "model": model,
                    "baseline": baseline,
                    "horizon": int(horizon),
                    "mean_crps_difference": float((paired[model] - paired[baseline]).mean()),
                    **test,
                }
            )
    return pd.DataFrame(records)


def build_scorecard(result, coverage_levels: list[float], var_levels: list[float]) -> pd.DataFrame:
    """Merge coverage, CRPS skill and VaR tests into one model x ticker x horizon table."""
    rows = result.rows
    if rows.empty:
        return pd.DataFrame()

    keys = ["model", "ticker", "horizon"]
    table = coverage_table(rows, coverage_levels)
    crps = crps_table(rows, result.baseline)
    if not crps.empty:
        table = table.merge(
            crps.drop(columns=["n", "n_independent"], errors="ignore"), on=keys, how="left"
        )
    var_tests = var_test_table(rows, var_levels)
    if not var_tests.empty:
        table = table.merge(
            var_tests.drop(columns=["n_independent"], errors="ignore"), on=keys, how="left"
        )

    table["low_power"] = table["n_independent"] < LOW_POWER_THRESHOLD
    sort_keys = [k for k in ("horizon", "ticker", "model") if k in table.columns]
    return table.sort_values(sort_keys).reset_index(drop=True)


def model_summary(scorecard: pd.DataFrame, baseline: str) -> pd.DataFrame:
    """Per model and horizon, averaged across tickers -- the headline table."""
    if scorecard.empty:
        return pd.DataFrame()
    aggregations = {
        "tickers": ("ticker", "nunique"),
        "n": ("n", "sum"),
        "n_independent": ("n_independent", "sum"),
    }
    for column in scorecard.columns:
        if column.startswith(("coverage_", "crps", "kupiec_p_", "christoffersen_")) and (
            not column.startswith("coverage_error")
        ):
            aggregations[column] = (column, "mean")
    summary = scorecard.groupby(["model", "horizon"], sort=False).agg(**aggregations).reset_index()
    summary["is_baseline"] = summary["model"] == baseline
    return summary.sort_values(["horizon", "model"]).reset_index(drop=True)


def _describe_coverage(value: float, nominal: float) -> str:
    error = value - nominal
    if abs(error) < 0.03:
        return "well calibrated"
    if error < 0:
        return "too narrow (overconfident)" if error < -0.08 else "slightly too narrow"
    return "too wide (underconfident)" if error > 0.08 else "slightly too wide"


def written_summary(
    result,
    scorecard: pd.DataFrame,
    summary: pd.DataFrame,
    pit: dict[str, pd.DataFrame] | None = None,
    dm: pd.DataFrame | None = None,
) -> str:
    """Plain-language reading of the scorecard, written to every backtest run folder.

    States what is calibrated, what is not, and where the sample is too small to tell --
    the last being the part that is easiest to leave out and most misleading to omit.
    """
    if scorecard.empty:
        return "# Backtest summary\n\nNo scoreable cells were produced.\n"

    lines: list[str] = ["# Backtest summary", ""]
    lines.append(
        f"{len(result.origins)} forecast origins from "
        f"{pd.Timestamp(result.origins.min()).date()} to "
        f"{pd.Timestamp(result.origins.max()).date()}; "
        f"{len(result.tickers)} tickers; {result.n_paths:,} paths per origin; "
        f"seed {result.seed}. Baseline: **{result.baseline}**."
    )
    if result.elapsed_seconds:
        lines.append(f"Run time {result.elapsed_seconds / 60:.1f} minutes.")
    lines.append("")

    if not result.failures.empty:
        counts = result.failures.groupby(["model", "stage"]).size()
        lines.append(f"**{len(result.failures)} cells failed** and were excluded:")
        for (model, stage), count in counts.items():
            lines.append(f"- {model} / {stage}: {count}")
        lines.append("")

    lines.append("## Calibration")
    lines.append("")
    for horizon in sorted(summary["horizon"].unique()):
        block = summary[summary["horizon"] == horizon]
        lines.append(f"### {int(horizon)}-day horizon")
        independent = int(block["n_independent"].max()) if not block.empty else 0
        if independent < LOW_POWER_THRESHOLD:
            lines.append(
                f"*Only {independent} non-overlapping observations across all tickers. "
                "Treat everything in this block as indicative; it cannot distinguish a "
                "well-calibrated model from a modestly broken one.*"
            )
        for _, row in block.iterrows():
            parts = [f"- **{row['model']}**"]
            for level in (50, 90):
                column = f"coverage_{level}"
                if column in row and np.isfinite(row[column]):
                    parts.append(
                        f"{level}% interval covers {row[column]:.1%} "
                        f"({_describe_coverage(row[column], level / 100)})"
                    )
            if "crps_skill" in row and np.isfinite(row["crps_skill"]):
                if row["model"] == result.baseline:
                    parts.append("CRPS skill 0.0% by definition (baseline)")
                else:
                    verdict = "better than" if row["crps_skill"] > 0 else "worse than"
                    parts.append(f"CRPS skill {row['crps_skill']:+.1%} ({verdict} baseline)")
            lines.append("; ".join(parts) + ".")
        lines.append("")

    if dm is not None and not dm.empty:
        lines.append("## Is the CRPS difference significant?")
        lines.append("")
        lines.append(
            "Diebold-Mariano on non-overlapping origins. A negative statistic favours the "
            "model; p < 0.05 means the difference is unlikely to be noise."
        )
        for _, row in dm.iterrows():
            if not np.isfinite(row.get("dm_statistic", np.nan)):
                lines.append(
                    f"- {row['model']} at {int(row['horizon'])}d: too few independent "
                    f"observations (n = {int(row['n'])}) to test."
                )
                continue
            verdict = (
                "significantly better"
                if row["dm_statistic"] < 0 and row["p_value"] < 0.05
                else "significantly worse"
                if row["dm_statistic"] > 0 and row["p_value"] < 0.05
                else "not distinguishable"
            )
            lines.append(
                f"- {row['model']} at {int(row['horizon'])}d: DM {row['dm_statistic']:+.2f}, "
                f"p = {row['p_value']:.3f}, n = {int(row['n'])} -- **{verdict}** from "
                f"{row['baseline']}."
            )
        lines.append("")

    lines.append("## VaR exception tests")
    lines.append("")
    lines.append(
        "Kupiec tests whether the *number* of breaches matches the nominal rate; "
        "Christoffersen tests whether they arrive independently rather than in clusters. "
        "Both run on non-overlapping origins only. A small p-value is a rejection."
    )
    for column in ("kupiec_p_95", "kupiec_p_99"):
        if column not in scorecard.columns:
            continue
        level = column.split("_")[-1]
        rejected = scorecard[scorecard[column] < 0.05]
        total = int(scorecard[column].notna().sum())
        worst = ""
        if not rejected.empty:
            names = rejected.nsmallest(3, column).apply(
                lambda r: f"{r.model}/{r.ticker}/{int(r.horizon)}d", axis=1
            )
            worst = f" Worst offenders: {', '.join(names)}."
        lines.append(
            f"- At {level}%: {len(rejected)} of {total} testable cells reject the correct "
            "exception rate." + worst
        )
    lines.append("")

    if pit:
        lines.append("## PIT uniformity")
        lines.append("")
        lines.append(
            "A calibrated forecast puts the outcome at a uniformly distributed percentile "
            "of its own distribution. The chi-squared p-values below are optimistic "
            "because overlapping windows make the observations dependent; read the shape, "
            "not the p-value."
        )
        for key, table in sorted(pit.items()):
            p_value = table.attrs.get("chi2_pvalue", np.nan)
            model, horizon = key.split("|")
            if np.isfinite(p_value):
                shape = "uniform" if p_value > 0.05 else "NOT uniform"
                lines.append(
                    f"- {model} at {horizon}d: {shape} (chi2 p = {p_value:.3f}, "
                    f"n = {table.attrs.get('n', 0)})."
                )
            else:
                lines.append(
                    f"- {model} at {horizon}d: too few observations "
                    f"(n = {table.attrs.get('n', 0)}) to test uniformity."
                )
        lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.extend(
        [
            "- Monthly origins with multi-month horizons overlap heavily. Coverage and PIT "
            "use every origin (they are descriptive); every significance test uses the "
            "non-overlapping subset only.",
            "- 126- and 252-day horizons have few independent observations by construction. "
            "Cells with fewer than "
            f"{LOW_POWER_THRESHOLD} are flagged `low_power` in the scorecard.",
            "- The universe is today's semiconductor leaders, so results carry survivorship "
            "bias: names that failed over this period are absent.",
            "- Models were not tuned on these periods, but they were *designed* with the "
            "2020 and 2022 drawdowns in mind, which is a milder form of the same problem.",
        ]
    )
    lines.append("")
    return "\n".join(lines)

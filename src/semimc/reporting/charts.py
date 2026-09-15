"""Charts.

Every function returns a matplotlib ``Figure`` rather than writing a file, so the same
code serves the CLI, the HTML report, and a notebook -- and so a future dashboard could
reuse it without a refactor.

A deliberate choice runs through all of these: the *distribution* is the subject. There is
no line labelled "the forecast". Fan charts show percentile bands, histograms show the
whole terminal distribution, and the median is drawn dashed and thin so it does not read
as a prediction.
"""

from __future__ import annotations

import base64
import io

import matplotlib

matplotlib.use("Agg")  # no display in a CLI or on CI

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

BAND_COLOR = "#3b6ea5"
ACCENT = "#c1440e"
GRID = {"alpha": 0.25, "linewidth": 0.6}


def figure_to_base64(figure: plt.Figure, dpi: int = 120) -> str:
    """Encode a figure as a base64 PNG for embedding in a self-contained HTML report."""
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=dpi, bbox_inches="tight")
    buffer.seek(0)
    return base64.b64encode(buffer.read()).decode("ascii")


def save_figure(figure: plt.Figure, path, dpi: int = 120) -> None:
    """Write a figure to disk and close it."""
    figure.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(figure)


def fan_chart(
    price_paths: np.ndarray,
    percentiles: list[int],
    title: str,
    spot: float | None = None,
    history: pd.Series | None = None,
) -> plt.Figure:
    """Percentile bands of simulated price paths over the horizon.

    Parameters
    ----------
    price_paths
        Simulated prices including the starting price, shape ``(n_paths, horizon + 1)``.
    percentiles
        Percentiles to band, e.g. ``[5, 25, 50, 75, 95]``. Paired symmetrically outwards
        from the median.
    title
        Chart title.
    spot
        Starting price, drawn as a reference line.
    history
        Optional recent price history to draw to the left of the origin, which makes the
        forecast's width legible next to how the stock has actually been moving.
    """
    figure, axis = plt.subplots(figsize=(10, 5.5))
    steps = np.arange(price_paths.shape[1])
    computed = np.percentile(price_paths, percentiles, axis=0)

    if history is not None and len(history):
        axis.plot(
            np.arange(-len(history) + 1, 1),
            history.to_numpy(dtype=float),
            color="#444444",
            linewidth=1.1,
            label="history",
        )

    pairs = [(p, 100 - p) for p in percentiles if p < 50]
    for index, (low, high) in enumerate(sorted(pairs, key=lambda pair: pair[0])):
        if low not in percentiles or high not in percentiles:
            continue
        lower = computed[percentiles.index(low)]
        upper = computed[percentiles.index(high)]
        axis.fill_between(
            steps,
            lower,
            upper,
            color=BAND_COLOR,
            alpha=0.18 + 0.14 * index,
            linewidth=0,
            label=f"{high - low}% interval",
        )

    if 50 in percentiles:
        axis.plot(
            steps,
            computed[percentiles.index(50)],
            color=BAND_COLOR,
            linestyle="--",
            linewidth=1.2,
            label="median (not a prediction)",
        )
    if spot is not None:
        axis.axhline(spot, color=ACCENT, linewidth=1.0, linestyle=":", label=f"spot {spot:,.2f}")

    axis.set_title(title)
    axis.set_xlabel("trading days from the forecast origin")
    axis.set_ylabel("price (USD)")
    axis.grid(True, **GRID)
    axis.legend(loc="upper left", fontsize=8, framealpha=0.9)
    figure.tight_layout()
    return figure


def terminal_histogram(
    terminal_prices: np.ndarray,
    spot: float,
    title: str,
    percentiles: tuple[int, int] = (5, 95),
    bins: int = 80,
) -> plt.Figure:
    """Distribution of the terminal price, with today's price and the tails marked."""
    figure, axis = plt.subplots(figsize=(10, 5))
    axis.hist(terminal_prices, bins=bins, color=BAND_COLOR, alpha=0.75, edgecolor="none")

    low, high = np.percentile(terminal_prices, percentiles)
    median = float(np.median(terminal_prices))
    axis.axvline(spot, color=ACCENT, linewidth=1.6, label=f"today {spot:,.2f}")
    axis.axvline(
        median, color=BAND_COLOR, linestyle="--", linewidth=1.3, label=f"median {median:,.2f}"
    )
    axis.axvline(
        low, color="#666666", linestyle=":", linewidth=1.1, label=f"p{percentiles[0]} {low:,.2f}"
    )
    axis.axvline(
        high, color="#666666", linestyle=":", linewidth=1.1, label=f"p{percentiles[1]} {high:,.2f}"
    )

    share_below = float(np.mean(terminal_prices < spot))
    axis.set_title(f"{title}\n{share_below:.0%} of paths finish below today's price")
    axis.set_xlabel("terminal price (USD)")
    axis.set_ylabel("paths")
    axis.grid(True, axis="y", **GRID)
    axis.legend(fontsize=8)
    figure.tight_layout()
    return figure


def convergence_chart(table: pd.DataFrame, title: str = "Monte Carlo convergence") -> plt.Figure:
    """The 5th percentile and CVaR against path count, with standard-error ribbons."""
    figure, axes = plt.subplots(1, 2, figsize=(11, 4))
    level = int(round(100 * table.attrs.get("var_level", 0.95)))
    for axis, (value, error, label) in zip(
        axes,
        [("p5_return", "p5_se", "5th percentile return"), ("cvar", "cvar_se", f"{level}% CVaR")],
        strict=True,
    ):
        axis.plot(table["n_paths"], table[value], marker="o", color=BAND_COLOR, linewidth=1.4)
        axis.fill_between(
            table["n_paths"],
            table[value] - 2 * table[error],
            table[value] + 2 * table[error],
            color=BAND_COLOR,
            alpha=0.2,
            linewidth=0,
            label="+/- 2 standard errors",
        )
        axis.set_xscale("log")
        axis.set_xlabel("paths (log scale)")
        axis.set_ylabel(label)
        axis.grid(True, **GRID)
        axis.legend(fontsize=8)
    figure.suptitle(title)
    figure.tight_layout()
    return figure


def correlation_heatmap(
    matrix: pd.DataFrame, title: str = "Daily return correlations"
) -> plt.Figure:
    """Correlation heatmap over the universe."""
    figure, axis = plt.subplots(figsize=(9, 7.5))
    image = axis.imshow(matrix.to_numpy(), vmin=0, vmax=1, cmap="YlGnBu")
    axis.set_xticks(range(len(matrix.columns)), matrix.columns, rotation=90, fontsize=8)
    axis.set_yticks(range(len(matrix.index)), matrix.index, fontsize=8)
    for i in range(len(matrix.index)):
        for j in range(len(matrix.columns)):
            value = matrix.iloc[i, j]
            if np.isfinite(value):
                axis.text(
                    j,
                    i,
                    f"{value:.2f}",
                    ha="center",
                    va="center",
                    fontsize=6,
                    color="white" if value > 0.6 else "black",
                )
    figure.colorbar(image, ax=axis, fraction=0.035)
    axis.set_title(title)
    figure.tight_layout()
    return figure


def pit_chart(table: pd.DataFrame, title: str) -> plt.Figure:
    """PIT histogram against the uniform frequency a calibrated model would produce."""
    figure, axis = plt.subplots(figsize=(6, 3.6))
    centres = (table["bin_low"] + table["bin_high"]) / 2
    width = float(table["bin_high"].iloc[0] - table["bin_low"].iloc[0]) * 0.9
    axis.bar(centres, table["count"], width=width, color=BAND_COLOR, alpha=0.8)
    if np.isfinite(table["expected"]).all():
        axis.axhline(
            float(table["expected"].iloc[0]),
            color=ACCENT,
            linestyle="--",
            linewidth=1.2,
            label="uniform",
        )
    axis.set_title(title, fontsize=10)
    axis.set_xlabel("PIT (outcome's percentile in the forecast)")
    axis.set_ylabel("count")
    axis.legend(fontsize=8)
    axis.grid(True, axis="y", **GRID)
    figure.tight_layout()
    return figure


def coverage_chart(summary: pd.DataFrame, level: int = 90) -> plt.Figure:
    """Realized coverage by model and horizon against the nominal level."""
    column = f"coverage_{level}"
    figure, axis = plt.subplots(figsize=(8, 4.2))
    if column not in summary.columns or summary.empty:
        axis.text(0.5, 0.5, "no coverage data", ha="center", va="center")
        return figure

    horizons = sorted(summary["horizon"].unique())
    models = list(dict.fromkeys(summary["model"]))
    width = 0.8 / max(len(models), 1)
    positions = np.arange(len(horizons))

    for index, model in enumerate(models):
        block = summary[summary["model"] == model].set_index("horizon").reindex(horizons)
        axis.bar(
            positions + index * width,
            block[column].to_numpy(dtype=float),
            width=width,
            label=model,
        )
    axis.axhline(level / 100, color=ACCENT, linestyle="--", linewidth=1.4, label="nominal")
    axis.set_xticks(positions + width * (len(models) - 1) / 2, [f"{h}d" for h in horizons])
    axis.set_ylabel(f"realized coverage of the {level}% interval")
    axis.set_ylim(0, 1.05)
    axis.legend(fontsize=8, ncol=max(1, len(models) // 2))
    axis.grid(True, axis="y", **GRID)
    axis.set_title(f"Coverage of the nominal {level}% interval")
    figure.tight_layout()
    return figure


def crps_skill_chart(summary: pd.DataFrame, baseline: str) -> plt.Figure:
    """CRPS skill against the baseline, by model and horizon."""
    figure, axis = plt.subplots(figsize=(8, 4.2))
    if "crps_skill" not in summary.columns or summary.empty:
        axis.text(0.5, 0.5, "no CRPS data", ha="center", va="center")
        return figure

    horizons = sorted(summary["horizon"].unique())
    models = [m for m in dict.fromkeys(summary["model"]) if m != baseline]
    if not models:
        axis.text(
            0.5,
            0.5,
            f"only the baseline ({baseline}) was run,\nso there is no skill score to plot",
            ha="center",
            va="center",
        )
        axis.set_axis_off()
        return figure

    width = 0.8 / max(len(models), 1)
    positions = np.arange(len(horizons))

    for index, model in enumerate(models):
        block = summary[summary["model"] == model].set_index("horizon").reindex(horizons)
        axis.bar(
            positions + index * width,
            block["crps_skill"].to_numpy(dtype=float),
            width=width,
            label=model,
        )
    axis.axhline(0, color=ACCENT, linewidth=1.2)
    axis.set_xticks(positions + width * (len(models) - 1) / 2, [f"{h}d" for h in horizons])
    axis.set_ylabel(f"CRPS skill vs {baseline}")
    axis.set_title(f"CRPS skill score against {baseline} (positive is better)")
    axis.legend(fontsize=8)
    axis.grid(True, axis="y", **GRID)
    figure.tight_layout()
    return figure


def drawdown_histogram(max_drawdowns: np.ndarray, title: str) -> plt.Figure:
    """Distribution of per-path maximum drawdown."""
    figure, axis = plt.subplots(figsize=(8, 4))
    axis.hist(max_drawdowns, bins=60, color="#7a4a8c", alpha=0.8, edgecolor="none")
    median = float(np.median(max_drawdowns))
    axis.axvline(median, color=ACCENT, linestyle="--", linewidth=1.4, label=f"median {median:.1%}")
    axis.set_title(title)
    axis.set_xlabel("maximum drawdown along the path")
    axis.set_ylabel("paths")
    axis.legend(fontsize=8)
    axis.grid(True, axis="y", **GRID)
    figure.tight_layout()
    return figure

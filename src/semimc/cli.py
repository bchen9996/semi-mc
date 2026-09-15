"""Command line interface.

Commands mirror the build phases: ``fetch`` and ``validate`` (data), ``simulate``
(forecast), ``backtest`` (the scoreboard), ``scenario`` (stress), ``report`` (write-up).

Two rules hold across all of them: every command that produces output writes a
``runs/<timestamp>/`` folder containing its config snapshot and seed, and nothing prints a
point prediction.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import numpy as np
import pandas as pd
import typer

from .config import Config, load_config

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Monte Carlo forecasting for semiconductor stocks. Distributions, not predictions.",
)

LOGGER = logging.getLogger("semimc")

ConfigOption = Annotated[Path | None, typer.Option("--config", "-c", help="Path to a YAML config.")]
SetOption = Annotated[
    list[str] | None,
    typer.Option("--set", "-s", help="Override a config key, e.g. -s drift.policy=risk_free."),
]
VerboseOption = Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")]


def _setup(config_path: Path | None, overrides: list[str] | None, verbose: bool) -> Config:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    return load_config(config_path, overrides)


def _echo_table(frame: pd.DataFrame | pd.Series, floatfmt: str = "%.4f") -> None:
    with pd.option_context(
        "display.max_rows",
        200,
        "display.width",
        160,
        "display.float_format",
        lambda v: floatfmt % v,
    ):
        typer.echo(frame.to_string())


def _parse_weights(text: str | None, tickers: list[str]) -> pd.Series | None:
    """Parse ``NVDA=0.4,AMD=0.6``; an empty value means equal weight."""
    if text is None:
        return None
    if not text.strip():
        return pd.Series(1.0 / len(tickers), index=tickers)
    weights: dict[str, float] = {}
    for part in text.split(","):
        if "=" not in part:
            raise typer.BadParameter(f"expected TICKER=weight, got {part!r}")
        name, value = part.split("=", 1)
        weights[name.strip().upper()] = float(value)
    return pd.Series(weights, dtype=float)


# ---------------------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------------------


@app.command()
def fetch(
    tickers: Annotated[
        str | None,
        typer.Option("--tickers", "-t", help="Comma-separated; default is the universe."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Refetch even if the cache is fresh.")
    ] = False,
    risk_free: Annotated[
        bool, typer.Option("--risk-free", help="Also download the 3-month T-bill series from FRED.")
    ] = False,
    earnings: Annotated[
        bool,
        typer.Option("--earnings", help="Also seed the earnings calendar CSV from the provider."),
    ] = False,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Download and cache adjusted daily prices."""
    cfg = _setup(config, set_, verbose)
    from .data import refresh_cache
    from .data.earnings import fetch_calendar_from_provider, load_calendar
    from .data.riskfree import fetch_risk_free_series

    symbols = [t.strip().upper() for t in tickers.split(",")] if tickers else cfg.all_tickers
    typer.echo(f"Fetching {len(symbols)} tickers into {cfg.cache_path} ...")
    statuses = refresh_cache(cfg, symbols, force=force)

    frame = pd.DataFrame(
        [
            {
                "ticker": s.ticker,
                "action": s.action,
                "rows": s.rows,
                "first": None if s.first_date is None else pd.Timestamp(s.first_date).date(),
                "last": None if s.last_date is None else pd.Timestamp(s.last_date).date(),
                "detail": s.detail,
            }
            for s in statuses
        ]
    )
    _echo_table(frame.set_index("ticker"))

    failed = frame[frame["action"] == "failed"]
    if not failed.empty:
        typer.secho(
            f"\n{len(failed)} ticker(s) failed: {', '.join(failed['ticker'])}. "
            "They are excluded, not filled.",
            fg=typer.colors.RED,
        )

    if risk_free:
        typer.echo("\nDownloading the 3-month T-bill series (FRED DTB3) ...")
        try:
            series = fetch_risk_free_series()
            destination = cfg.path(cfg.backtest.risk_free_series)
            destination.parent.mkdir(parents=True, exist_ok=True)
            series.rename("rate").to_frame().to_csv(destination, index_label="date")
            typer.echo(
                f"  {len(series):,} rows, {series.index.min().date()} to "
                f"{series.index.max().date()}, written to {destination}"
            )
        except Exception as exc:  # noqa: BLE001
            typer.secho(f"  failed: {exc}", fg=typer.colors.RED)

    if earnings:
        typer.echo("\nSeeding the earnings calendar from the provider ...")
        fetched = fetch_calendar_from_provider(
            [t for t in symbols if t not in cfg.benchmarks.tickers]
        )
        destination = cfg.path(cfg.data.earnings_calendar)
        if fetched.empty:
            typer.secho("  the provider returned no earnings dates.", fg=typer.colors.YELLOW)
        else:
            existing = load_calendar(destination).events
            combined = (
                pd.concat([existing, fetched], ignore_index=True)
                .drop_duplicates(subset=["ticker", "date"])
                .sort_values(["ticker", "date"])
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            combined.to_csv(destination, index=False)
            typer.secho(
                f"  {len(fetched)} rows fetched, {len(combined)} total in {destination}.\n"
                "  Provider coverage is only a few recent quarters and timing is often "
                "missing. Review and extend this file by hand before relying on the "
                "earnings model.",
                fg=typer.colors.YELLOW,
            )


# ---------------------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------------------


@app.command()
def validate(
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Check cached data and write a validation report."""
    cfg = _setup(config, set_, verbose)
    from .data import load_market_data
    from .data.returns import correlation_matrix, summary_statistics
    from .data.validation import validate_prices
    from .reporting.charts import correlation_heatmap, save_figure
    from .runs import RunDirectory

    market = load_market_data(cfg)
    run = RunDirectory.create(cfg.runs_path, "validate", cfg.to_dict())
    run.record_data_range(market.prices, "prices")

    report = validate_prices(
        prices=market.prices,
        requested=cfg.all_tickers,
        volumes=market.volumes,
        min_observations=cfg.fit.min_observations,
        max_gap_days=cfg.data.max_gap_days,
        outlier_sigma=cfg.data.outlier_sigma,
    )

    typer.echo(f"\nValidation over {len(cfg.all_tickers)} requested tickers:")
    summary = report.summary()
    _echo_table(summary if not summary.empty else pd.DataFrame({"issues": []}), "%.0f")
    run.write_table(report.to_frame(), "validation_issues", index=False)
    run.write_table(summary, "validation_summary")

    if report.errors:
        typer.secho(f"\n{len(report.errors)} ERROR-level issues:", fg=typer.colors.RED)
        for issue in report.errors[:20]:
            typer.echo(f"  {issue.ticker}: {issue.detail}")

    market_returns = market.log_returns.get(cfg.benchmarks.market)
    sector_returns = market.log_returns.get(cfg.benchmarks.sector)
    stats = summary_statistics(
        market.log_returns.loc[:, [t for t in cfg.universe.tickers if t in market.log_returns]],
        market=market_returns,
        sector=sector_returns,
        trading_days_per_year=cfg.simulation.trading_days_per_year,
    )
    typer.echo("\nSummary statistics:")
    _echo_table(stats)
    run.write_table(stats, "summary_statistics")

    correlations = correlation_matrix(
        market.log_returns.loc[:, [t for t in cfg.all_tickers if t in market.log_returns]]
    )
    run.write_table(correlations, "correlation_matrix")
    save_figure(
        correlation_heatmap(correlations), run.figure_path("correlations"), cfg.reporting.figure_dpi
    )

    run.record("n_issues", len(report.issues))
    run.record("n_errors", len(report.errors))
    typer.echo(f"\nWritten to {run.close()}")


# ---------------------------------------------------------------------------------------
# simulate
# ---------------------------------------------------------------------------------------


@app.command()
def simulate(
    ticker: Annotated[str | None, typer.Option("--ticker", "-t", help="Single ticker.")] = None,
    portfolio: Annotated[
        str | None,
        typer.Option(
            "--portfolio",
            "-p",
            help="Portfolio weights, e.g. 'NVDA=0.5,AMD=0.5'. Empty string means equal weight "
            "across the whole universe.",
        ),
    ] = None,
    horizon: Annotated[int, typer.Option("--horizon", "-h", help="Trading days.")] = 126,
    model: Annotated[str, typer.Option("--model", "-m", help="Model name.")] = "gbm",
    paths: Annotated[
        int | None, typer.Option("--paths", help="Overrides simulation.n_paths.")
    ] = None,
    price_target: Annotated[
        float | None,
        typer.Option("--above", help="Report the probability of finishing above this price."),
    ] = None,
    seed: Annotated[int | None, typer.Option("--seed", help="Overrides simulation.seed.")] = None,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Simulate a forecast distribution for one ticker or a weighted portfolio."""
    cfg = _setup(config, set_, verbose)
    from .analytics.convergence import convergence_table, summarize_convergence
    from .analytics.risk import contribution_to_cvar, risk_table
    from .data import load_market_data
    from .engine.runner import run_simulation
    from .models import build_model
    from .reporting.charts import (
        convergence_chart,
        drawdown_histogram,
        fan_chart,
        save_figure,
        terminal_histogram,
    )
    from .runs import RunDirectory

    if (ticker is None) == (portfolio is None):
        raise typer.BadParameter("give exactly one of --ticker or --portfolio")

    weights = _parse_weights(portfolio, cfg.universe.tickers)
    if weights is not None and portfolio.strip() == "":
        symbols = list(cfg.universe.tickers)
        weights = pd.Series(1.0 / len(symbols), index=symbols)
    elif weights is not None:
        symbols = list(weights.index)
    else:
        symbols = [ticker.strip().upper()]

    n_paths = paths or cfg.simulation.n_paths
    used_seed = seed if seed is not None else cfg.simulation.seed
    rng = np.random.default_rng(used_seed)

    market = load_market_data(cfg, tickers=list(dict.fromkeys(symbols + cfg.benchmarks.tickers)))
    as_of = market.last_date
    training = market.training_window(symbols, as_of)
    context = market.context(as_of)

    typer.echo(
        f"Fitting {model} on {len(training):,} days to {as_of.date()} "
        f"({training.index.min().date()} onward), drift policy {cfg.drift.policy}."
    )
    fitted = build_model(model, cfg).fit(training, context)

    run = RunDirectory.create(
        cfg.runs_path,
        "simulate",
        cfg.to_dict(),
        seed=used_seed,
        label=(symbols[0] if weights is None else "portfolio"),
    )
    run.record_data_range(training, "training")
    run.record("model", fitted.describe())
    run.record("horizon", horizon)
    run.record("n_paths", n_paths)
    run.record("as_of", str(as_of.date()))

    spot = market.spot(symbols, as_of)
    result = run_simulation(
        model=fitted,
        spot=spot,
        n_paths=n_paths,
        horizon=horizon,
        rng=rng,
        chunk_size=cfg.simulation.chunk_size,
        dtype=cfg.simulation.dtype,
        track=symbols if weights is None else [],
        portfolio_weights=weights,
        rebalance=cfg.portfolio.rebalance,
    )

    percentiles = cfg.reporting.fan_percentiles
    if weights is None:
        name = symbols[0]
        terminal = result.terminal_for(name)
        drawdowns = result.max_drawdown[:, 0]
        table = risk_table(
            terminal,
            float(spot[name]),
            max_drawdown=drawdowns,
            var_levels=cfg.risk.var_levels,
            loss_threshold=cfg.risk.loss_threshold,
            price_threshold=price_target,
            horizon=horizon,
        )
        paths_for_chart = result.tracked_paths[name]
        history = market.prices[name].dropna().iloc[-126:]
        save_figure(
            fan_chart(
                paths_for_chart,
                percentiles,
                f"{name}: {horizon}-day simulated paths ({model})",
                float(spot[name]),
                history,
            ),
            run.figure_path("fan_chart"),
            cfg.reporting.figure_dpi,
        )
        save_figure(
            terminal_histogram(
                paths_for_chart[:, -1],
                float(spot[name]),
                f"{name}: terminal price in {horizon} days",
            ),
            run.figure_path("terminal_distribution"),
            cfg.reporting.figure_dpi,
        )
    else:
        name = "PORTFOLIO"
        terminal = np.log(np.clip(result.portfolio_value, 1e-12, None))
        drawdowns = result.portfolio_max_drawdown
        table = risk_table(
            terminal,
            1.0,
            max_drawdown=drawdowns,
            var_levels=cfg.risk.var_levels,
            loss_threshold=cfg.risk.loss_threshold,
            horizon=horizon,
        )
        contributions = contribution_to_cvar(
            result.terminal_log_returns, weights.reindex(result.tickers).to_numpy(dtype=float)
        )
        contributions.index = result.tickers
        contributions = contributions.sort_values(ascending=False)
        typer.echo("\nContribution to 95% CVaR (sums to portfolio CVaR):")
        _echo_table(contributions.rename("contribution"))
        run.write_table(contributions.rename("contribution"), "cvar_contributions")
        save_figure(
            terminal_histogram(
                np.exp(terminal), 1.0, f"Portfolio: terminal value in {horizon} days (start = 1.0)"
            ),
            run.figure_path("terminal_distribution"),
            cfg.reporting.figure_dpi,
        )

    save_figure(
        drawdown_histogram(drawdowns, f"{name}: maximum drawdown over {horizon} days"),
        run.figure_path("drawdowns"),
        cfg.reporting.figure_dpi,
    )

    typer.echo(f"\nRisk table -- {name}, {horizon} trading days, {n_paths:,} paths, model {model}:")
    _echo_table(table)
    run.write_table(table.rename("value"), "risk_table")

    convergence = convergence_table(terminal, var_level=max(cfg.risk.var_levels), rng=rng)
    typer.echo("\nConvergence:")
    _echo_table(convergence)
    run.write_table(convergence, "convergence", index=False)
    save_figure(
        convergence_chart(convergence), run.figure_path("convergence"), cfg.reporting.figure_dpi
    )
    typer.echo("\n" + summarize_convergence(convergence))

    run.write_table(
        pd.DataFrame({"terminal_log_return": terminal, "max_drawdown": drawdowns}),
        "terminal_sample",
        index=False,
    )
    typer.echo(f"\nWritten to {run.close()}")


# ---------------------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------------------


@app.command()
def backtest(
    models: Annotated[
        str | None, typer.Option("--models", help="Comma-separated model names.")
    ] = None,
    horizons: Annotated[
        str | None, typer.Option("--horizons", help="Comma-separated trading-day horizons.")
    ] = None,
    tickers: Annotated[str | None, typer.Option("--tickers", "-t", help="Comma-separated.")] = None,
    start: Annotated[str | None, typer.Option("--start", help="First forecast origin.")] = None,
    end: Annotated[str | None, typer.Option("--end", help="Last forecast origin.")] = None,
    paths: Annotated[int | None, typer.Option("--paths", help="Paths per origin.")] = None,
    seed: Annotated[int | None, typer.Option("--seed")] = None,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Walk-forward backtest: the scoreboard every model has to face."""
    cfg = _setup(config, set_, verbose)
    from .backtest.scorecard import (
        build_scorecard,
        dm_table,
        model_summary,
        pit_tables,
        written_summary,
    )
    from .backtest.walkforward import run_walk_forward
    from .data import load_market_data
    from .reporting.charts import coverage_chart, crps_skill_chart, pit_chart, save_figure
    from .runs import RunDirectory

    model_names = [m.strip() for m in models.split(",")] if models else list(cfg.backtest.models)
    horizon_list = [int(h) for h in horizons.split(",")] if horizons else list(cfg.horizons)
    symbols = [t.strip().upper() for t in tickers.split(",")] if tickers else cfg.universe.tickers

    market = load_market_data(
        cfg, tickers=list(dict.fromkeys(symbols + cfg.benchmarks.tickers)), for_backtest=True
    )
    if not market.risk_free.is_point_in_time:
        typer.secho(
            "WARNING: no historical risk-free series found, so drift is NOT point-in-time. "
            "Run `semimc fetch --risk-free` for a clean backtest.",
            fg=typer.colors.YELLOW,
        )

    run = RunDirectory.create(
        cfg.runs_path, "backtest", cfg.to_dict(), seed=seed or cfg.simulation.seed
    )
    result = run_walk_forward(
        market=market,
        config=cfg,
        models=model_names,
        tickers=symbols,
        horizons=horizon_list,
        start=pd.Timestamp(start) if start else None,
        end=pd.Timestamp(end) if end else None,
        n_paths=paths,
        seed=seed,
    )

    if result.is_empty:
        typer.secho("No scoreable cells were produced.", fg=typer.colors.RED)
        if not result.failures.empty:
            _echo_table(result.failures.head(20))
        run.close()
        raise typer.Exit(code=1)

    scorecard = build_scorecard(result, cfg.backtest.coverage_levels, cfg.risk.var_levels)
    summary = model_summary(scorecard, result.baseline)
    pit = pit_tables(result.rows, bins=cfg.backtest.pit_bins)
    dm = dm_table(result.rows, result.baseline)

    typer.echo("\nModel summary (averaged across tickers):")
    _echo_table(summary)

    run.write_table(result.rows, "backtest_rows", index=False)
    run.write_table(scorecard, "scorecard", index=False)
    run.write_table(summary, "model_summary", index=False)
    if not dm.empty:
        run.write_table(dm, "diebold_mariano", index=False)
    if not result.failures.empty:
        run.write_table(result.failures, "failures", index=False)
        typer.secho(
            f"\n{len(result.failures)} cells failed; see failures.csv", fg=typer.colors.YELLOW
        )
    for key, table in pit.items():
        safe = key.replace("|", "_")
        run.write_table(table, f"pit_{safe}", index=False)
        save_figure(
            pit_chart(table, f"PIT: {key}"),
            run.figure_path(f"pit_{safe}"),
            cfg.reporting.figure_dpi,
        )

    for level in (50, 90):
        if f"coverage_{level}" in summary.columns:
            save_figure(
                coverage_chart(summary, level),
                run.figure_path(f"coverage_{level}"),
                cfg.reporting.figure_dpi,
            )
    save_figure(
        crps_skill_chart(summary, result.baseline),
        run.figure_path("crps_skill"),
        cfg.reporting.figure_dpi,
    )

    text = written_summary(result, scorecard, summary, pit, dm)
    run.write_text(text, "summary")
    run.record("elapsed_seconds", round(result.elapsed_seconds, 1))
    run.record("fit_counts", result.fit_counts)
    run.record("n_origins", len(result.origins))
    typer.echo("\n" + text)
    typer.echo(f"\nWritten to {run.close()}")


# ---------------------------------------------------------------------------------------
# scenario
# ---------------------------------------------------------------------------------------


@app.command()
def scenario(
    name: Annotated[str, typer.Option("--name", "-n", help="Scenario name from the config.")],
    tickers: Annotated[str | None, typer.Option("--tickers", "-t")] = None,
    horizon: Annotated[int, typer.Option("--horizon", "-h")] = 63,
    model: Annotated[str, typer.Option("--model", "-m")] = "gbm",
    paths: Annotated[int | None, typer.Option("--paths")] = None,
    equal_weight: Annotated[
        bool, typer.Option("--equal-weight", help="Also report an equal-weighted portfolio.")
    ] = True,
    seed: Annotated[int | None, typer.Option("--seed")] = None,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Run a stress scenario over the universe."""
    cfg = _setup(config, set_, verbose)
    from .data import load_market_data
    from .data.returns import estimate_betas
    from .runs import RunDirectory
    from .scenarios.stress import run_scenario

    if name not in cfg.scenarios:
        raise typer.BadParameter(
            f"unknown scenario {name!r}; configured: {', '.join(cfg.scenarios) or 'none'}"
        )

    symbols = [t.strip().upper() for t in tickers.split(",")] if tickers else cfg.universe.tickers
    n_paths = paths or cfg.simulation.n_paths
    used_seed = seed if seed is not None else cfg.simulation.seed
    rng = np.random.default_rng(used_seed)

    market = load_market_data(cfg, tickers=list(dict.fromkeys(symbols + cfg.benchmarks.tickers)))
    as_of = market.last_date
    available = [t for t in symbols if t in market.log_returns.columns]
    training = market.training_window(available, as_of, require_complete=True)
    context = market.context(as_of)

    from .models import build_model

    fitted = build_model(model, cfg).fit(training, context)
    simulated = np.asarray(fitted.simulate(n_paths, horizon, rng), dtype=float)

    sector = market.log_returns.get(cfg.benchmarks.sector)
    market_returns = market.log_returns.get(cfg.benchmarks.market)
    weights = pd.Series(1.0 / len(fitted.tickers), index=fitted.tickers) if equal_weight else None

    result = run_scenario(
        name=name,
        spec=cfg.scenarios[name],
        log_return_paths=simulated,
        tickers=fitted.tickers,
        betas_sector=estimate_betas(training, sector) if sector is not None else None,
        betas_market=estimate_betas(training, market_returns)
        if market_returns is not None
        else None,
        daily_drift=fitted.daily_log_drift(horizon),
        benchmark_returns=sector if sector is not None else market_returns,
        weights=weights,
    )

    run = RunDirectory.create(cfg.runs_path, "scenario", cfg.to_dict(), seed=used_seed, label=name)
    run.record("scenario", {"name": name, "kind": result.kind, "description": result.description})
    run.record("horizon", horizon)
    run.record("model", model)
    run.record_data_range(training, "training")

    typer.echo(f"\nScenario: {name}\n{result.description}\n")
    table = result.table()
    _echo_table(table)
    run.write_table(table, "scenario_impact")
    typer.echo(
        "\nThese are conditional outcomes, not forecasts: no probability attaches to the "
        "scenario itself."
    )
    typer.echo(f"\nWritten to {run.close()}")


# ---------------------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------------------


@app.command()
def report(
    run: Annotated[str, typer.Option("--run", help="Run folder name, or 'latest'.")] = "latest",
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Build a self-contained HTML report for a run."""
    cfg = _setup(config, set_, verbose)
    import base64
    import json

    from .reporting.html import _image, _table, build_report, write_report
    from .runs import find_run, load_manifest

    folder = find_run(cfg.runs_path, run)
    manifest = load_manifest(folder)
    command = manifest.get("command", "run")

    metadata = {
        "Run": folder.name,
        "Command": command,
        "Created": manifest.get("created_at", "-"),
        "Seed": manifest.get("seed", "-"),
        "Model": (manifest.get("model") or {}).get("model", manifest.get("model", "-"))
        if isinstance(manifest.get("model"), dict)
        else manifest.get("model", "-"),
        "Horizon (days)": manifest.get("horizon", "-"),
        "Paths": f"{manifest.get('n_paths', 0):,}" if manifest.get("n_paths") else "-",
        "Drift policy": cfg.drift.policy,
        "Python": manifest.get("python", "-"),
    }
    training_range = manifest.get("training_range") or manifest.get("prices_range")
    if training_range:
        metadata["Data range"] = (
            f"{training_range['first_date']} to {training_range['last_date']} "
            f"({training_range['rows']:,} rows)"
        )

    sections: list[tuple[str, str]] = []

    summary_path = folder / "summary.md"
    if summary_path.exists():
        from .reporting.html import _markdown_lite

        sections.append(("Findings", _markdown_lite(summary_path.read_text(encoding="utf-8"))))

    for stem, heading, note in [
        (
            "risk_table",
            "Risk table",
            "VaR and CVaR are positive losses as a fraction of the starting value.",
        ),
        ("model_summary", "Backtest scorecard (by model and horizon)", ""),
        ("scorecard", "Backtest scorecard (by model, ticker and horizon)", ""),
        (
            "scenario_impact",
            "Scenario impact",
            "Conditional outcomes; no probability attaches to the scenario.",
        ),
        ("cvar_contributions", "Contribution to CVaR", "Components sum to the portfolio CVaR."),
        ("summary_statistics", "Universe statistics", ""),
        (
            "convergence",
            "Monte Carlo convergence",
            "Standard errors shrink with the square root of the path count.",
        ),
        (
            "validation_summary",
            "Data validation",
            "Counts of issues by ticker; nothing was filled or dropped silently.",
        ),
    ]:
        path = folder / f"{stem}.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path, index_col=0 if stem not in {"convergence"} else None)
        body = (f'<p class="sub">{note}</p>' if note else "") + _table(frame)
        sections.append((heading, body))

    images = sorted(folder.glob("*.png"))
    if images:
        blocks = []
        for image_path in images:
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            blocks.append(_image(encoded, image_path.stem.replace("_", " ")))
        sections.append(("Charts", "\n".join(blocks)))

    sections.append(
        (
            "Reproducing this run",
            "<p>The exact configuration is saved beside this report as "
            "<code>config_snapshot.json</code>. With that file and the seed above, the run "
            "is reproducible byte for byte.</p>"
            f"<pre><code>{json.dumps(manifest, indent=2)[:4000]}</code></pre>",
        )
    )

    html_text = build_report(
        title=f"semimc {command} report",
        subtitle=f"{folder.name} - distributions and probabilities, not price targets",
        metadata=metadata,
        sections=sections,
    )
    destination = write_report(folder / "report.html", html_text)
    typer.echo(f"Report written to {destination}")


@app.command()
def models() -> None:
    """List the available models."""
    from .models import MODEL_NAMES

    typer.echo("Available models:")
    for name in MODEL_NAMES:
        typer.echo(f"  {name}")


if __name__ == "__main__":  # pragma: no cover
    app()

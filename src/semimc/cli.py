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
    industry: Annotated[
        bool,
        typer.Option(
            "--industry",
            help="Also seed the industry-cycle series (addendum signal 4) from FRED. "
            "Release dates are approximated -- see signals/external.py.",
        ),
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
            # Concatenating an all-empty frame changes dtype inference in pandas, so only
            # merge when there is something to merge with.
            frames = [f for f in (existing, fetched) if not f.empty]
            combined = (
                pd.concat(frames, ignore_index=True)
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

    if industry:
        from .signals.external import (
            ExternalSeriesError,
            fetch_fred_series,
            with_approximate_release_dates,
            write_external_series,
        )

        typer.echo(f"\nDownloading {cfg.signals.industry_series_id} from FRED ...")
        try:
            raw = fetch_fred_series(cfg.signals.industry_series_id)
            frame = with_approximate_release_dates(
                raw, cfg.signals.industry_series_id, cfg.signals.industry_publication_lag_days
            )
            series_id = cfg.signals.industry_series_id
            destination = cfg.path(cfg.signals.external_dir) / f"{series_id}.csv"
            write_external_series(frame, destination)
            typer.secho(
                f"  {len(frame):,} rows, {frame['period'].min().date()} to "
                f"{frame['period'].max().date()}, written to {destination}.\n"
                "  This is FRED's revised, current vintage -- not the value as first "
                "published -- and release_date is approximated as period + "
                f"{cfg.signals.industry_publication_lag_days}d, not a fact. Revision bias "
                "is a real limitation on this signal; see MODEL_NOTES.",
                fg=typer.colors.YELLOW,
            )
        except ExternalSeriesError as exc:
            typer.secho(f"  failed: {exc}", fg=typer.colors.RED)


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
    tilt: Annotated[
        bool,
        typer.Option(
            "--tilt",
            help="Apply the addendum's Phase 4B regression-based drift tilt on top of the "
            "configured policy drift. Fits the pooled relative-signal regression on "
            "everything available up to the forecast origin; if there isn't enough "
            "history the run proceeds untilted and says so.",
        ),
    ] = False,
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

    tilt_diagnostics: dict | None = None
    if tilt:
        from .forecasting.pipeline import fit_live_tilt, load_supply_chain_links_if_present

        links = load_supply_chain_links_if_present(cfg.path(cfg.signals.supply_chain_links))
        outcome = fit_live_tilt(
            market,
            cfg,
            symbols,
            as_of,
            horizon_days=horizon,
            supply_chain_links=links,
            rng=np.random.default_rng(used_seed),
        )
        if outcome.tilt is None:
            typer.secho(
                f"--tilt requested but no tilt could be fitted ({outcome.reason}); "
                "proceeding on the policy drift alone.",
                fg=typer.colors.YELLOW,
            )
        else:
            context = context.with_tilt(outcome.tilt)
            typer.echo(
                f"Drift tilt fitted on {outcome.n_training_rows} rows across "
                f"{outcome.n_origins} origins ({', '.join(outcome.feature_columns)}): "
                f"{outcome.tilt.describe()['annual_tilt']}"
            )
        tilt_diagnostics = {
            "applied": outcome.tilt is not None,
            "reason": outcome.reason,
            "feature_columns": outcome.feature_columns,
            "n_training_rows": outcome.n_training_rows,
            "n_origins": outcome.n_origins,
            "diagnostics": outcome.diagnostics,
        }

    typer.echo(
        f"Fitting {model} on {len(training):,} days to {as_of.date()} "
        f"({training.index.min().date()} onward), drift policy {cfg.drift.policy}."
    )
    fitted = build_model(model, cfg, market=market).fit(training, context)

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
    if tilt_diagnostics is not None:
        run.record("drift_tilt", tilt_diagnostics)

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
    # The config snapshot records the *file*, not what the CLI overrode on top of it, so
    # a run invoked with --models/--horizons/--tickers could not be reproduced from its
    # own record. Store what was actually run.
    run.record(
        "effective",
        {
            "models": result.models,
            "horizons": result.horizons,
            "tickers": result.tickers,
            "baseline": result.baseline,
            "n_paths": result.n_paths,
            "seed": result.seed,
            "drift_policy": cfg.drift.policy,
            "first_origin": str(pd.Timestamp(result.origins.min()).date()),
            "last_origin": str(pd.Timestamp(result.origins.max()).date()),
            "risk_free_point_in_time": market.risk_free.is_point_in_time,
        },
    )
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

    fitted = build_model(model, cfg, market=market).fit(training, context)
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


# ---------------------------------------------------------------------------------------
# signals (addendum Phase 4B)
# ---------------------------------------------------------------------------------------


@app.command()
def signals(
    tickers: Annotated[str | None, typer.Option("--tickers", "-t", help="Comma-separated.")] = None,
    horizon: Annotated[
        int | None,
        typer.Option("--horizon", "-h", help="Trading days. Defaults to signals.horizons[0]."),
    ] = None,
    holdout: Annotated[
        bool,
        typer.Option(
            "--holdout",
            help="Also evaluate the frozen holdout window. Reported once, whatever it "
            "shows -- refuses to run twice against the same config so the holdout cannot "
            "quietly be re-tried until it looks good.",
        ),
    ] = False,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Evaluate the Phase 4B candidate signals: single-signal, combined, and the sector tilt.

    Everything before the holdout is standard walk-forward cross-validation on dates, with
    a gap at least as long as the horizon (``forecasting.cv``) -- never a plain random
    split, which would let two halves of one overlapping return window land on opposite
    sides of a fold. Pass ``--holdout`` only when you are ready to see the one-time result;
    it is written to ``research_log.md`` and cannot be silently re-run.
    """
    cfg = _setup(config, set_, verbose)
    from .data import load_market_data
    from .forecasting import training_rows
    from .forecasting.combine import (
        cross_validated_pinball_losses,
        select_alpha_by_cv,
        shrinkage_spec,
        simple_average_spec,
        single_or_combined_spec,
    )
    from .forecasting.evaluate import clark_west_statistic, oos_r_squared
    from .forecasting.pipeline import (
        RELATIVE_SIGNAL_COLUMNS,
        build_full_panel,
        load_supply_chain_links_if_present,
    )
    from .forecasting.sector import build_sector_regression_row, fit_sector_tilt, sector_factor
    from .forecasting.stability import (
        fama_macbeth_coefficient_path,
        market_environment_split,
        rolling_coefficient_stability,
    )
    from .reporting.charts import rolling_coefficient_chart, save_figure
    from .runs import RunDirectory
    from .signals.external import ExternalSeriesError, load_external_series

    symbols = [t.strip().upper() for t in tickers.split(",")] if tickers else cfg.universe.tickers
    h = horizon or cfg.signals.horizons[0]

    market = load_market_data(
        cfg, tickers=list(dict.fromkeys(symbols + cfg.benchmarks.tickers)), for_backtest=True
    )
    links = load_supply_chain_links_if_present(cfg.path(cfg.signals.supply_chain_links))
    customer_returns = market.log_returns if links is not None else None

    last_date = market.last_date
    holdout_start = last_date - pd.DateOffset(months=cfg.signals.holdout_months)

    typer.echo(f"Building the signal panel for {len(symbols)} tickers at {h}-day horizon ...")
    panel = build_full_panel(
        market,
        symbols,
        h,
        start=cfg.backtest.start,
        end=last_date,
        supply_chain_links=links,
        customer_returns=customer_returns,
        momentum_lookback_days=cfg.signals.momentum_lookback_days,
        momentum_skip_days=cfg.signals.momentum_skip_days,
        relative_strength_lookback_days=cfg.signals.relative_strength_lookback_days,
        earnings_drift_active_days=cfg.signals.earnings_drift_active_days,
    )
    if panel.empty:
        typer.secho("No origins produced a panel row.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    realized = training_rows(panel, last_date)
    pre_holdout = realized[realized["date"] < holdout_start].copy()
    holdout_rows = realized[realized["date"] >= holdout_start].copy() if holdout else pd.DataFrame()

    available_columns = [c for c in RELATIVE_SIGNAL_COLUMNS if pre_holdout[c].notna().mean() > 0.5]
    if not available_columns:
        typer.secho("No signal has enough coverage to evaluate.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    maxlags = max(1, round(cfg.signals.driscoll_kraay_lag_per_horizon * h / 21))

    def _usable(columns: list[str]) -> pd.DataFrame:
        return pre_holdout.dropna(subset=[*columns, "target"])

    # The addendum's own ladder, simple to complex: one regression per signal, then the
    # kitchen-sink regression, the simple average of the single-signal forecasts (which the
    # addendum says to try *before* the kitchen sink -- "often more robust"), then ridge and
    # lasso. Every candidate is reduced to a CandidateSpec so cross-validation and the
    # eventual one-time holdout fit share exactly the same fit/predict calls.
    candidate_data: dict[str, pd.DataFrame] = {}
    fitted_specs: dict[str, object] = {}
    for column in available_columns:
        spec = single_or_combined_spec(column, [column], maxlags)
        fitted_specs[spec.label] = spec
        candidate_data[spec.label] = _usable([column])
    if len(available_columns) > 1:
        combined_usable = _usable(available_columns)
        combined_spec = single_or_combined_spec("combined", available_columns, maxlags)
        fitted_specs[combined_spec.label] = combined_spec
        candidate_data[combined_spec.label] = combined_usable

        average_spec = simple_average_spec(available_columns, maxlags)
        fitted_specs[average_spec.label] = average_spec
        # Unlike the kitchen sink, the simple average tolerates a missing signal per row --
        # scored on the full pre-holdout frame, not the intersection where every column is
        # present, so it can show the robustness advantage the addendum credits it with.
        candidate_data[average_spec.label] = pre_holdout

        for method in ("ridge", "lasso"):
            alpha, _ = select_alpha_by_cv(
                combined_usable,
                available_columns,
                method,
                cfg.signals.ridge_lasso_alphas,
                cfg.signals.cv_n_folds,
                cfg.signals.cv_gap_periods,
            )
            method_spec = shrinkage_spec(available_columns, alpha, method)
            fitted_specs[method_spec.label] = method_spec
            candidate_data[method_spec.label] = combined_usable

    report_rows = []
    for label, spec in fitted_specs.items():
        data = candidate_data[label]
        outcome = spec.evaluate(data, cfg.signals.cv_n_folds, cfg.signals.cv_gap_periods)
        if outcome is None:
            continue
        report_rows.append(outcome)
    report = pd.DataFrame(report_rows)
    if not report.empty:
        report = report.sort_values("cv_oos_r2", ascending=False)

    # The specification the holdout will (if requested) be spent on -- fixed here, from
    # pre-holdout cross-validation alone, before any holdout row is touched anywhere below.
    best_label = str(report.iloc[0]["signal"]) if not report.empty else None

    typer.echo(
        f"\nPre-holdout, cross-validated (n_folds={cfg.signals.cv_n_folds}), {h}-day horizon:"
    )
    if report.empty:
        typer.echo("(no candidate had enough data)")
    else:
        _echo_table(report.set_index("signal"))

    quantile_usable = _usable(available_columns)
    if len(quantile_usable) >= cfg.signals.min_regression_observations:
        pinball = cross_validated_pinball_losses(
            quantile_usable,
            available_columns,
            tuple(cfg.signals.quantile_levels),
            cfg.signals.cv_n_folds,
            cfg.signals.cv_gap_periods,
        )
        typer.echo("\nQuantile regression (all signals), cross-validated pinball loss by level:")
        for level, loss in pinball.items():
            rendered = f"{loss:.6f}" if np.isfinite(loss) else "n/a"
            typer.echo(f"  q={level:.2f}: {rendered}")
    else:
        pinball = {}
        typer.echo(
            f"\nQuantile regression: only {len(quantile_usable)} usable rows, skipped "
            f"(need >= {cfg.signals.min_regression_observations})."
        )

    # Stability: rolling (per-date) coefficients, sign flips, and a market-environment
    # split -- the addendum's own example is the 2022 downturn vs. the rally after it.
    coefficient_path = fama_macbeth_coefficient_path(quantile_usable, available_columns)
    stability = rolling_coefficient_stability(quantile_usable, available_columns)
    typer.echo("\nStability, rolling (per-date) coefficients:")
    for column, info in stability.items():
        flip = info["sign_flip_rate"]
        rendered = f"{flip:.2f}" if np.isfinite(flip) else "n/a"
        typer.echo(f"  {column}: n_dates={info['n_dates']} sign_flip_rate={rendered}")

    environment_split = market_environment_split(
        quantile_usable,
        available_columns,
        maxlags=maxlags,
        min_observations=cfg.signals.min_regression_observations,
    )
    typer.echo("\nMarket-environment split (2022 downturn vs. the rally after it):")
    for label, info in environment_split.items():
        if info["result"] is None:
            typer.echo(f"  {label}: only {info['n_observations']} rows, not enough to fit")
        else:
            typer.echo(
                f"  {label}: n={info['n_observations']} R^2={info['result']['r_squared']:.4f}"
            )

    # Sector tilt, if the industry series has been fetched.
    industry_path = cfg.path(cfg.signals.external_dir) / f"{cfg.signals.industry_series_id}.csv"
    sector_result = None
    if industry_path.exists():
        try:
            external = load_external_series(industry_path)
            sector_returns = sector_factor(
                market.log_returns[cfg.benchmarks.market], market.log_returns[cfg.benchmarks.sector]
            )
            origins = panel["date"].drop_duplicates().sort_values()
            sector_rows = pd.DataFrame(
                [build_sector_regression_row(sector_returns, external, o, h) for o in origins]
            )
            sector_pre = sector_rows[sector_rows["date"] < holdout_start].dropna(
                subset=["industry_yoy", "target"]
            )
            if len(sector_pre) >= 12:
                sector_result = fit_sector_tilt(sector_pre, maxlags=maxlags)
                coef = sector_result.coefficients["industry_yoy"]
                t_stat = sector_result.t_stats["industry_yoy"]
                typer.echo(
                    f"\nSector tilt (industry cycle -> sector factor), "
                    f"n={sector_result.n_observations}: coef={coef:.4f} t={t_stat:.2f} "
                    f"R^2={sector_result.r_squared:.4f}"
                )
            else:
                typer.echo(f"\nSector tilt: only {len(sector_pre)} usable rows, skipped.")
        except ExternalSeriesError as exc:
            typer.echo(f"\nSector tilt unavailable: {exc}")
    else:
        typer.echo(
            f"\nSector tilt unavailable: no {industry_path.name} in {cfg.signals.external_dir}. "
            "Run `semimc fetch --industry` to seed it."
        )

    run = RunDirectory.create(cfg.runs_path, "signals", cfg.to_dict(), seed=cfg.simulation.seed)
    run.write_table(report, "signal_report", index=False)
    run.record("horizon", h)
    run.record("tickers", symbols)
    run.record("available_columns", available_columns)
    run.record("n_pre_holdout_rows", len(pre_holdout))
    run.record("holdout_start", str(holdout_start.date()))
    run.record("best_pre_holdout_candidate", best_label)
    if pinball:
        run.record("quantile_pinball_loss", {str(k): float(v) for k, v in pinball.items()})
    run.record("stability", stability)
    run.record(
        "market_environment_split",
        {
            label: {"n_observations": info["n_observations"], "result": info["result"]}
            for label, info in environment_split.items()
        },
    )
    if not coefficient_path.empty:
        run.write_table(coefficient_path, "rolling_coefficients")
        save_figure(
            rolling_coefficient_chart(coefficient_path, f"Rolling coefficients, {h}d horizon"),
            run.figure_path("rolling_coefficients"),
            cfg.reporting.figure_dpi,
        )
    if sector_result is not None:
        run.record(
            "sector_tilt",
            {
                "coefficient": float(sector_result.coefficients["industry_yoy"]),
                "t_stat": float(sector_result.t_stats["industry_yoy"]),
                "r_squared": sector_result.r_squared,
                "n_observations": sector_result.n_observations,
            },
        )

    holdout_summary = ""
    if holdout:
        if h != cfg.signals.holdout_headline_horizon:
            typer.secho(
                f"\nWARNING: holdout at {h}d has low power (24mo leaves ~8 non-overlapping "
                f"origins). The headline horizon is {cfg.signals.holdout_headline_horizon}d.",
                fg=typer.colors.YELLOW,
            )
        if best_label is None:
            typer.secho(
                "\nHoldout requested but no candidate had enough pre-holdout data to select "
                "a specification; not evaluated.",
                fg=typer.colors.YELLOW,
            )
        else:
            # Fixed above, from pre-holdout CV alone -- this is the addendum's "all signal
            # selection ... happen before that window" rule, enforced by construction: the
            # holdout section never sees `report` computed on anything but pre_holdout, and
            # `best_label` was chosen before this branch runs.
            winning_spec = fitted_specs[best_label]
            holdout_columns = winning_spec.feature_columns
            usable_holdout = holdout_rows.dropna(
                subset=["target"]
                if best_label == "simple_average"
                else [*holdout_columns, "target"]
            )
            usable_pre = candidate_data[best_label]
            if len(usable_holdout) < 5 or len(usable_pre) < cfg.signals.min_regression_observations:
                typer.secho(
                    f"\nHoldout requested but only {len(usable_holdout)} holdout rows / "
                    f"{len(usable_pre)} pre-holdout rows are usable; not evaluated.",
                    fg=typer.colors.YELLOW,
                )
            else:
                final_model = winning_spec.fit(usable_pre)
                predicted = np.asarray(
                    winning_spec.predict(final_model, usable_holdout), dtype=float
                )
                actual = usable_holdout["target"].to_numpy()
                r2 = oos_r_squared(actual, predicted)
                cw_t, cw_p = clark_west_statistic(actual, np.zeros_like(actual), predicted)
                holdout_summary = (
                    f"\n=== ONE-TIME HOLDOUT RESULT ({h}d, {holdout_start.date()} onward, "
                    f"n={len(usable_holdout)}) ===\n"
                    f"Pre-registered specification (best pre-holdout CV OOS R^2): {best_label}\n"
                    f"OOS R^2: {r2:.4f}   Clark-West t: {cw_t:.2f} (p={cw_p:.3f})\n"
                    "Reported once, regardless of outcome, per the addendum's evaluation rule."
                )
                typer.echo(holdout_summary)
                run.record(
                    "holdout_result",
                    {
                        "horizon": h,
                        "specification": best_label,
                        "n_observations": len(usable_holdout),
                        "oos_r2": r2,
                        "clark_west_t": cw_t,
                        "clark_west_p": cw_p,
                    },
                )

                log_path = cfg.path(cfg.signals.research_log)
                entry = (
                    f"\n## {pd.Timestamp.now().strftime('%Y-%m-%d')} -- holdout evaluation "
                    f"({h}d)\n\n"
                    f"- Tickers: {', '.join(symbols)}\n"
                    f"- Specification (selected by pre-holdout CV, before any holdout row "
                    f"was read): {best_label}\n"
                    f"- Signals: {', '.join(holdout_columns)}\n"
                    f"- Pre-holdout training rows: {len(usable_pre)}\n"
                    f"- Holdout window: {holdout_start.date()} onward "
                    f"({len(usable_holdout)} rows)\n"
                    f"- OOS R^2: {r2:.4f}\n"
                    f"- Clark-West t: {cw_t:.2f} (p={cw_p:.3f})\n"
                    f"- Run: {run.path.name}\n"
                )
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(entry)
                typer.echo(f"Appended to {log_path}")

    text_lines = [
        f"# Signal evaluation: {h}-day horizon",
        "",
        f"Tickers: {', '.join(symbols)}",
        f"Pre-holdout rows: {len(pre_holdout)}; holdout starts {holdout_start.date()}",
        f"Best pre-holdout candidate (by CV OOS R^2): {best_label or 'none'}",
        "",
        "## Pre-holdout, cross-validated",
        "",
        "```\n" + report.set_index("signal").to_string() + "\n```"
        if not report.empty
        else "(no candidate had enough data)",
    ]
    if pinball:
        text_lines += [
            "",
            "## Quantile regression, cross-validated pinball loss",
            "",
            "```\n"
            + "\n".join(
                f"q={level:.2f}: {loss:.6f}" if np.isfinite(loss) else f"q={level:.2f}: n/a"
                for level, loss in pinball.items()
            )
            + "\n```",
        ]
    text_lines += [
        "",
        "## Stability: rolling coefficients and sign flips",
        "",
        "```\n"
        + "\n".join(
            f"{column}: n_dates={info['n_dates']} sign_flip_rate={info['sign_flip_rate']:.2f}"
            if np.isfinite(info["sign_flip_rate"])
            else f"{column}: n_dates={info['n_dates']} sign_flip_rate=n/a"
            for column, info in stability.items()
        )
        + "\n```",
        "",
        "## Market-environment split (2022 downturn vs. the rally after it)",
        "",
        "```\n"
        + "\n".join(
            f"{label}: n={info['n_observations']} R^2={info['result']['r_squared']:.4f}"
            if info["result"] is not None
            else f"{label}: only {info['n_observations']} rows, not enough to fit"
            for label, info in environment_split.items()
        )
        + "\n```",
    ]
    if holdout_summary:
        text_lines.append(holdout_summary)
    run.write_text("\n".join(text_lines), "summary")
    typer.echo(f"\nWritten to {run.close()}")


if __name__ == "__main__":  # pragma: no cover
    app()

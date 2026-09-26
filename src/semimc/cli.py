"""Command line interface.

Commands mirror the build phases: ``fetch`` and ``validate`` (data), ``simulate``
(forecast), ``backtest`` (the scoreboard), ``scenario`` (stress), ``report`` (write-up).

Two rules hold across all of them: every command that produces output writes a
``runs/<timestamp>/`` folder containing its config snapshot and seed, and nothing prints a
point prediction.
"""

from __future__ import annotations

import logging
import re
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

paper_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Live paper-trading jobs: decide, execute, mark, report (semis_paper_trading_brief.md).",
)
app.add_typer(paper_app, name="paper")

intraday_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="S4 intraday agent jobs: preflight, research, preopen, monitor, open-batch, "
    "cycle, carry, eod-review (BRIEF_INTRADAY.md).",
)
app.add_typer(intraday_app, name="intraday")

placebo_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="S4's placebo agent (T6's control group): open-batch, cycle, carry, on its own "
    "shadow ledger. Never submits a real order -- no broker, no LLM call.",
)
intraday_app.add_typer(placebo_app, name="placebo")

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
    implied_vol: Annotated[
        bool,
        typer.Option(
            "--implied-vol",
            help="Override GBM's historical volatility with today's options-implied "
            "volatility for this horizon (brief stretch item). Live-only: fetches the "
            "current option chain over the network, so it cannot be backtested or "
            "validated against history the way every other number here is -- see "
            "data/options.py. Requires --model gbm and a single --ticker.",
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
    from .engine.runner import fit_at_origin, run_simulation
    from .reporting.charts import (
        convergence_chart,
        drawdown_histogram,
        fan_chart,
        save_figure,
        terminal_histogram,
    )
    from .runs import RunDirectory

    if implied_vol and model != "gbm":
        raise typer.BadParameter("--implied-vol only supports --model gbm")
    if implied_vol and portfolio is not None:
        raise typer.BadParameter("--implied-vol only supports --ticker, not --portfolio")

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

    implied_vol_diagnostics: dict | None = None
    implied_vol_override: dict[str, float] = {}
    if implied_vol:
        from .data.options import OptionsUnavailableError, fetch_implied_vol_term_structure

        typer.echo(f"Fetching the live option chain for {symbols[0]} ...")
        try:
            term_structure = fetch_implied_vol_term_structure(
                symbols[0],
                rate=cfg.drift.risk_free_rate,
                dividend_yield=cfg.options.dividend_yield,
                moneyness_band=cfg.options.moneyness_band,
                max_expiries=cfg.options.max_expiries,
                min_quotes=cfg.options.min_quotes_per_expiry,
            )
            annual_vol, vol_diagnostics = term_structure.annual_vol_for_horizon(
                horizon, cfg.simulation.trading_days_per_year
            )
            implied_spot = term_structure.spot
        except OptionsUnavailableError as exc:
            typer.secho(
                f"  unavailable ({exc}); using historical volatility instead.",
                fg=typer.colors.YELLOW,
            )
            annual_vol, vol_diagnostics, implied_spot = float("nan"), {"error": str(exc)}, None

        if np.isfinite(annual_vol):
            implied_vol_override = {symbols[0]: annual_vol}
            typer.echo(
                f"  implied annual vol for {horizon}d: {annual_vol:.1%} "
                f"(spot {implied_spot:.2f}, {vol_diagnostics.get('method', '?')})"
            )
        else:
            typer.secho(
                "  no usable implied vol from the chain; using historical volatility instead.",
                fg=typer.colors.YELLOW,
            )
        implied_vol_diagnostics = {
            "applied": bool(implied_vol_override),
            "annual_vol": annual_vol if np.isfinite(annual_vol) else None,
            "spot": implied_spot,
            **vol_diagnostics,
        }

    typer.echo(
        f"Fitting {model} on {len(training):,} days to {as_of.date()} "
        f"({training.index.min().date()} onward), drift policy {cfg.drift.policy}."
    )

    def _apply_implied_vol(unfitted) -> None:  # noqa: ANN001
        if implied_vol_override:
            unfitted.implied_annual_vol = implied_vol_override

    fitted, training = fit_at_origin(
        market, cfg, model, symbols, as_of, context, pre_fit=_apply_implied_vol
    )

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
    if implied_vol_diagnostics is not None:
        run.record("implied_vol", implied_vol_diagnostics)

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
    # Same seed the run itself used, so the block-bootstrap correction is reproducible
    # from the recorded seed like everything else in the run.
    dependence_rng = np.random.default_rng(result.seed)
    pit = pit_tables(
        result.rows,
        bins=cfg.backtest.pit_bins,
        n_bootstrap=cfg.backtest.n_dependence_bootstraps,
        rng=dependence_rng,
    )
    dm = dm_table(
        result.rows,
        result.baseline,
        n_bootstrap=cfg.backtest.n_dependence_bootstraps,
        rng=dependence_rng,
    )

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
        bool,
        typer.Option(
            "--equal-weight/--no-equal-weight", help="Also report an equal-weighted portfolio."
        ),
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


@app.command()
def dashboard(
    port: Annotated[int, typer.Option("--port", help="Local port to serve on.")] = 8765,
    open_: Annotated[
        bool, typer.Option("--open/--no-open", help="Open the dashboard in a browser tab.")
    ] = True,
) -> None:
    """Launch the HTML dashboard: a local web server that drives this same CLI.

    Every action it offers runs the same ``semimc <command> ...`` a terminal user would
    type, as a subprocess (``dashboard/jobs.py``) -- see ``dashboard/server.py``'s module
    docstring. Stdlib ``http.server`` only, so nothing extra needs installing.
    """
    from .dashboard.server import run_server

    run_server(port=port, open_browser=open_)


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
            "shows -- refuses to run if research_log.md already records a holdout window "
            "at or after this one, so the holdout cannot quietly be re-tried until it "
            "looks good.",
        ),
    ] = False,
    confirm_new_holdout_window: Annotated[
        bool,
        typer.Option(
            "--confirm-new-holdout-window",
            help="Required in addition to --holdout when research_log.md already records "
            "an earlier holdout window, even if this run's window is genuinely later "
            "(more calendar time has passed). Pre-register the new window's specification "
            "in research_log.md before passing this.",
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
    it is written to ``research_log.md``, and a second ``--holdout`` run whose window
    overlaps one already recorded there is refused (see ``--confirm-new-holdout-window``).
    """
    cfg = _setup(config, set_, verbose)
    from .analytics.power import required_sample_size
    from .data import load_market_data
    from .forecasting import approx_trading_days_per_period, min_gap_periods, training_rows
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

    if holdout:
        log_path = cfg.path(cfg.signals.research_log)
        prior_holdout_starts = []
        if log_path.exists():
            prior_holdout_starts = [
                pd.Timestamp(match)
                for match in re.findall(
                    r"-\s*Holdout window:\s*(\d{4}-\d{2}-\d{2})\s+onward",
                    log_path.read_text(encoding="utf-8"),
                )
            ]
        if prior_holdout_starts:
            latest_prior = max(prior_holdout_starts)
            if latest_prior >= holdout_start:
                typer.secho(
                    f"\n{log_path} already records a holdout window starting "
                    f"{latest_prior.date()}, at or after this run's {holdout_start.date()}. "
                    "Refusing to re-spend it -- there is no legitimate reason to re-run an "
                    "identical-or-earlier holdout window.",
                    fg=typer.colors.RED,
                )
                raise typer.Exit(code=1)
            if not confirm_new_holdout_window:
                typer.secho(
                    f"\n{log_path} already records a holdout window starting "
                    f"{latest_prior.date()}. This run's window ({holdout_start.date()}) is "
                    "genuinely later, but still overlaps most of the same calendar range. "
                    "Pre-register the new window's specification in research_log.md, then "
                    "pass --confirm-new-holdout-window to proceed.",
                    fg=typer.colors.RED,
                )
                raise typer.Exit(code=1)

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
        origin_freq=cfg.signals.origin_frequency,
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

    period_trading_days = approx_trading_days_per_period(cfg.signals.origin_frequency)
    maxlags = max(1, round(cfg.signals.driscoll_kraay_lag_per_horizon * h / period_trading_days))
    required_gap = min_gap_periods(h, cfg.signals.origin_frequency)
    if cfg.signals.cv_gap_periods < required_gap:
        typer.secho(
            f"\nWARNING: signals.cv_gap_periods={cfg.signals.cv_gap_periods} is smaller "
            f"than the {required_gap} needed to keep a {h}-day forward window from "
            f"spanning a fold boundary at origin_frequency={cfg.signals.origin_frequency!r} "
            f"(~{period_trading_days:.1f} trading days/period). CV folds may leak.",
            fg=typer.colors.YELLOW,
        )

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
        # Power diagnostic, not a new scoring metric: how much more data, at this same
        # cadence and assuming the same effect size, would be needed to clear the
        # addendum's own "~3" skepticism bar (see MODEL_NOTES/plan_addendum.md). A rough
        # planning heuristic (analytics.power.required_sample_size), not a promise.
        target_t = 3.0
        dates_per_year = 252.0 / period_trading_days
        typer.echo(f"\nPower: data needed to reach a rank-IC t-stat of {target_t:.1f}:")
        for _, row in report.iterrows():
            observed_t, observed_n = row["rank_ic_t"], row["rank_ic_n_dates"]
            if not np.isfinite(observed_t) or not np.isfinite(observed_n) or observed_n <= 0:
                typer.echo(f"  {row['signal']}: n/a")
                continue
            needed_n = required_sample_size(observed_t, int(observed_n), target_t)
            if not np.isfinite(needed_n):
                typer.echo(f"  {row['signal']}: n/a (observed effect is non-positive)")
                continue
            extra_years = max(needed_n - observed_n, 0.0) / dates_per_year
            typer.echo(
                f"  {row['signal']}: t={observed_t:.2f} on n={int(observed_n)} -> "
                f"~{extra_years:.1f} more years at this cadence"
            )

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


# ---------------------------------------------------------------------------------------
# paper decide / execute / mark / report
# ---------------------------------------------------------------------------------------


def _paper_as_of(value: str | None) -> pd.Timestamp:
    return pd.Timestamp(value) if value else pd.Timestamp.now().normalize()


def _paper_now(cfg: Config):  # noqa: ANN001 -- returns dt.datetime
    import datetime as dt
    from zoneinfo import ZoneInfo

    return dt.datetime.now(ZoneInfo(cfg.paper.execution.timezone)).replace(tzinfo=None)


def _validate_sleeve(sleeve: str) -> None:
    from .strategy.decide import SLEEVES

    if sleeve not in SLEEVES:
        raise typer.BadParameter(f"sleeve must be one of {SLEEVES}, got {sleeve!r}")


SleeveOption = Annotated[
    str, typer.Option("--sleeve", help="s0, s3, vol_twin_shadow, or sector_shadow.")
]
PaperAsOfOption = Annotated[
    str | None, typer.Option("--as-of", help="Trading day, YYYY-MM-DD. Defaults to today.")
]


@paper_app.command("decide")
def paper_decide(
    sleeve: SleeveOption,
    as_of: PaperAsOfOption = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Record even if this sleeve already decided this rebalance period, or a "
            "different decision already exists for this exact day.",
        ),
    ] = False,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Freeze one sleeve's target weights for today (or --as-of) into the ledger.

    A no-op, not an error, if this sleeve already decided this rebalance period -- a
    schedule can safely call this daily without landing on exactly the right day.
    """
    cfg = _setup(config, set_, verbose)
    _validate_sleeve(sleeve)
    from .data import load_market_data
    from .live.decide import run_decide
    from .live.ledger import Ledger

    as_of_ts = _paper_as_of(as_of)
    symbols = cfg.universe.tickers
    market = load_market_data(cfg, tickers=list(dict.fromkeys(symbols + cfg.benchmarks.tickers)))
    ledger = Ledger(cfg.path(cfg.paper.ledger_path))
    try:
        try:
            result = run_decide(sleeve, market, cfg, symbols, as_of_ts, ledger, force=force)
        except ValueError as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc
    finally:
        ledger.close()

    if result.skipped_reason is not None:
        typer.echo(f"Skipped: {result.skipped_reason}")
        return
    decision = result.decision
    typer.echo(
        f"Decided {sleeve!r} for {as_of_ts.date()}: snapshot_hash={decision.snapshot_hash[:16]}"
    )
    _echo_table(decision.weights.rename("weight"))


@paper_app.command("execute")
def paper_execute(
    sleeve: SleeveOption,
    as_of: PaperAsOfOption = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Build and print intended orders without submitting or writing to the ledger.",
        ),
    ] = False,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Execute the sleeve's most recently frozen decision against the real paper account.

    Refuses (a clean skip, not an error) if this sleeve has never been decided, and again
    if any pretrade gate declines (kill switch, trading disabled, unregistered account,
    closed market, outside the execution window) -- these are the gates working as
    designed, not failures.
    """
    cfg = _setup(config, set_, verbose)
    _validate_sleeve(sleeve)
    from .data import load_market_data
    from .execution.alpaca_broker import AlpacaPaperBroker
    from .live.execute import run_execute
    from .live.ledger import Ledger

    as_of_ts = _paper_as_of(as_of)
    symbols = cfg.universe.tickers
    market = load_market_data(cfg, tickers=list(dict.fromkeys(symbols + cfg.benchmarks.tickers)))
    now = _paper_now(cfg)
    ledger = Ledger(cfg.path(cfg.paper.ledger_path))
    broker = AlpacaPaperBroker.from_env()
    try:
        result = run_execute(
            sleeve, cfg, broker, ledger, market, symbols, as_of_ts, now, dry_run=dry_run
        )
    finally:
        ledger.close()

    if result.skipped_reason is not None:
        typer.echo(f"Skipped: {result.skipped_reason}")
        return
    if result.pretrade_failures:
        typer.secho("Declined at pretrade:", fg=typer.colors.YELLOW)
        for failure in result.pretrade_failures:
            typer.echo(f"  - {failure}")
        return
    if not result.orders:
        typer.echo("Nothing to trade -- already at target.")
        return

    verb = "Would submit" if dry_run else "Submitted"
    typer.echo(f"{verb} {len(result.orders)} order(s):")
    for order in result.orders:
        typer.echo(f"  {order.side} {order.qty:.4f} {order.symbol} @ {order.limit_price:.2f}")
    if result.order_failures:
        typer.secho(
            f"{len(result.order_failures)} order(s) failed the tradability check:",
            fg=typer.colors.RED,
        )
        for request, reason in result.order_failures:
            typer.echo(f"  {request.symbol}: {reason}")
        raise typer.Exit(code=1)


@paper_app.command("mark")
def paper_mark(
    sleeve: SleeveOption,
    as_of: PaperAsOfOption = None,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Poll fill status, prune stale unfilled orders past the reprice interval, and
    reconcile the ledger's own view of positions against the broker's actual ones."""
    cfg = _setup(config, set_, verbose)
    _validate_sleeve(sleeve)
    from .execution.alpaca_broker import AlpacaPaperBroker
    from .live.ledger import Ledger
    from .live.mark import run_mark

    as_of_ts = _paper_as_of(as_of)
    now = _paper_now(cfg)
    ledger = Ledger(cfg.path(cfg.paper.ledger_path))
    broker = AlpacaPaperBroker.from_env()
    try:
        result = run_mark(sleeve, cfg, broker, ledger, as_of_ts, now)
    finally:
        ledger.close()

    typer.echo(f"Fills recorded: {result.fills_recorded}")
    if result.orders_canceled_for_reprice:
        typer.echo(f"Canceled for reprice: {', '.join(result.orders_canceled_for_reprice)}")

    failed = False
    if result.orders_not_found:
        typer.secho(
            f"{len(result.orders_not_found)} open order(s) not found at the broker:",
            fg=typer.colors.RED,
        )
        for client_order_id in result.orders_not_found:
            typer.echo(f"  {client_order_id}")
        failed = True

    if result.reconciliation_status == "ok":
        typer.echo("Reconciliation: ok")
    else:
        typer.secho(f"Reconciliation: {result.reconciliation_status}", fg=typer.colors.RED)
        for d in result.discrepancies:
            typer.echo(f"  {d['symbol']}: expected {d['expected']:.4f}, actual {d['actual']:.4f}")
        failed = True

    if failed:
        raise typer.Exit(code=1)


@paper_app.command("report")
def paper_report(
    sleeve: SleeveOption,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Summarize a sleeve's ledger: equity, positions, open incidents, reconciliation
    status. Never touches the network or the broker -- a plain read of the ledger."""
    cfg = _setup(config, set_, verbose)
    _validate_sleeve(sleeve)
    from .live.ledger import Ledger
    from .live.report import build_report, render_markdown

    ledger = Ledger(cfg.path(cfg.paper.ledger_path))
    try:
        report = build_report(sleeve, ledger)
    finally:
        ledger.close()

    markdown = render_markdown(report)
    typer.echo(markdown)

    out_dir = cfg.runs_path / sleeve
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"report_{pd.Timestamp.now():%Y%m%d}.md"
    out_path.write_text(markdown, encoding="utf-8")
    typer.echo(f"Written to {out_path}")


def _echo_comparison(result) -> None:  # noqa: ANN001 -- ComparisonResult
    frame = pd.DataFrame([vars(result.control), vars(result.candidate)]).set_index("label")
    _echo_table(frame.drop(columns=["start", "end", "n_days"]))
    typer.echo(f"n_days: {dict(frame['n_days'])}")
    typer.echo(f"overlapping days: {result.n_overlapping_days}")
    rd, vd = result.annualized_return_difference, result.annualized_vol_difference
    typer.echo(
        f"annualized return difference (candidate - control): {rd.point_estimate:.4f} "
        f"[{rd.ci_low:.4f}, {rd.ci_high:.4f}] (95% CI, n_bootstrap={rd.n_bootstrap})"
    )
    typer.echo(
        f"annualized vol difference (candidate - control): {vd.point_estimate:.4f} "
        f"[{vd.ci_low:.4f}, {vd.ci_high:.4f}] (95% CI, n_bootstrap={vd.n_bootstrap})"
    )
    min_months = 3
    if result.overlap_calendar_days < min_months * 30:
        typer.secho(
            f"\nCAVEAT: only {result.overlap_calendar_days} overlapping calendar days -- "
            f"below the {min_months}-month minimum THESES.md sets before a verdict here "
            "means anything beyond descriptive. Numbers above are real, not a conclusion.",
            fg=typer.colors.YELLOW,
        )


@paper_app.command("evaluate")
def paper_evaluate(
    thesis: Annotated[str, typer.Option("--thesis", help="t2, t3, or t5.")],
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """T2/T3/T5 (`THESES.md`): compare sleeves' realized paper-trading performance, or
    report execution cost. Always prints whatever the ledger currently supports -- never
    refuses for lack of history, only caveats a comparison shorter than THESES.md's
    stated minimum."""
    cfg = _setup(config, set_, verbose)
    if thesis not in ("t2", "t3", "t5"):
        raise typer.BadParameter(f"thesis must be one of t2, t3, t5, got {thesis!r}")
    from .data import load_market_data
    from .live.ledger import Ledger

    ledger = Ledger(cfg.path(cfg.paper.ledger_path))
    try:
        if thesis in ("t2", "t3"):
            from .analytics.paper_evaluation import evaluate_t2, evaluate_t3

            symbols = cfg.universe.tickers
            market = load_market_data(
                cfg, tickers=list(dict.fromkeys(symbols + cfg.benchmarks.tickers))
            )
            result = (
                evaluate_t2(ledger, market, cfg)
                if thesis == "t2"
                else evaluate_t3(ledger, market, cfg, symbols)
            )
            _echo_comparison(result)
        else:
            from .analytics.execution_cost import execution_cost_report
            from .execution.market_data import AlpacaMarketDataClient

            data_client = AlpacaMarketDataClient.from_env()
            frames = []
            for sleeve in ("s0", "s3"):
                report = execution_cost_report(ledger, sleeve, data_client)
                if not report.empty:
                    report.insert(0, "sleeve", sleeve)
                    frames.append(report)
            if not frames:
                typer.echo("No fills recorded yet for s0/s3 -- nothing to report.")
                return
            combined = pd.concat(frames, ignore_index=True)
            _echo_table(combined.set_index(["sleeve", "client_order_id"]))
            typer.echo(f"\nmean slippage (bps): {combined['slippage_bps'].mean():.2f}")
            if len(combined) < 20:
                typer.secho(
                    f"\nCAVEAT: only {len(combined)} fills -- THESES.md treats fewer "
                    "than ~20 as anecdotal, not a pattern.",
                    fg=typer.colors.YELLOW,
                )
    finally:
        ledger.close()


# -- S4 intraday agent jobs (BRIEF_INTRADAY.md, Phases 5-6) ------------------------------


def _intraday_as_of(value: str | None) -> pd.Timestamp:
    return pd.Timestamp(value) if value else pd.Timestamp.now().normalize()


def _intraday_now(cfg: Config):  # noqa: ANN001 -- returns dt.datetime
    import datetime as dt
    from zoneinfo import ZoneInfo

    return dt.datetime.now(ZoneInfo(cfg.intraday.schedule.timezone)).replace(tzinfo=None)


def _intraday_broker():  # noqa: ANN001 -- returns AlpacaPaperBroker
    from .execution.alpaca_broker import AlpacaPaperBroker

    return AlpacaPaperBroker.from_env(
        key_env="ALPACA_S4_API_KEY", secret_env="ALPACA_S4_SECRET_KEY"
    )


def _intraday_market_data():  # noqa: ANN001 -- returns AlpacaMarketDataClient
    from .execution.market_data import AlpacaMarketDataClient

    return AlpacaMarketDataClient.from_env(
        key_env="ALPACA_S4_API_KEY", secret_env="ALPACA_S4_SECRET_KEY"
    )


def _intraday_options_market_data():  # noqa: ANN001 -- returns AlpacaOptionsMarketDataClient
    from .execution.market_data import AlpacaOptionsMarketDataClient

    return AlpacaOptionsMarketDataClient.from_env(
        key_env="ALPACA_S4_API_KEY", secret_env="ALPACA_S4_SECRET_KEY"
    )


def _intraday_news_client():  # noqa: ANN001 -- returns AlpacaNewsClient
    from .execution.news import AlpacaNewsClient

    return AlpacaNewsClient.from_env()


def _intraday_execution_broker(cfg, ledger, market_data, now):  # noqa: ANN001 -- Broker
    """The broker `open-batch`/`cycle`/`carry`/`monitor` actually submit orders to --
    `_intraday_broker()` (real Alpaca) unless `cfg.intraday.shadow_mode` (Phase 11,
    Rollout Stage 1), in which case a `FakeBroker` reconstructed from `ledger`'s own
    persisted state (mirroring `_load_shadow_broker`'s exact shape, but against the real
    ledger rather than the placebo's separate one) and marked to market fresh. The caller
    is responsible for saving state back afterward with `ledger.save_shadow_broker_state
    (broker.to_state())` when `cfg.intraday.shadow_mode` -- mirroring every placebo
    command's own explicit load/run/save shape rather than hiding it in a context
    manager.

    `research`/`preflight` deliberately never call this -- both stay on the real broker
    always (see `intraday/jobs.py`'s Phase 11 docstring additions for why)."""
    if not cfg.intraday.shadow_mode:
        return _intraday_broker()
    from .execution.fake_broker import FakeBroker
    from .intraday.jobs import mark_broker_to_market

    broker = FakeBroker.from_state(ledger.load_shadow_broker_state(), now=now)
    mark_broker_to_market(broker, ledger, market_data, cfg.intraday.shadow_starting_equity, now)
    return broker


IntradayAsOfOption = Annotated[
    str | None, typer.Option("--as-of", help="Trading day, YYYY-MM-DD. Defaults to today.")
]


@intraday_app.command("preflight")
def intraday_preflight(
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Health checks before today's trading: the pretrade gate, yesterday's
    reconciliation status, a market-data smoke test, and a news-feed smoke test.

    A canary, not a gate other commands depend on -- `open-batch`/`cycle`/`carry` each
    re-run their own gates fresh regardless of this command's result, so a missed or
    failed preflight never blocks trading by itself; it exists to surface a dead account
    or feed via GitHub's own failure email before the day's real jobs waste a cycle on it.
    Exits 0 for a routine, by-design pretrade decline (not yet `trading_enabled`, market
    not open yet) -- those are printed as "(routine)" notes, not failures, so this command
    only fails the job (and fires that email) on something actually broken (kill switch,
    account mismatch, a dead feed, a bad reconciliation). See `run_preflight`'s docstring.
    """
    cfg = _setup(config, set_, verbose)
    from .intraday.jobs import run_preflight
    from .intraday.ledger import IntradayLedger

    now = _intraday_now(cfg)
    ledger = IntradayLedger(cfg.path(cfg.intraday.ledger_path))
    broker = _intraday_broker()
    market_data = _intraday_market_data()
    news_client = _intraday_news_client()
    try:
        result = run_preflight(cfg, broker, market_data, news_client, ledger, now)
    finally:
        ledger.close()

    version_line = (
        f"Playbook version: {result.playbook_version}"
        if result.playbook_version is not None
        else "Playbook version: none adopted yet"
    )
    mode_line = f"Mode: {'shadow' if result.shadow_mode else 'live'}"
    if result.passed:
        typer.echo("Preflight: passed")
        typer.echo(f"  {mode_line}")
        typer.echo(f"  {version_line}")
        for note in result.notes:
            typer.echo(f"  (routine) {note}")
        return
    typer.secho("Preflight: failed", fg=typer.colors.RED)
    typer.echo(f"  {mode_line}")
    typer.echo(f"  {version_line}")
    for failure in result.failures:
        typer.echo(f"  - {failure}")
    for note in result.notes:
        typer.echo(f"  (routine) {note}")
    raise typer.Exit(code=1)


@intraday_app.command("research")
def intraday_research(
    cycle: Annotated[
        str, typer.Option("--cycle", help="Label recorded for this research run.")
    ] = "research",
    as_of: IntradayAsOfOption = None,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """The Researcher: reads overnight news, pre-market moves, the earnings calendar, and
    a realized-vol snapshot, and writes a morning brief plus a watchlist. Idempotent per
    trading day -- a repeat call after the first success this same `as_of` is a no-op
    that spends nothing (see `run_research`'s docstring)."""
    cfg = _setup(config, set_, verbose)
    from .data import load_market_data
    from .intraday.jobs import run_research
    from .intraday.ledger import IntradayLedger
    from .intraday.llm import ClaudeCliClient

    as_of_ts = _intraday_as_of(as_of)
    now = _intraday_now(cfg)
    ledger = IntradayLedger(cfg.path(cfg.intraday.ledger_path))
    broker = _intraday_broker()
    market_data = _intraday_market_data()
    news_client = _intraday_news_client()
    market_data_pit = load_market_data(cfg, tickers=cfg.universe.tickers)
    llm_client = ClaudeCliClient.from_env()
    try:
        result = run_research(
            cfg,
            broker,
            market_data,
            news_client,
            market_data_pit,
            ledger,
            llm_client,
            as_of_ts,
            cycle,
            now,
        )
    finally:
        ledger.close()

    if result.skipped_reason is not None:
        typer.echo(f"Skipped: {result.skipped_reason}")
        return
    typer.echo(f"Brief recorded. Watchlist size: {result.watchlist_size}")


@intraday_app.command("preopen")
def intraday_preopen(
    as_of: IntradayAsOfOption = None,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Deterministic refresh of today's watchlist quotes and headlines: drops anything
    whose spread or quote staleness fails the configured gate, and logs any genuinely new
    headline per symbol (never used here to drop a candidate -- see `run_preopen`'s
    docstring). Read-only/logging only -- `open-batch`/`cycle` re-run the identical spread
    gate themselves rather than trusting this command's result."""
    cfg = _setup(config, set_, verbose)
    from .intraday.jobs import run_preopen
    from .intraday.ledger import IntradayLedger

    as_of_ts = _intraday_as_of(as_of)
    now = _intraday_now(cfg)
    ledger = IntradayLedger(cfg.path(cfg.intraday.ledger_path))
    market_data = _intraday_market_data()
    news_client = _intraday_news_client()
    try:
        result = run_preopen(cfg, market_data, news_client, ledger, as_of_ts, now)
    finally:
        ledger.close()

    typer.echo(f"Tradable: {', '.join(result.tradable) if result.tradable else '(none)'}")
    if result.dropped:
        typer.secho("Dropped:", fg=typer.colors.YELLOW)
        for _symbol, reason in result.dropped:
            typer.echo(f"  {reason}")
    if result.new_headlines:
        typer.echo("New headlines:")
        for symbol, headline in result.new_headlines:
            typer.echo(f"  {symbol}: {headline}")


@intraday_app.command("monitor")
def intraday_monitor(
    as_of: IntradayAsOfOption = None,
    cycle: Annotated[
        str, typer.Option("--cycle", help="Label recorded on any close this run submits.")
    ] = "monitor",
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Deterministic, model-free invalidation/time-stop enforcement -- meant for a tight
    cron (every few minutes) since it makes no LLM call. `open-batch`/`cycle` also run
    this first, so a position is never re-evaluated by the Trader after it should
    already be closed."""
    cfg = _setup(config, set_, verbose)
    from .intraday.jobs import midpoint_price, quote_map
    from .intraday.ledger import IntradayLedger
    from .intraday.monitor import check_invalidation_and_time_stops

    as_of_ts = _intraday_as_of(as_of)
    now = _intraday_now(cfg)
    ledger = IntradayLedger(cfg.path(cfg.intraday.ledger_path))
    market_data = _intraday_market_data()
    broker = _intraday_execution_broker(cfg, ledger, market_data, now)
    try:
        positions = broker.list_positions()
        quotes = quote_map({p.symbol for p in positions}, market_data, now)
        prices = {s: midpoint_price(q) for s, q in quotes.items()}
        result = check_invalidation_and_time_stops(
            cfg.intraday, broker, ledger, prices, as_of_ts, cycle, now
        )
        if cfg.intraday.shadow_mode:
            ledger.save_shadow_broker_state(broker.to_state())
    finally:
        ledger.close()

    typer.echo(f"Closed: {', '.join(result.closed) if result.closed else '(none)'}")
    typer.echo(f"Still open: {', '.join(result.still_open) if result.still_open else '(none)'}")
    if result.gap_seconds is not None:
        typer.echo(f"Gap since last check: {result.gap_seconds / 60.0:.1f}m")


def _run_intraday_cycle(
    cycle: str,
    as_of: str | None,
    config: Path | None,
    set_: list[str] | None,
    verbose: bool,
) -> None:
    cfg = _setup(config, set_, verbose)
    from .intraday.jobs import run_cycle
    from .intraday.ledger import IntradayLedger
    from .intraday.llm import ClaudeCliClient

    as_of_ts = _intraday_as_of(as_of)
    now = _intraday_now(cfg)
    ledger = IntradayLedger(cfg.path(cfg.intraday.ledger_path))
    market_data = _intraday_market_data()
    broker = _intraday_execution_broker(cfg, ledger, market_data, now)
    llm_client = ClaudeCliClient.from_env()
    # Rollout Stage 4 (Phase 12b): both stay None -- no extra client construction, no
    # extra local data load -- unless options shadow pricing (or real trading) is actually
    # on, so Stage 1-3's real, currently-running cycles pay nothing for this. See
    # `run_cycle`'s own docstring for what each defaults to when neither is supplied.
    market_data_pit = None
    options_market_data = None
    if cfg.intraday.options.shadow_pricing or cfg.intraday.options.enabled:
        from .data import load_market_data

        market_data_pit = load_market_data(cfg, tickers=cfg.universe.tickers)
        options_market_data = _intraday_options_market_data()
    try:
        result = run_cycle(
            cycle,
            cfg,
            broker,
            ledger,
            market_data,
            llm_client,
            as_of_ts,
            now,
            market_data_pit=market_data_pit,
            options_market_data=options_market_data,
        )
        if cfg.intraday.shadow_mode:
            ledger.save_shadow_broker_state(broker.to_state())
    finally:
        ledger.close()

    if result.skipped_reason is not None:
        typer.echo(f"Skipped: {result.skipped_reason}")
        return

    typer.echo(
        f"Proposals: {result.proposals_seen} seen, {result.proposals_accepted} accepted, "
        f"{result.proposals_rejected} rejected"
    )
    if result.orders_submitted:
        typer.echo(f"Orders submitted: {', '.join(result.orders_submitted)}")
    if result.order_failures:
        typer.secho(f"{len(result.order_failures)} order failure(s):", fg=typer.colors.RED)
        for failure in result.order_failures:
            typer.echo(f"  {failure}")
        raise typer.Exit(code=1)


@intraday_app.command("open-batch")
def intraday_open_batch(
    as_of: IntradayAsOfOption = None,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """The first Trader cycle of the day -- identical to `cycle`, just labeled
    "open_batch" for the ledger and idempotency (`intraday_client_order_id`)."""
    _run_intraday_cycle("open_batch", as_of, config, set_, verbose)


@intraday_app.command("cycle")
def intraday_cycle(
    as_of: IntradayAsOfOption = None,
    cycle: Annotated[
        str, typer.Option("--cycle", help="Label for this cycle, e.g. cycle_0835.")
    ] = "cycle",
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """One Trader cycle: monitor -> tranche continuation -> propose -> validate ->
    execute. Meant to run every `schedule.cycle_interval_minutes` through the session."""
    _run_intraday_cycle(cycle, as_of, config, set_, verbose)


@intraday_app.command("carry")
def intraday_carry(
    as_of: IntradayAsOfOption = None,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """End-of-session: closes anything without a written multi-day thesis, then closes
    the lowest-confidence carried positions until back under the overnight gross cap."""
    cfg = _setup(config, set_, verbose)
    from .intraday.jobs import run_carry
    from .intraday.ledger import IntradayLedger

    as_of_ts = _intraday_as_of(as_of)
    now = _intraday_now(cfg)
    ledger = IntradayLedger(cfg.path(cfg.intraday.ledger_path))
    market_data = _intraday_market_data()
    broker = _intraday_execution_broker(cfg, ledger, market_data, now)
    try:
        result = run_carry(cfg, broker, ledger, market_data, as_of_ts, now)
        if cfg.intraday.shadow_mode:
            ledger.save_shadow_broker_state(broker.to_state())
    finally:
        ledger.close()

    closed_str = ", ".join(result.closed) if result.closed else "(none)"
    carried_str = ", ".join(result.carried) if result.carried else "(none)"
    typer.echo(f"Closed (no multi-day thesis): {closed_str}")
    typer.echo(f"Carried: {carried_str}")
    if result.closed_for_cap:
        typer.secho(
            f"Closed for overnight cap: {', '.join(result.closed_for_cap)}", fg=typer.colors.YELLOW
        )


@intraday_app.command("eod-review")
def intraday_eod_review(
    as_of: IntradayAsOfOption = None,
    cycle: Annotated[
        str, typer.Option("--cycle", help="Label recorded for this review's model call.")
    ] = "eod_review",
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """The Reviewer: grades the day and writes `reports/YYYY-MM-DD/eod.md` -- the day's
    blotter, P&L, attribution, benchmarks (SOXX, S0, the placebo, "do nothing"), closing
    market report, process scorecard, and any playbook proposal (`run_eod_review`,
    Phase 9b). Idempotent per trading day, and skips cleanly with nothing to review yet
    (see `run_eod_review`'s docstring) -- same shape as `research`'s own idempotency.
    """
    cfg = _setup(config, set_, verbose)
    from .data import load_market_data
    from .intraday.jobs import run_eod_review
    from .intraday.ledger import IntradayLedger
    from .intraday.llm import ClaudeCliClient
    from .live.ledger import Ledger as PaperLedger

    as_of_ts = _intraday_as_of(as_of)
    now = _intraday_now(cfg)
    ledger = IntradayLedger(cfg.path(cfg.intraday.ledger_path))
    market_data = _intraday_market_data()
    news_client = _intraday_news_client()
    market_data_pit = load_market_data(cfg, tickers=["SOXX"])
    paper_ledger = PaperLedger(cfg.path(cfg.paper.ledger_path))
    shadow_ledger = IntradayLedger(cfg.path(cfg.intraday.shadow_ledger_path))
    llm_client = ClaudeCliClient.from_env()
    try:
        result = run_eod_review(
            cfg,
            market_data,
            news_client,
            market_data_pit,
            paper_ledger,
            shadow_ledger,
            ledger,
            llm_client,
            as_of_ts,
            cycle,
            now,
        )
    finally:
        paper_ledger.close()
        shadow_ledger.close()
        ledger.close()

    if result.skipped_reason is not None:
        typer.echo(f"Skipped: {result.skipped_reason}")
        return
    typer.echo(f"Wrote {result.report_path}")
    typer.echo(f"Reflections graded: {result.reflections_updated}")
    if result.playbook_proposal_written:
        typer.echo("Playbook proposal written to proposals/")


@intraday_app.command("weekly-review")
def intraday_weekly_review(
    as_of: IntradayAsOfOption = None,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Friday 15:45: writes `reports/YYYY-MM-DD/weekly_review.md` -- the week's numbers,
    any pending playbook proposals, and any active entries due for re-review
    (`run_weekly_review`, Phase 10). No model call; never accepts, rejects, or retires
    anything itself -- see `intraday playbook`'s own commands for that.
    """
    cfg = _setup(config, set_, verbose)
    from .intraday.jobs import run_weekly_review
    from .intraday.ledger import IntradayLedger

    as_of_ts = _intraday_as_of(as_of)
    ledger = IntradayLedger(cfg.path(cfg.intraday.ledger_path))
    try:
        result = run_weekly_review(cfg, ledger, as_of_ts)
    finally:
        ledger.close()

    typer.echo(f"Wrote {result.report_path}")
    typer.echo(f"Pending playbook proposals: {result.pending_proposals}")
    typer.echo(f"Entries due for re-review: {result.entries_due_for_review}")


# -- S4's playbook: human accept/reject/retire actions (BRIEF_INTRADAY.md's
# "Self-improvement rules", Phase 10). Deliberately never called from a cron job -- the
# brief's own "Ask first: Accepting a playbook proposal" and "accepted or rejected by
# me... not automatically" both rule that out. See intraday/playbook.py's own docstring.

playbook_app = typer.Typer(help="Accept, reject, or retire a playbook proposal.")
intraday_app.add_typer(playbook_app, name="playbook")


@playbook_app.command("pending")
def playbook_pending(
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """List playbook proposals not yet accepted or rejected."""
    cfg = _setup(config, set_, verbose)
    from .intraday.ledger import IntradayLedger
    from .intraday.playbook import pending_proposal_paths

    ledger = IntradayLedger(cfg.path(cfg.intraday.ledger_path))
    try:
        paths = pending_proposal_paths(cfg, ledger)
    finally:
        ledger.close()

    if not paths:
        typer.echo("No pending proposals.")
        return
    for path in paths:
        typer.echo(str(path))


@playbook_app.command("accept")
def playbook_accept(
    date: Annotated[
        str, typer.Option("--date", help="Proposal date, YYYY-MM-DD (proposals/<date>.md).")
    ],
    evidence_trading_days: Annotated[
        int | None,
        typer.Option("--evidence-trading-days", help="Trading days of evidence behind it."),
    ] = None,
    evidence_trades: Annotated[
        int | None, typer.Option("--evidence-trades", help="Relevant trades of evidence behind it.")
    ] = None,
    bug_fix: Annotated[
        bool, typer.Option("--bug-fix", help="Exempt from the evidence bar (a bug or process fix).")
    ] = False,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Accept `proposals/<date>.md`, writing a new `PLAYBOOK.md` version. Requires
    `--bug-fix` or one of `--evidence-trading-days`/`--evidence-trades` to meet the
    configured minimum (BRIEF_INTRADAY.md's own "at least 10 trading days or 20 relevant
    trades... unless it is a bug or process fix") -- never inferred from the proposal's
    own text.
    """
    cfg = _setup(config, set_, verbose)
    from .intraday.ledger import IntradayLedger
    from .intraday.playbook import PlaybookEvidenceError, accept_proposal

    now = _intraday_now(cfg)
    ledger = IntradayLedger(cfg.path(cfg.intraday.ledger_path))
    try:
        version = accept_proposal(
            cfg,
            ledger,
            date,
            evidence_trading_days=evidence_trading_days,
            evidence_trades=evidence_trades,
            bug_fix=bug_fix,
            now=now,
        )
    except PlaybookEvidenceError as exc:
        typer.secho(f"Not accepted: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from None
    finally:
        ledger.close()

    typer.echo(f"Accepted. PLAYBOOK.md is now version {version}.")


@playbook_app.command("reject")
def playbook_reject(
    date: Annotated[
        str, typer.Option("--date", help="Proposal date, YYYY-MM-DD (proposals/<date>.md).")
    ],
    reason: Annotated[str, typer.Option("--reason", help="Why this proposal was rejected.")],
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Reject `proposals/<date>.md`. Never touches `PLAYBOOK.md`."""
    cfg = _setup(config, set_, verbose)
    from .intraday.ledger import IntradayLedger
    from .intraday.playbook import reject_proposal

    ledger = IntradayLedger(cfg.path(cfg.intraday.ledger_path))
    try:
        entry_id = reject_proposal(cfg, ledger, date, reason)
    finally:
        ledger.close()

    typer.echo(f"Rejected (entry {entry_id}).")


@playbook_app.command("retire")
def playbook_retire(
    entry_id: Annotated[
        int, typer.Option("--entry-id", help="The active playbook_entries row id to retire.")
    ],
    reason: Annotated[str, typer.Option("--reason", help="Why this rule stopped holding.")],
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Retire a currently-active playbook entry, writing a new `PLAYBOOK.md` version."""
    cfg = _setup(config, set_, verbose)
    from .intraday.ledger import IntradayLedger
    from .intraday.playbook import retire_entry

    now = _intraday_now(cfg)
    ledger = IntradayLedger(cfg.path(cfg.intraday.ledger_path))
    try:
        version = retire_entry(cfg, ledger, entry_id, reason, now)
    except ValueError as exc:
        typer.secho(f"Not retired: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from None
    finally:
        ledger.close()

    typer.echo(f"Retired. PLAYBOOK.md is now version {version}.")


# -- S4's placebo agent (BRIEF_INTRADAY.md's "Placebo agent", THESES.md's T6, Phase 7) ---


def _load_shadow_broker(cfg: Config, shadow_ledger, now):  # noqa: ANN001 -- returns FakeBroker
    from .execution.fake_broker import FakeBroker

    return FakeBroker.from_state(shadow_ledger.load_shadow_broker_state(), now=now)


def _run_placebo_cycle(
    cycle: str, as_of: str | None, config: Path | None, set_: list[str] | None, verbose: bool
) -> None:
    import random

    cfg = _setup(config, set_, verbose)
    from .intraday.ledger import IntradayLedger
    from .intraday.placebo import run_placebo_cycle

    as_of_ts = _intraday_as_of(as_of)
    now = _intraday_now(cfg)
    real_ledger = IntradayLedger(cfg.path(cfg.intraday.ledger_path))
    shadow_ledger = IntradayLedger(cfg.path(cfg.intraday.shadow_ledger_path))
    market_data = _intraday_market_data()
    shadow_broker = _load_shadow_broker(cfg, shadow_ledger, now)
    try:
        result = run_placebo_cycle(
            cycle,
            cfg,
            shadow_broker,
            shadow_ledger,
            real_ledger,
            market_data,
            as_of_ts,
            now,
            random.Random(),
        )
        shadow_ledger.save_shadow_broker_state(shadow_broker.to_state())
    finally:
        real_ledger.close()
        shadow_ledger.close()

    if result.skipped_reason is not None:
        typer.echo(f"Skipped: {result.skipped_reason}")
        return
    typer.echo(f"Proposed: {result.proposed_symbol}")
    if result.order_submitted:
        typer.echo(f"Order submitted: {result.order_submitted}")


@placebo_app.command("open-batch")
def placebo_open_batch(
    as_of: IntradayAsOfOption = None,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """The first placebo cycle of the day -- identical to `cycle`, just labeled
    "open_batch" for the shadow ledger and idempotency."""
    _run_placebo_cycle("open_batch", as_of, config, set_, verbose)


@placebo_app.command("cycle")
def placebo_cycle(
    as_of: IntradayAsOfOption = None,
    cycle: Annotated[
        str, typer.Option("--cycle", help="Label for this cycle, e.g. cycle_0835.")
    ] = "cycle",
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """One placebo cycle: the shared kill switch -> deterministic invalidation/time-stop
    enforcement -> one random watchlist entry, sized and risk-checked identically to the
    real Trader's own proposals. Meant to run on the same cadence as `intraday cycle`."""
    _run_placebo_cycle(cycle, as_of, config, set_, verbose)


@placebo_app.command("carry")
def placebo_carry(
    as_of: IntradayAsOfOption = None,
    config: ConfigOption = None,
    set_: SetOption = None,
    verbose: VerboseOption = False,
) -> None:
    """End-of-session for the shadow book: every placebo position is intraday-only by
    construction, so this always closes whatever's still held (`run_carry`'s own "no
    written multi-day thesis" safe default)."""
    cfg = _setup(config, set_, verbose)
    from .intraday.ledger import IntradayLedger
    from .intraday.placebo import run_placebo_carry

    as_of_ts = _intraday_as_of(as_of)
    now = _intraday_now(cfg)
    shadow_ledger = IntradayLedger(cfg.path(cfg.intraday.shadow_ledger_path))
    market_data = _intraday_market_data()
    shadow_broker = _load_shadow_broker(cfg, shadow_ledger, now)
    try:
        result = run_placebo_carry(cfg, shadow_broker, shadow_ledger, market_data, as_of_ts, now)
        shadow_ledger.save_shadow_broker_state(shadow_broker.to_state())
    finally:
        shadow_ledger.close()

    closed_str = ", ".join(result.closed) if result.closed else "(none)"
    carried_str = ", ".join(result.carried) if result.carried else "(none)"
    typer.echo(f"Closed (no multi-day thesis): {closed_str}")
    typer.echo(f"Carried: {carried_str}")
    if result.closed_for_cap:
        typer.secho(
            f"Closed for overnight cap: {', '.join(result.closed_for_cap)}", fg=typer.colors.YELLOW
        )


if __name__ == "__main__":  # pragma: no cover
    app()

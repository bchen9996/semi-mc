"""Streamlit dashboard: a browser front end that drives the CLI, not a replacement for it.

Run with:

    uv run --extra dashboard streamlit run src/semimc/dashboard/app.py

Every page below builds a ``semimc <command> ...`` argument list from form input and hands
it to :func:`semimc.dashboard.jobs.launch`, which is the same command a terminal user would
type. That keeps exactly one implementation of ``simulate``/``backtest``/``signals``/
``scenario`` (``cli.py``) and means the dashboard can never drift out of sync with it.

One deliberate omission: ``signals --holdout`` is not exposed here. It is a one-time,
already-spent evaluation (research_log.md, 2026-09-16) and CLAUDE.md forbids re-running it;
a dashboard button is exactly the kind of casual, repeatable action that resource must never
be. Use the CLI directly, deliberately, if that ever needs to change.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from semimc.config import PROJECT_ROOT, Config, load_config
from semimc.dashboard.jobs import extract_run_folder, launch
from semimc.models import MODEL_NAMES
from semimc.runs import find_run, load_manifest

st.set_page_config(page_title="semimc", layout="wide")


# -----------------------------------------------------------------------------------------
# Config, shared across every page
# -----------------------------------------------------------------------------------------


def _sidebar_config() -> tuple[Config, list[str]]:
    st.sidebar.title("semimc")
    config_path = st.sidebar.text_input(
        "Config file", value="", placeholder="config/default.yaml (default)"
    )
    overrides_text = st.sidebar.text_area(
        "Overrides (one key.path=value per line)",
        value="",
        help="Same syntax as the CLI's repeatable --set flag, e.g. drift.policy=risk_free.",
    )
    overrides = [
        line.strip()
        for line in overrides_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    config_args: list[str] = []
    if config_path.strip():
        config_args += ["--config", config_path.strip()]
    for override in overrides:
        config_args += ["--set", override]

    try:
        cfg = load_config(config_path.strip() or None, overrides or None)
    except Exception as exc:  # noqa: BLE001
        st.sidebar.error(f"Config failed to load: {exc}")
        st.stop()
        raise  # pragma: no cover -- st.stop() never returns

    st.sidebar.caption(f"{len(cfg.universe.tickers)} tickers, drift policy: {cfg.drift.policy}")
    return cfg, config_args


# -----------------------------------------------------------------------------------------
# Shared: run a command, stream its log, show the run it produced
# -----------------------------------------------------------------------------------------


def _run_and_show(
    label: str, args: list[str], cfg: Config, config_args: list[str], show_report: bool = True
) -> None:
    full_args = [*args, *config_args]
    lines: list[str] = []
    with st.status(f"Running: semimc {' '.join(full_args)}", expanded=True) as status:
        log_box = st.empty()
        job = launch(full_args, PROJECT_ROOT)
        for line in job.stream_lines():
            lines.append(line)
            log_box.code("\n".join(lines[-400:]), language=None)
        if job.succeeded:
            status.update(label=f"{label}: done", state="complete")
        else:
            status.update(label=f"{label}: failed (exit {job.returncode})", state="error")

    if not job.succeeded:
        st.error("Command failed; see the log above.")
        return

    if not show_report:
        st.success(f"{label} finished.")
        return

    run_path = extract_run_folder(lines)
    if run_path is None:
        st.warning(
            "Finished, but couldn't find which run folder was written. Check Runs & Reports."
        )
        return
    run_name = Path(run_path).name
    st.success(f"Run written to {run_name}")
    _show_report(cfg, config_args, run_name)


def _show_report(cfg: Config, config_args: list[str], run_name: str) -> None:
    try:
        folder = find_run(cfg.runs_path, run_name)
    except (FileNotFoundError, ValueError) as exc:
        st.error(str(exc))
        return

    report_path = folder / "report.html"
    if report_path.exists():
        clicked = st.button("Regenerate report", key=f"regen-{folder.name}")
    else:
        clicked = True  # build it once, automatically, the first time it's viewed

    if clicked:
        with st.spinner("Building report..."):
            job = launch(["report", "--run", folder.name, *config_args], PROJECT_ROOT)
            report_lines = list(job.stream_lines())
        if not job.succeeded:
            st.error("Report generation failed:\n" + "\n".join(report_lines[-20:]))
            return

    if report_path.exists():
        st.components.v1.html(report_path.read_text(encoding="utf-8"), height=1000, scrolling=True)
    else:
        st.info("This run produced no report.html (nothing to render for this command).")


# -----------------------------------------------------------------------------------------
# Pages
# -----------------------------------------------------------------------------------------


def page_overview(cfg: Config) -> None:
    st.header("Overview")
    col1, col2, col3 = st.columns(3)
    col1.metric("Universe tickers", len(cfg.universe.tickers))
    col2.metric("Benchmarks", ", ".join(cfg.benchmarks.tickers))
    col3.metric("Drift policy", cfg.drift.policy)

    st.subheader("Universe")
    for group, members in cfg.universe.groups.items():
        st.write(f"**{group}**: {', '.join(members)}")

    st.subheader("Recent runs")
    runs = _list_runs(cfg)
    if not runs:
        st.info("No runs yet -- try Simulate or Backtest from the sidebar.")
        return
    st.dataframe(_runs_table(runs[:10]), width="stretch")


def page_fetch_validate(cfg: Config, config_args: list[str]) -> None:
    st.header("Fetch & validate")
    st.caption("Both hit the network (yfinance, and optionally FRED for --risk-free/--industry).")

    st.subheader("Fetch")
    tickers = st.multiselect("Tickers (empty = configured universe)", options=cfg.all_tickers)
    c1, c2, c3, c4 = st.columns(4)
    force = c1.checkbox("--force")
    risk_free = c2.checkbox("--risk-free")
    earnings = c3.checkbox("--earnings")
    industry = c4.checkbox("--industry")
    if st.button("Run fetch", type="primary"):
        args = ["fetch"]
        if tickers:
            args += ["--tickers", ",".join(tickers)]
        if force:
            args.append("--force")
        if risk_free:
            args.append("--risk-free")
        if earnings:
            args.append("--earnings")
        if industry:
            args.append("--industry")
        _run_and_show("fetch", args, cfg, config_args, show_report=False)

    st.divider()
    st.subheader("Validate")
    if st.button("Run validate", type="primary"):
        _run_and_show("validate", ["validate"], cfg, config_args)


def page_simulate(cfg: Config, config_args: list[str]) -> None:
    st.header("Simulate")
    mode = st.radio("Target", ["Single ticker", "Portfolio"], horizontal=True)

    ticker = portfolio = None
    if mode == "Single ticker":
        ticker = st.text_input("Ticker", value=cfg.universe.tickers[0])
        st.caption(f"Configured universe: {', '.join(cfg.universe.tickers)}")
    else:
        portfolio = st.text_input(
            "Weights (TICKER=weight,...)",
            value="",
            placeholder="empty = equal-weight full universe",
        )

    c1, c2, c3 = st.columns(3)
    horizon = c1.number_input("Horizon (trading days)", min_value=1, value=126)
    model = c2.selectbox("Model", options=MODEL_NAMES, index=MODEL_NAMES.index("gbm"))
    paths = c3.number_input("Paths", min_value=100, value=cfg.simulation.n_paths, step=1000)

    c4, c5, c6 = st.columns(3)
    seed = c4.number_input("Seed", value=cfg.simulation.seed, step=1)
    above = c5.number_input("Report P(above) this price (0 = skip)", value=0.0, step=1.0)
    tilt = c6.checkbox(
        "--tilt (Phase 4B drift tilt)",
        help="Fits the pooled regression tilt live, on top of the configured drift policy.",
    )

    implied_vol = st.checkbox(
        "--implied-vol (live options-implied volatility)",
        help="Requires --model gbm and a single ticker. Fetches today's option chain over "
        "the network; cannot be backtested (see MODEL_NOTES §16).",
        disabled=(model != "gbm" or mode != "Single ticker"),
    )

    if st.button("Run simulation", type="primary"):
        if mode == "Single ticker" and not ticker.strip():
            st.error("Enter a ticker.")
            return
        args = [
            "simulate",
            "--horizon",
            str(int(horizon)),
            "--model",
            model,
            "--paths",
            str(int(paths)),
        ]
        args += (
            ["--ticker", ticker.strip().upper()]
            if mode == "Single ticker"
            else ["--portfolio", portfolio]
        )
        args += ["--seed", str(int(seed))]
        if above > 0:
            args += ["--above", str(above)]
        if tilt:
            args.append("--tilt")
        if implied_vol:
            args.append("--implied-vol")
        _run_and_show("simulate", args, cfg, config_args)


def page_backtest(cfg: Config, config_args: list[str]) -> None:
    st.header("Backtest")
    st.caption(
        "Walk-forward scoreboard. The full default universe/model/horizon set takes about "
        "3.5 minutes (CLAUDE.md) -- this page blocks until it finishes, same as the CLI."
    )

    models = st.multiselect("Models", options=MODEL_NAMES, default=list(cfg.backtest.models))
    horizons = st.multiselect("Horizons", options=cfg.horizons, default=list(cfg.horizons))
    tickers = st.multiselect(
        "Tickers", options=cfg.universe.tickers, default=list(cfg.universe.tickers)
    )

    c1, c2, c3, c4 = st.columns(4)
    start = c1.date_input("Start", value=cfg.backtest.start)
    end = c2.date_input("End (today = no limit)", value=None)
    paths = c3.number_input("Paths per origin", min_value=100, value=cfg.backtest.n_paths, step=500)
    seed = c4.number_input("Seed", value=cfg.simulation.seed, step=1)

    if st.button("Run backtest", type="primary"):
        if not models or not horizons or not tickers:
            st.error("Pick at least one model, horizon, and ticker.")
            return
        args = [
            "backtest",
            "--models",
            ",".join(models),
            "--horizons",
            ",".join(str(h) for h in horizons),
            "--tickers",
            ",".join(tickers),
            "--start",
            str(start),
            "--paths",
            str(int(paths)),
            "--seed",
            str(int(seed)),
        ]
        if end is not None:
            args += ["--end", str(end)]
        _run_and_show("backtest", args, cfg, config_args)


def page_signals(cfg: Config, config_args: list[str]) -> None:
    st.header("Signals")
    st.caption(
        "Cross-validated, pre-holdout evaluation of the Phase 4B signals. The one-time "
        "--holdout evaluation is deliberately not offered here -- see this page's module "
        "docstring and CLAUDE.md."
    )

    tickers = st.multiselect(
        "Tickers", options=cfg.universe.tickers, default=list(cfg.universe.tickers)
    )
    horizon = st.selectbox("Horizon", options=cfg.signals.horizons)

    if st.button("Run signals", type="primary"):
        if not tickers:
            st.error("Pick at least one ticker.")
            return
        args = ["signals", "--tickers", ",".join(tickers), "--horizon", str(horizon)]
        _run_and_show("signals", args, cfg, config_args)


def page_scenario(cfg: Config, config_args: list[str]) -> None:
    st.header("Scenario")
    if not cfg.scenarios:
        st.warning("No scenarios configured.")
        return

    name = st.selectbox("Scenario", options=list(cfg.scenarios))
    tickers = st.multiselect(
        "Tickers", options=cfg.universe.tickers, default=list(cfg.universe.tickers)
    )
    c1, c2, c3 = st.columns(3)
    horizon = c1.number_input("Horizon (trading days)", min_value=1, value=63)
    model = c2.selectbox("Model", options=MODEL_NAMES, index=MODEL_NAMES.index("gbm"))
    paths = c3.number_input("Paths", min_value=100, value=cfg.simulation.n_paths, step=1000)
    seed = st.number_input("Seed", value=cfg.simulation.seed, step=1)
    equal_weight = st.checkbox("Also report an equal-weighted portfolio", value=True)

    if st.button("Run scenario", type="primary"):
        if not tickers:
            st.error("Pick at least one ticker.")
            return
        args = [
            "scenario",
            "--name",
            name,
            "--tickers",
            ",".join(tickers),
            "--horizon",
            str(int(horizon)),
            "--model",
            model,
            "--paths",
            str(int(paths)),
            "--seed",
            str(int(seed)),
            "--equal-weight" if equal_weight else "--no-equal-weight",
        ]
        _run_and_show("scenario", args, cfg, config_args)


def page_runs(cfg: Config, config_args: list[str]) -> None:
    st.header("Runs & reports")
    runs = _list_runs(cfg)
    if not runs:
        st.info("No runs yet.")
        return
    st.dataframe(_runs_table(runs), width="stretch")

    names = [folder.name for folder, _ in runs]
    selected = st.selectbox("View a run", options=names)
    if selected:
        _show_report(cfg, config_args, selected)


# -----------------------------------------------------------------------------------------
# Run listing helpers
# -----------------------------------------------------------------------------------------


def _list_runs(cfg: Config) -> list[tuple[Path, dict]]:
    if not cfg.runs_path.exists():
        return []
    out = []
    for folder in sorted(cfg.runs_path.iterdir(), reverse=True):
        if not folder.is_dir():
            continue
        try:
            manifest = load_manifest(folder)
        except FileNotFoundError:
            manifest = {}
        out.append((folder, manifest))
    return out


def _runs_table(runs: list[tuple[Path, dict]]):
    import pandas as pd

    return pd.DataFrame(
        [
            {
                "run": folder.name,
                "command": manifest.get("command", "-") if manifest else "(incomplete)",
                "created_at": manifest.get("created_at", "-"),
                "seed": manifest.get("seed", "-"),
            }
            for folder, manifest in runs
        ]
    ).set_index("run")


# -----------------------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------------------


def main() -> None:
    cfg, config_args = _sidebar_config()
    page = st.sidebar.radio(
        "Page",
        [
            "Overview",
            "Fetch & validate",
            "Simulate",
            "Backtest",
            "Signals",
            "Scenario",
            "Runs & reports",
        ],
    )
    if page == "Overview":
        page_overview(cfg)
    elif page == "Fetch & validate":
        page_fetch_validate(cfg, config_args)
    elif page == "Simulate":
        page_simulate(cfg, config_args)
    elif page == "Backtest":
        page_backtest(cfg, config_args)
    elif page == "Signals":
        page_signals(cfg, config_args)
    elif page == "Scenario":
        page_scenario(cfg, config_args)
    elif page == "Runs & reports":
        page_runs(cfg, config_args)


main()

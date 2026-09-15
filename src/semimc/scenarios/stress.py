"""Stress scenarios.

These answer a different question from the forecast. The forecast asks "what does the
model think is likely?"; a scenario asks "if *this* happened, what would it do to me?" --
a conditional statement with no probability attached. Scenario output is therefore never
labelled with a likelihood.

All four kinds transform an already-simulated array of daily log returns, so any model can
be stressed without knowing anything about how it works.

``sector_shock``       an instantaneous sector move, propagated through each ticker's beta
``custom_shock``       per-ticker instantaneous moves, specified directly
``vol_multiplier``     volatility scaled by a factor for the first N days
``historical_replay``  a real historical benchmark path, re-scaled to current betas
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)

SCENARIO_KINDS = {"sector_shock", "custom_shock", "vol_multiplier", "historical_replay"}


@dataclass
class ScenarioResult:
    """Outcome of one scenario, alongside the unstressed baseline."""

    name: str
    kind: str
    tickers: list[str]
    terminal_log_returns: np.ndarray  # (n_paths, n_tickers), stressed
    baseline_terminal: np.ndarray  # (n_paths, n_tickers), unstressed
    description: str = ""
    portfolio_terminal: np.ndarray | None = None
    portfolio_baseline: np.ndarray | None = None

    def table(self) -> pd.DataFrame:
        """Median and tail impact per ticker, stressed versus baseline, in simple returns."""
        stressed = np.expm1(self.terminal_log_returns)
        baseline = np.expm1(self.baseline_terminal)
        records = []
        for index, ticker in enumerate(self.tickers):
            records.append(
                {
                    "ticker": ticker,
                    "baseline_median": float(np.median(baseline[:, index])),
                    "scenario_median": float(np.median(stressed[:, index])),
                    "median_impact": float(
                        np.median(stressed[:, index]) - np.median(baseline[:, index])
                    ),
                    "baseline_p5": float(np.percentile(baseline[:, index], 5)),
                    "scenario_p5": float(np.percentile(stressed[:, index], 5)),
                    "scenario_prob_loss_20pct": float(np.mean(stressed[:, index] <= -0.20)),
                }
            )
        frame = pd.DataFrame(records).set_index("ticker")
        if self.portfolio_terminal is not None and self.portfolio_baseline is not None:
            frame.loc["PORTFOLIO"] = {
                "baseline_median": float(np.median(self.portfolio_baseline)),
                "scenario_median": float(np.median(self.portfolio_terminal)),
                "median_impact": float(
                    np.median(self.portfolio_terminal) - np.median(self.portfolio_baseline)
                ),
                "baseline_p5": float(np.percentile(self.portfolio_baseline, 5)),
                "scenario_p5": float(np.percentile(self.portfolio_terminal, 5)),
                "scenario_prob_loss_20pct": float(np.mean(self.portfolio_terminal <= -0.20)),
            }
        return frame


def apply_instantaneous_shock(
    log_return_paths: np.ndarray,
    shocks_by_ticker: np.ndarray,
    step: int = 0,
) -> np.ndarray:
    """Add an instantaneous move to one step of every path.

    The shock is supplied as a **simple** return and converted with ``log1p`` before being
    added, so a -25% shock is exactly -25% rather than the -22% that adding -0.25 in log
    space would give.

    Parameters
    ----------
    log_return_paths
        Simulated daily log returns, shape ``(n_paths, horizon, n_tickers)``.
    shocks_by_ticker
        Simple returns, shape ``(n_tickers,)``.
    step
        Horizon step the shock lands on.

    Returns
    -------
    np.ndarray
        A modified copy.
    """
    out = log_return_paths.copy()
    out[:, step, :] += np.log1p(shocks_by_ticker).reshape(1, -1)
    return out


def apply_volatility_multiplier(
    log_return_paths: np.ndarray,
    multiplier: float,
    days: int,
    daily_drift: np.ndarray,
) -> np.ndarray:
    """Scale volatility for the first ``days`` steps, leaving the drift alone.

    Deviations from the configured drift are multiplied, not the returns themselves, so a
    2x volatility scenario doubles the *risk* without also doubling the expected return.
    """
    out = log_return_paths.copy()
    window = min(days, out.shape[1])
    drift = daily_drift.reshape(1, 1, -1)
    out[:, :window, :] = drift + multiplier * (out[:, :window, :] - drift)
    return out


def apply_historical_replay(
    log_return_paths: np.ndarray,
    benchmark_path: np.ndarray,
    betas: np.ndarray,
    keep_idiosyncratic: bool = True,
) -> np.ndarray:
    """Overlay a historical benchmark path, scaled to each ticker's current beta.

    Parameters
    ----------
    log_return_paths
        Simulated daily log returns, shape ``(n_paths, horizon, n_tickers)``.
    benchmark_path
        Realized daily benchmark log returns from the replayed window, shape ``(n_days,)``.
        Truncated to the horizon if longer, and left to end early if shorter -- the rest
        of the horizon then evolves normally, which is the sensible reading of "March 2020
        happens, then life continues".
    betas
        Each ticker's beta to that benchmark, shape ``(n_tickers,)``.
    keep_idiosyncratic
        Keep the simulated idiosyncratic component on top of the replayed systematic path.
        Turning it off gives a single deterministic path per ticker -- cleaner to read,
        but it hides the fact that individual names dispersed wildly around the index in
        March 2020.

    Returns
    -------
    np.ndarray
        A modified copy.
    """
    out = log_return_paths.copy()
    n_days = min(len(benchmark_path), out.shape[1])
    systematic = benchmark_path[:n_days].reshape(1, n_days, 1) * betas.reshape(1, 1, -1)
    if keep_idiosyncratic:
        # Replace the systematic part of the window: scale the simulated returns down by
        # beta's share is not identifiable path by path, so the replay is *added* to a
        # de-meaned simulated window instead.
        window = out[:, :n_days, :]
        out[:, :n_days, :] = window - window.mean(axis=0, keepdims=True) + systematic
    else:
        out[:, :n_days, :] = np.broadcast_to(systematic, (out.shape[0], n_days, out.shape[2]))
    return out


def _terminal(paths: np.ndarray) -> np.ndarray:
    return paths.sum(axis=1)


def _portfolio_terminal(paths: np.ndarray, weights: np.ndarray | None) -> np.ndarray | None:
    """Portfolio terminal simple return, aggregated across holdings in simple space."""
    if weights is None:
        return None
    growth = np.exp(paths.sum(axis=1))
    return growth @ (weights / weights.sum()) - 1.0


def run_scenario(
    name: str,
    spec: dict,
    log_return_paths: np.ndarray,
    tickers: list[str],
    betas_sector: pd.Series | None = None,
    betas_market: pd.Series | None = None,
    daily_drift: pd.Series | None = None,
    benchmark_returns: pd.Series | None = None,
    weights: pd.Series | None = None,
) -> ScenarioResult:
    """Run one configured scenario over an already-simulated path array.

    Parameters
    ----------
    name
        Scenario name, as used by ``semimc scenario --name``.
    spec
        The scenario's config block; must contain ``kind``.
    log_return_paths
        Baseline simulated daily log returns, shape ``(n_paths, horizon, n_tickers)``.
    tickers
        Column order of the path array.
    betas_sector, betas_market
        Betas used to propagate sector shocks and historical replays.
    daily_drift
        Per-ticker daily log drift, needed by the volatility multiplier.
    benchmark_returns
        Realized daily benchmark log returns, indexed by date, for replays.
    weights
        Optional portfolio weights, to report a portfolio-level impact.

    Returns
    -------
    ScenarioResult
    """
    kind = spec.get("kind")
    if kind not in SCENARIO_KINDS:
        raise ValueError(
            f"scenario {name!r} has unknown kind {kind!r}; expected one of {SCENARIO_KINDS}"
        )

    weight_vector = (
        weights.reindex(tickers).fillna(0.0).to_numpy(dtype=float) if weights is not None else None
    )
    baseline_terminal = _terminal(log_return_paths)
    baseline_portfolio = _portfolio_terminal(log_return_paths, weight_vector)

    if kind == "sector_shock":
        shock = float(spec["shock"])
        if betas_sector is None:
            raise ValueError(f"scenario {name!r} needs sector betas")
        betas = betas_sector.reindex(tickers).fillna(1.0).to_numpy(dtype=float)
        # A beta-2 name takes twice the sector's move: shock it by beta * shock in simple terms.
        stressed = apply_instantaneous_shock(log_return_paths, betas * shock)
        description = (
            f"Instantaneous {shock:+.0%} move in the sector benchmark, propagated through "
            f"each ticker's sector beta (range {betas.min():.2f} to {betas.max():.2f})."
        )

    elif kind == "custom_shock":
        shocks = spec.get("shocks", {})
        vector = np.array([float(shocks.get(t, 0.0)) for t in tickers], dtype=float)
        stressed = apply_instantaneous_shock(log_return_paths, vector)
        named = ", ".join(f"{t} {shocks[t]:+.0%}" for t in tickers if t in shocks)
        description = f"Custom instantaneous shocks: {named or 'none for these tickers'}."

    elif kind == "vol_multiplier":
        if daily_drift is None:
            raise ValueError(f"scenario {name!r} needs the per-ticker daily drift")
        multiplier = float(spec["multiplier"])
        days = int(spec.get("days", 21))
        stressed = apply_volatility_multiplier(
            log_return_paths,
            multiplier,
            days,
            daily_drift.reindex(tickers).fillna(0.0).to_numpy(dtype=float),
        )
        description = (
            f"Volatility multiplied by {multiplier:.1f}x for the first {days} trading days, "
            "with drift held at its configured value."
        )

    elif kind == "historical_replay":
        if benchmark_returns is None:
            raise ValueError(f"scenario {name!r} needs benchmark returns to replay")
        betas_source = betas_sector if betas_sector is not None else betas_market
        if betas_source is None:
            raise ValueError(f"scenario {name!r} needs betas")
        window = benchmark_returns.loc[pd.Timestamp(spec["start"]) : pd.Timestamp(spec["end"])]
        if window.empty:
            raise ValueError(
                f"scenario {name!r}: no benchmark data between {spec['start']} and {spec['end']}"
            )
        betas = betas_source.reindex(tickers).fillna(1.0).to_numpy(dtype=float)
        stressed = apply_historical_replay(log_return_paths, window.to_numpy(dtype=float), betas)
        realized = float(np.expm1(window.sum()))
        description = (
            f"Replay of {pd.Timestamp(spec['start']).date()} to "
            f"{pd.Timestamp(spec['end']).date()} ({len(window)} sessions, benchmark "
            f"{realized:+.1%}), re-scaled to each ticker's current beta. Idiosyncratic "
            "dispersion from the model is retained."
        )
    else:  # pragma: no cover - guarded above
        raise AssertionError(kind)

    return ScenarioResult(
        name=name,
        kind=kind,
        tickers=list(tickers),
        terminal_log_returns=_terminal(stressed),
        baseline_terminal=baseline_terminal,
        description=description,
        portfolio_terminal=_portfolio_terminal(stressed, weight_vector),
        portfolio_baseline=baseline_portfolio,
    )

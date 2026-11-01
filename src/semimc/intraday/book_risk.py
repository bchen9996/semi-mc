"""The book's risk, read every cycle from a model built once a day (added 2026-10-31).

S4 is a few semiconductor names at a time, and SOXX explains about 61% of what they do over
30 minutes, so several positions can be one bet. This module says how much of one: net SOXX
exposure, a Monte Carlo of the book to the close, how much of its risk is the sector, each
name's z-score, and how each name correlates with what's already held.

Only the net exposure is a rule (`risk.net_beta_cap_pct`, in `proposals.py::
validate_proposal`). The rest is information for the Trader and the daily report: at these
sizes the simulation rarely comes near a loss limit, and nothing here has been shown to make
money. **A z-score is not a signal.** Fading residual moves had no edge from 2024 to mid-2026
(`research_log.md`, 2026-10-31).

The model comes from `lookback_sessions` of 30-minute bars ending the night before, built by
the day's first cycle and stored in the ledger: a cycle's runner has no price cache, and each
day's model stays on record. Intraday bars rather than `MarketData`'s daily returns, because
S4 holds through the session and not the overnight gap that dominates a daily close-to-close
figure.

The simulation assumes zero drift, draws one move to the close with Student-t marginals
through a Gaussian copula (Cholesky on t draws breaks the marginals; see `CLAUDE.md`), scales
variance with the share of the session left, and aggregates in simple returns. It ignores
stops, so its tail is what the book could lose if none of them held, and trailing volatility,
so it understates a day like 2026-10-30.
"""

from __future__ import annotations

import datetime as dt
import math
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from ..config import IntradayConfig
from ..models.gbm import nearest_psd_cholesky
from .ledger import IntradayLedger

_EXCHANGE_TZ = "America/New_York"
_BAR_MINUTES = 30
_SESSION_BARS = 13
_FIRST_BAR = "09:30"
_LAST_BAR = "15:30"
# Before this there's one half hour of history behind a z-score, and it opens wide.
_FIRST_Z_CLOCK = "10:00"


@dataclass(frozen=True)
class RiskModel:
    """One morning's view of the universe. `covariance` is of log returns over a whole
    session, shape ``(n, n)`` in `symbols` order. `spreads[clock][symbol]` is the standard
    deviation of the symbol's move since the open, net of its beta to the hedge instrument,
    at that time of day (the bar's end, HH:MM New York)."""

    symbols: tuple[str, ...]
    betas: dict[str, float]
    covariance: np.ndarray
    spreads: dict[str, dict[str, float]]
    sessions: int
    first_day: str
    last_day: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "betas": self.betas,
            "covariance": self.covariance.tolist(),
            "spreads": self.spreads,
            "sessions": self.sessions,
            "first_day": self.first_day,
            "last_day": self.last_day,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> RiskModel:
        return cls(
            symbols=tuple(payload["symbols"]),
            betas=dict(payload["betas"]),
            covariance=np.array(payload["covariance"], dtype=float),
            spreads={clock: dict(row) for clock, row in payload["spreads"].items()},
            sessions=payload["sessions"],
            first_day=payload["first_day"],
            last_day=payload["last_day"],
        )


def _session_moves(bars: Mapping[str, list[Any]], before_day: str) -> pd.DataFrame:
    """Each symbol's log move from the session's open to each bar's end, on days before
    `before_day`. Index ``(day, clock)``, one column per symbol."""
    rows = [
        (symbol, pd.Timestamp(bar.timestamp), float(bar.open), float(bar.close))
        for symbol, symbol_bars in bars.items()
        for bar in symbol_bars
    ]
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows, columns=["symbol", "ts", "open", "close"])
    stamps = pd.to_datetime(frame["ts"], utc=True).dt.tz_convert(_EXCHANGE_TZ)
    frame["day"] = stamps.dt.strftime("%Y-%m-%d")
    frame["start"] = stamps.dt.strftime("%H:%M")
    frame["clock"] = (stamps + pd.Timedelta(minutes=_BAR_MINUTES)).dt.strftime("%H:%M")
    frame = frame[frame["start"].between(_FIRST_BAR, _LAST_BAR) & (frame["day"] < before_day)]
    session_open = frame[frame["start"] == _FIRST_BAR].set_index(["day", "symbol"])["open"]
    keys = pd.MultiIndex.from_frame(frame[["day", "symbol"]])
    frame = frame.assign(move=np.log(frame["close"].to_numpy() / session_open.reindex(keys).values))
    moves = frame.pivot_table(index=["day", "clock"], columns="symbol", values="move")
    return moves.groupby(level="day").ffill()


def build_risk_model(
    market_data: Any, config: IntradayConfig, tickers: Iterable[str], as_of: pd.Timestamp
) -> RiskModel | None:
    """The model for `as_of`, from full sessions before it. `None` with fewer than
    `book_risk.min_sessions` of them. A half day is left out: its bars aren't a session."""
    settings = config.book_risk
    instrument = config.hedge.instrument
    symbols = list(dict.fromkeys([*tickers, instrument]))
    day = pd.Timestamp(as_of).normalize()
    end = day.tz_localize(_EXCHANGE_TZ).tz_convert("UTC").to_pydatetime()
    start = end - dt.timedelta(days=settings.lookback_sessions * 7 // 5 + 10)
    moves = _session_moves(
        market_data.bars(symbols, start, end, minutes=_BAR_MINUTES), str(day.date())
    )
    if moves.empty or instrument not in moves.columns:
        return None

    bars_per_day = moves[instrument].notna().groupby(level="day").sum()
    days = bars_per_day[bars_per_day == _SESSION_BARS].index[-settings.lookback_sessions :]
    if len(days) < settings.min_sessions:
        return None
    moves = moves[moves.index.get_level_values("day").isin(days)]

    returns = moves.groupby(level="day").diff().fillna(moves)
    columns = [c for c in returns.columns if returns[c].notna().sum() >= settings.min_sessions]
    variance = returns[instrument].var()
    betas = {c: float(returns[c].cov(returns[instrument]) / variance) for c in columns}
    spreads: dict[str, dict[str, float]] = {}
    for clock, at_clock in moves.groupby(level="clock"):
        spreads[str(clock)] = {
            c: float((at_clock[c] - betas[c] * at_clock[instrument]).std()) for c in columns
        }
    return RiskModel(
        symbols=tuple(columns),
        betas=betas,
        covariance=returns[columns].cov().to_numpy() * _SESSION_BARS,
        spreads=spreads,
        sessions=len(days),
        first_day=str(days[0]),
        last_day=str(days[-1]),
    )


def ensure_risk_model(
    ledger: IntradayLedger,
    market_data: Any,
    config: IntradayConfig,
    tickers: Iterable[str],
    as_of: pd.Timestamp,
) -> RiskModel | None:
    """Today's model, built and stored by whichever cycle needs it first."""
    stored = ledger.risk_model_for(as_of)
    if stored is not None:
        return RiskModel.from_payload(stored)
    model = build_risk_model(market_data, config, tickers, as_of)
    if model is not None:
        ledger.record_risk_model(as_of, model.to_payload())
    return model


def session_fraction_left(now: dt.datetime, close_at: pd.Timestamp) -> float:
    """The share of the session still to come, 0 to 1. `close_at` is aware."""
    open_at = close_at.normalize() + pd.Timedelta(hours=9, minutes=30)
    moment = pd.Timestamp(now if now.tzinfo else now.replace(tzinfo=dt.UTC))
    return float(np.clip((close_at - moment) / (close_at - open_at), 0.0, 1.0))


def z_scores(
    model: RiskModel, session: Mapping[str, Mapping[str, Any]], instrument: str
) -> dict[str, float]:
    """Each name's move since the open, net of its beta to `instrument`, in units of how
    far that usually runs by this time of day. `session` is `session.py`'s snapshot. Empty
    before the first half hour is complete, or without the instrument's bars."""
    reference = session.get(instrument) or {}
    start = reference.get("last_bar_start_et")
    if not start or not reference.get("open") or not reference.get("last"):
        return {}
    hours, minutes = (int(part) for part in start.split(":"))
    finished = hours * 60 + minutes + 5  # a 5-minute bar, so it ended 5 minutes later
    floored = finished - finished % _BAR_MINUTES
    clock = f"{floored // 60:02d}:{floored % 60:02d}"
    spreads = model.spreads.get(clock)
    if clock < _FIRST_Z_CLOCK or spreads is None:
        return {}
    sector_move = math.log(reference["last"] / reference["open"])
    out: dict[str, float] = {}
    for symbol in model.symbols:
        row = session.get(symbol) or {}
        spread = spreads.get(symbol)
        if symbol == instrument or not spread or not row.get("open") or not row.get("last"):
            continue
        residual = math.log(row["last"] / row["open"]) - model.betas[symbol] * sector_move
        out[symbol] = residual / spread
    return out


def simulate_book(
    positions: Mapping[str, float],
    model: RiskModel,
    fraction_left: float,
    n_paths: int,
    df: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """Simulated dollar P&L to the close, shape ``(n_paths,)``. `positions` is signed
    dollars by symbol, each in `model.symbols`. Each symbol's log return is Student-t with
    the model's variance scaled by `fraction_left`, coupled by a Gaussian copula; dollars
    come from simple returns, since log returns don't add across a portfolio."""
    symbols = list(positions)
    if not symbols or fraction_left <= 0:
        return np.zeros(n_paths)
    index = [model.symbols.index(symbol) for symbol in symbols]
    covariance = model.covariance[np.ix_(index, index)] * fraction_left
    vols = np.sqrt(np.diag(covariance))
    correlation = covariance / np.outer(vols, vols)
    normals = (
        rng.standard_normal(size=(n_paths, len(symbols))) @ nearest_psd_cholesky(correlation).T
    )
    uniforms = np.clip(stats.norm.cdf(normals), 1e-12, 1 - 1e-12)
    shocks = stats.t.ppf(uniforms, df=df) / np.sqrt(df / (df - 2.0))
    notionals = np.array([positions[symbol] for symbol in symbols])
    return (np.expm1(shocks * vols) * notionals).sum(axis=1)


def net_beta_usd(positions: Mapping[str, float], betas: Mapping[str, float]) -> float:
    """Dollars of the hedge instrument the book moves like. A symbol with no beta counts as
    1: a limit shouldn't lapse because a number is missing."""
    return sum(notional * betas.get(symbol, 1.0) for symbol, notional in positions.items())


def _seed(as_of: pd.Timestamp, cycle: str, label: str) -> int:
    """Deterministic, so any day's simulation can be reproduced from the ledger."""
    return zlib.crc32(f"{pd.Timestamp(as_of).date()}:{cycle}:{label}".encode())


def measure_book(
    positions: Mapping[str, float],
    equity: float,
    session_open_equity: float,
    model: RiskModel,
    fraction_left: float,
    config: IntradayConfig,
    as_of: pd.Timestamp,
    cycle: str,
    label: str,
) -> dict[str, Any]:
    """The whole-book figures. `tail_loss_usd` is the `tail_probability` quantile of P&L to
    the close, `soft_limit_probability` the chance the day ends past `daily_loss_soft_pct`
    given what it has made so far, and `sector_share` the part of the book's variance that
    its net exposure to the hedge instrument accounts for."""
    settings = config.book_risk
    held = {s: n for s, n in positions.items() if n and s in model.symbols}
    seed = _seed(as_of, cycle, label)
    rng = np.random.default_rng(seed)
    pnl = simulate_book(held, model, fraction_left, settings.mc_paths, settings.student_t_df, rng)
    limit = config.risk.daily_loss_soft_pct * session_open_equity
    day_so_far = equity - session_open_equity

    sector_share = None
    if held:
        symbols = list(held)
        index = [model.symbols.index(symbol) for symbol in symbols]
        notionals = np.array([held[symbol] for symbol in symbols])
        total = float(notionals @ model.covariance[np.ix_(index, index)] @ notionals)
        reference = model.symbols.index(config.hedge.instrument)
        sector = net_beta_usd(held, model.betas) ** 2 * model.covariance[reference, reference]
        sector_share = min(sector / total, 1.0) if total > 0 else None

    return {
        "positions": dict(positions),
        "net_beta_pct": net_beta_usd(positions, model.betas) / equity,
        "net_beta_cap_pct": config.risk.net_beta_cap_pct,
        "gross_pct": sum(abs(n) for n in positions.values()) / equity,
        "tail_probability": settings.tail_probability,
        "tail_loss_usd": float(np.quantile(pnl, settings.tail_probability)),
        "soft_limit_probability": float(np.mean(day_so_far + pnl <= -limit)),
        "sector_share": sector_share,
        "fraction_left": fraction_left,
        "not_modeled": sorted(set(positions) - set(model.symbols)),
        "seed": seed,
    }


def correlation_with_book(
    positions: Mapping[str, float], model: RiskModel, symbols: Iterable[str]
) -> dict[str, float]:
    """How each of `symbols` has moved with the book as it stands, -1 to 1. Near 1, a new
    position in it adds to the same bet; near 0 or below, it spreads the risk."""
    held = {s: n for s, n in positions.items() if n and s in model.symbols}
    if not held:
        return {}
    weights = np.zeros(len(model.symbols))
    for symbol, notional in held.items():
        weights[model.symbols.index(symbol)] = notional
    with_book = model.covariance @ weights
    book_variance = float(weights @ with_book)
    if book_variance <= 0:
        return {}
    out = {}
    for symbol in symbols:
        if symbol not in model.symbols:
            continue
        index = model.symbols.index(symbol)
        out[symbol] = float(
            with_book[index] / math.sqrt(model.covariance[index, index] * book_variance)
        )
    return out


def trader_section(
    measured: Mapping[str, Any],
    model: RiskModel,
    session: Mapping[str, Mapping[str, Any]],
    symbols: Iterable[str],
    config: IntradayConfig,
) -> dict[str, Any]:
    """What the Trader sees: the book's figures, and for each name in view its beta, z-score
    and correlation with the book."""
    names = sorted(set(symbols))
    scores = z_scores(model, session, config.hedge.instrument)
    return {
        "net_beta_pct": measured["net_beta_pct"],
        "net_beta_cap_pct": measured["net_beta_cap_pct"],
        "tail_probability": measured["tail_probability"],
        "tail_loss_usd": measured["tail_loss_usd"],
        "soft_limit_probability": measured["soft_limit_probability"],
        "sector_share": measured["sector_share"],
        "betas": {s: model.betas[s] for s in names if s in model.betas},
        "z_scores": {s: scores[s] for s in names if s in scores},
        "correlation_with_book": correlation_with_book(measured["positions"], model, names),
    }


def summarize_book_risk(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The day's extremes, from `ledger.book_risk_for`'s per-cycle rows, for the daily
    report. `None` if no cycle measured the book."""
    measured = [row["payload"] for row in rows if row["label"] == "book"]
    if not measured:
        return None
    held_z = [
        (symbol, score)
        for payload in measured
        for symbol, score in (payload.get("z_scores") or {}).items()
        if payload["positions"].get(symbol)
    ]
    shares = [p["sector_share"] for p in measured if p.get("sector_share") is not None]
    return {
        "cycles": len(measured),
        "peak_net_beta_pct": max((p["net_beta_pct"] for p in measured), key=abs),
        "net_beta_cap_pct": measured[-1]["net_beta_cap_pct"],
        "tail_probability": measured[-1]["tail_probability"],
        "largest_tail_loss_usd": min(p["tail_loss_usd"] for p in measured),
        "highest_soft_limit_probability": max(p["soft_limit_probability"] for p in measured),
        "highest_sector_share": max(shares) if shares else None,
        "largest_held_z": max(held_z, key=lambda item: abs(item[1])) if held_z else None,
    }

"""Drift policies.

Section 8 of the brief: *"Apply the configured drift policy to every model; none may
silently inherit the historical sample mean."* This module is the single place a target
expected return is decided; :class:`~semimc.models.base.BaseReturnModel` then forces every
model's simulated paths onto it.

Everything here works in **annual arithmetic** terms -- the continuously-compounded
expected growth rate ``mu_A`` such that ``E[S_T] / S_0 = exp(mu_A * T)``. The conversion
to the log-space drift a simulator actually needs, ``mu_A - sigma^2 / 2``, happens in
``base.py`` because it requires the model's own variance.

Why not the historical mean: NVDA compounded at roughly 70% a year over the five years to
2025. Feeding that forward as an expectation would project a share price in the thousands
and every risk number downstream would be nonsense. CAPM ties the drift to systematic risk
instead, which at least has a defensible story behind it.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..data.returns import estimate_beta

LOGGER = logging.getLogger(__name__)


def historical_annual_arithmetic(
    log_returns: pd.Series,
    trading_days_per_year: int = 252,
) -> float:
    """Annualized arithmetic drift implied by a sample of daily log returns.

    ``mu_A = 252 * (mean + variance / 2)``: the sample log mean plus the Ito correction,
    so the result is comparable with a CAPM expected return rather than with a log drift.
    """
    clean = log_returns.dropna()
    if len(clean) < 2:
        return float("nan")
    mean = float(clean.mean())
    variance = float(clean.var(ddof=1))
    return trading_days_per_year * (mean + variance / 2.0)


def capm_annual_arithmetic(beta: float, risk_free: float, equity_risk_premium: float) -> float:
    """``rf + beta * ERP``. A non-finite beta falls back to a beta of 1."""
    used_beta = 1.0 if not np.isfinite(beta) else beta
    return float(risk_free + used_beta * equity_risk_premium)


def annual_arithmetic_drift(
    tickers: list[str],
    log_returns: pd.DataFrame,
    benchmark_returns: pd.Series | None,
    policy: str,
    risk_free: float,
    equity_risk_premium: float = 0.05,
    shrinkage_weight: float = 0.25,
    manual_annual_return: dict[str, float] | None = None,
    trading_days_per_year: int = 252,
) -> pd.Series:
    """Target annual arithmetic expected return per ticker.

    Parameters
    ----------
    tickers
        Symbols to produce a drift for.
    log_returns
        Training panel, already truncated to the forecast origin. Index: dates.
        Columns: tickers.
    benchmark_returns
        Daily log returns of the benchmark CAPM betas are measured against. ``None``
        forces a beta of 1 under the CAPM policies.
    policy
        ``capm``, ``risk_free``, ``shrunk_historical``, or ``manual``.
    risk_free
        Annualized risk-free rate in force at the origin, as a decimal.
    equity_risk_premium
        Annual equity risk premium used by the CAPM policies.
    shrinkage_weight
        Weight on the historical estimate under ``shrunk_historical``; the remainder goes
        on CAPM.
    manual_annual_return
        Per-ticker overrides, required under ``manual``.
    trading_days_per_year
        Annualization factor.

    Returns
    -------
    pd.Series
        Annual arithmetic drift per ticker, indexed by ticker.
    """
    manual = manual_annual_return or {}
    out: dict[str, float] = {}

    for ticker in tickers:
        if policy == "risk_free":
            out[ticker] = float(risk_free)
            continue

        if policy == "manual":
            if ticker not in manual:
                raise ValueError(
                    f"drift.policy = manual but no drift.manual_annual_return for {ticker!r}"
                )
            out[ticker] = float(manual[ticker])
            continue

        series = (
            log_returns[ticker].dropna()
            if ticker in log_returns.columns
            else pd.Series(dtype=float)
        )
        beta = (
            estimate_beta(series, benchmark_returns)
            if benchmark_returns is not None and not series.empty
            else float("nan")
        )
        capm = capm_annual_arithmetic(beta, risk_free, equity_risk_premium)

        if policy == "capm":
            out[ticker] = capm
            continue

        if policy == "shrunk_historical":
            historical = historical_annual_arithmetic(series, trading_days_per_year)
            if not np.isfinite(historical):
                LOGGER.warning(
                    "%s has too little history for a sample mean; using CAPM alone", ticker
                )
                out[ticker] = capm
            else:
                out[ticker] = shrinkage_weight * historical + (1.0 - shrinkage_weight) * capm
            continue

        raise ValueError(f"unknown drift policy {policy!r}")

    return pd.Series(out, name="annual_arithmetic_drift", dtype=float)

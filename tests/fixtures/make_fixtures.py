"""Generate the deterministic fixture datasets the test suite runs on.

Run with ``uv run python tests/fixtures/make_fixtures.py`` to regenerate. The output CSVs
are committed so tests never touch the network and never depend on this script staying
identical.

The data is synthetic but structured like the real thing, because tests that check
statistical behaviour need data with that behaviour in it:

* a market factor and a sector factor, so betas and the factor model have something to find;
* GJR-GARCH volatility with known parameters, so parameter recovery can be tested;
* Student-t innovations, so the fat tails are real;
* scheduled earnings-day jumps on NVDA, so the earnings overlay has something to measure;
* one deliberately corrupted file (an unadjusted 10-for-1 split, plus a gap and a stale
  run) so the validation report has defects to catch.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent

# GJR-GARCH(1,1)-t parameters the recovery test asserts against, in percent-return units.
TRUE_OMEGA = 0.05
TRUE_ALPHA = 0.06
TRUE_GAMMA = 0.08
TRUE_BETA = 0.86
TRUE_NU = 6.0

N_DAYS = 2000
START = "2016-01-04"
SEED = 20240101


def simulate_gjr_garch(
    n: int,
    rng: np.random.Generator,
    omega: float,
    alpha: float,
    gamma: float,
    beta: float,
    nu: float,
) -> np.ndarray:
    """Simulate a zero-mean GJR-GARCH(1,1) series with standardized-t innovations."""
    innovations = rng.standard_t(df=nu, size=n) / np.sqrt(nu / (nu - 2.0))
    out = np.empty(n)
    variance = omega / max(1.0 - alpha - gamma / 2.0 - beta, 1e-3)
    for t in range(n):
        shock = np.sqrt(variance) * innovations[t]
        out[t] = shock
        variance = omega + (alpha + gamma * (shock < 0)) * shock**2 + beta * variance
    return out


def build() -> None:
    rng = np.random.default_rng(SEED)
    dates = pd.bdate_range(START, periods=N_DAYS)

    # Factors, in percent units so the GARCH parameters above are on a natural scale.
    market = simulate_gjr_garch(N_DAYS, rng, 0.01, 0.05, 0.06, 0.88, 8.0)
    sector_extra = simulate_gjr_garch(N_DAYS, rng, 0.02, 0.05, 0.05, 0.87, 8.0)
    sector = 1.30 * market + sector_extra

    # NVDA carries the known GARCH parameters in its idiosyncratic component.
    nvda_idio = simulate_gjr_garch(
        N_DAYS, rng, TRUE_OMEGA, TRUE_ALPHA, TRUE_GAMMA, TRUE_BETA, TRUE_NU
    )
    amd_idio = simulate_gjr_garch(N_DAYS, rng, 0.06, 0.05, 0.07, 0.85, 7.0)

    nvda = 1.10 * market + 0.60 * sector_extra + nvda_idio
    amd = 1.05 * market + 0.70 * sector_extra + amd_idio

    # Quarterly earnings jumps for NVDA and AMD, on a fixed 63-trading-day cadence.
    earnings_rows = []
    for ticker, series, jump_scale in (("NVDA", nvda, 4.5), ("AMD", amd, 4.0)):
        for step in range(40, N_DAYS, 63):
            series[step] += jump_scale * rng.standard_t(df=5) / np.sqrt(5 / 3)
            # Reported after the close of the previous session, so the reaction is `step`.
            earnings_rows.append(
                {"ticker": ticker, "date": dates[step - 1].date(), "timing": "amc"}
            )

    series_by_ticker = {
        "NVDA": nvda,
        "AMD": amd,
        "SPY": market,
        "SOXX": sector,
    }
    start_price = {"NVDA": 50.0, "AMD": 30.0, "SPY": 200.0, "SOXX": 100.0}

    for ticker, percent_returns in series_by_ticker.items():
        log_returns = percent_returns / 100.0
        prices = start_price[ticker] * np.exp(np.cumsum(log_returns))
        frame = pd.DataFrame(
            {
                "date": dates,
                "adj_close": np.round(prices, 4),
                "volume": rng.integers(1_000_000, 50_000_000, size=N_DAYS),
            }
        )
        frame.to_csv(HERE / f"{ticker}.csv", index=False)

    # A deliberately defective file for the validation tests: an unadjusted 10-for-1
    # split, a two-week hole, and a run of identical closes.
    broken = pd.read_csv(HERE / "NVDA.csv")
    broken.loc[1200:, "adj_close"] = (broken.loc[1200:, "adj_close"] / 10.0).round(4)
    broken.loc[900:902, "adj_close"] = broken.loc[899, "adj_close"]
    broken.loc[1500, "volume"] = 0
    broken = broken.drop(index=range(400, 412)).reset_index(drop=True)
    broken.to_csv(HERE / "BROKEN.csv", index=False)

    pd.DataFrame(earnings_rows).sort_values(["ticker", "date"]).to_csv(
        HERE / "earnings_calendar.csv", index=False
    )

    # A risk-free series that moves, so point-in-time lookups are testable.
    rate = 0.5 + 2.0 * np.sin(np.linspace(0, 3.0, N_DAYS)) ** 2
    pd.DataFrame({"date": dates, "rate": np.round(rate, 4)}).to_csv(
        HERE / "risk_free_3m.csv", index=False
    )

    print(f"wrote fixtures to {HERE}")
    for path in sorted(HERE.glob("*.csv")):
        print(f"  {path.name}: {len(pd.read_csv(path)):,} rows")


if __name__ == "__main__":
    build()

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


def simulate_intraday_range(
    log_closes: np.ndarray,
    daily_sigma: np.ndarray,
    rng: np.random.Generator,
    start_log_price: float,
    n_intraday_steps: int = 8,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Synthetic adjusted open/high/low consistent with a known close-to-close series.

    Builds a Brownian bridge in log-price space between each day's (synthetic) open and
    its known close, with extra intraday noise scaled to that day's realized volatility,
    so the resulting Garman-Klass proxy is genuinely informative about real variance
    rather than degenerate (which a naive open == previous close would produce every day).
    ``high``/``low`` are the max/min of the whole bridge, which by construction always
    contains the open and close -- so the range invariant holds without a separate check.
    """
    n = len(log_closes)
    opens = np.empty(n)
    highs = np.empty(n)
    lows = np.empty(n)
    prev_close = start_log_price
    grid = np.linspace(0.0, 1.0, n_intraday_steps + 1)
    interior_scale = np.sqrt(np.minimum(grid, 1.0 - grid) + 0.05)[1:-1]

    for t in range(n):
        sigma = max(float(daily_sigma[t]), 1e-6)
        open_log = prev_close + rng.normal(0.0, 0.35 * sigma)
        close_log = float(log_closes[t])
        path = open_log + grid * (close_log - open_log)
        path[1:-1] += rng.normal(0.0, 1.0, n_intraday_steps - 1) * (0.5 * sigma * interior_scale)
        opens[t] = open_log
        highs[t] = path.max()
        lows[t] = path.min()
        prev_close = close_log
    return opens, highs, lows


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
        log_prices = np.log(start_price[ticker]) + np.cumsum(log_returns)
        prices = np.exp(log_prices)

        # Rough trailing realized-vol proxy, just to give the intraday-range generator a
        # time-varying, autocorrelated scale to work from -- not meant to recover the
        # exact GARCH parameters, only to make the OHLC informative rather than degenerate.
        daily_sigma = (
            pd.Series(log_returns).abs().ewm(span=20, min_periods=1).mean().to_numpy() * 1.25
        )
        open_log, high_log, low_log = simulate_intraday_range(
            log_prices, daily_sigma, rng, start_log_price=np.log(start_price[ticker])
        )

        frame = pd.DataFrame(
            {
                "date": dates,
                "adj_close": np.round(prices, 4),
                "adj_open": np.round(np.exp(open_log), 4),
                "adj_high": np.round(np.exp(high_log), 4),
                "adj_low": np.round(np.exp(low_log), 4),
                "volume": rng.integers(1_000_000, 50_000_000, size=N_DAYS),
            }
        )
        frame.to_csv(HERE / f"{ticker}.csv", index=False)

    # A deliberately defective file for the validation tests: an unadjusted 10-for-1
    # split, a two-week hole, and a run of identical closes.
    broken = pd.read_csv(HERE / "NVDA.csv")
    # The split is applied to adj_close only, not to open/high/low -- an unadjusted-basis
    # bug, which is exactly what adjustment_consistency() in data/realized.py is for.
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

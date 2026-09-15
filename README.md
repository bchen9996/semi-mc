# semimc

Monte Carlo forecasting for U.S.-listed semiconductor stocks: distributions of future
prices and returns, probabilities of reaching price levels, and downside risk (VaR, CVaR,
drawdowns) for single names and portfolios.

**Educational project. Not investment advice.** Nothing here is a price target or a
recommendation.

---

## The point

Monte Carlo cannot tell you where a stock *will* go, only the range of where it *could* go
under a model's assumptions. So the product is not the simulation — it is **calibration**.
If a model says there is a 90% chance NVDA finishes the quarter inside a range, that should
happen about 90% of the time out of sample.

Every model is therefore judged by a walk-forward backtest against a GBM baseline, on
coverage, PIT uniformity, CRPS skill and VaR exception tests — not by how convincing its
fan chart looks. The scoreboard (`semimc backtest`) was built before the sophisticated
models, so each one has to earn its place.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or a venv and pip).

```bash
git clone <this repo> && cd semi-mc
uv sync --extra dev          # creates .venv and installs pinned dependencies
uv run semimc --help
```

Then fetch data. The first fetch pulls full daily history for 15 semiconductor names plus
SPY and SOXX — about 110,000 rows — and takes a minute or two:

```bash
uv run semimc fetch --risk-free      # prices + the historical 3-month T-bill series
uv run semimc validate               # data quality report and universe statistics
```

`--risk-free` downloads FRED's `DTB3` series so backtests use the rate that was actually in
force at each origin rather than today's. No API key is needed.

## Usage

```bash
# Forecast one ticker: fan chart, terminal distribution, risk table, convergence
uv run semimc simulate --ticker NVDA --horizon 126 --model garch_t

# Probability of finishing above a specific price
uv run semimc simulate --ticker AMD --horizon 63 --above 200

# A weighted portfolio, with each holding's contribution to CVaR
uv run semimc simulate --portfolio "NVDA=0.4,AMD=0.3,AVGO=0.3" --horizon 126

# An equal-weighted portfolio of the whole universe
uv run semimc simulate --portfolio "" --horizon 252

# The scoreboard
uv run semimc backtest --models gbm,bootstrap,garch_t --horizons 21,63

# Stress tests
uv run semimc scenario --name sector_shock_25
uv run semimc scenario --name replay_covid_2020

# A self-contained HTML report for any run
uv run semimc report --run latest
```

Development:

```bash
uv run pytest                                  # all tests, no network
uv run ruff check . && uv run ruff format .    # lint + format
```

Any config value can be overridden per command without editing the file:

```bash
uv run semimc simulate --ticker NVDA -s drift.policy=risk_free -s simulation.n_paths=50000
```

## What the backtest found

The first full run — 177 monthly origins from 2012 to 2026, 14 tickers, 2,000 paths per
origin — is worth stating up front, because it is not the result a demo would choose:

- **No model beat the GBM baseline.** GARCH-t's CRPS skill was +0.3% to +0.5% at 21–126
  days and never statistically distinguishable from the baseline. The block bootstrap was
  *worse* at every horizon, consistent with its inability to generate a day worse than the
  worst day it has seen.
- **The widths were well calibrated at short horizons** (90% intervals covered 88–91% at
  21 and 63 days) **and overconfident at a year** (50% intervals covered ~40%).
- **The centre was biased low at every horizon.** Mean PIT rose from 0.56 at 21 days to
  0.67 at 252; at one year the median realized return was +25% against a median forecast of
  +1.8%. That is a verdict on the CAPM drift assumption, not on any model — all three share
  the same drift, which is why all three show the same tilt. Semis simply beat
  `rf + beta × 5%` over this period.

The drift assumption dominated every other modelling choice. `MODEL_NOTES.md` §7 has the
detail and the trade-off: raising the drift would fix the tilt and would also have
projected absurd prices in 2021.

## Models

| name | what it adds | see |
|---|---|---|
| `gbm` | baseline: constant volatility, normal returns | MODEL_NOTES §1 |
| `bootstrap` | stationary block bootstrap of real historical days | §2 |
| `garch_t` | volatility clustering, asymmetry, fat tails | §3 |
| `earnings_jumps` | scheduled earnings gaps, overlaid on any base model | §4 |
| `factor` | market + sector factors with their own dynamics | §5 |
| `fhs` | filtered historical simulation, cross-section resampled by date | §6 |

`MODEL_NOTES.md` gives the equations, the assumptions, and — for each model — where it is
known to break.

## Output

Every command that produces output writes `runs/<timestamp>-<command>/` containing the
artifacts, a full config snapshot, the random seed, and the date range of the input data.
Given that folder, the run reproduces exactly; if it does not, something was read from
outside the config, and that is a bug.

`semimc report --run latest` turns any run into one self-contained HTML file with the
charts embedded — no external assets, so it can be emailed or opened years later.

## The earnings calendar

The earnings overlay needs real report dates and **will not invent them**. To seed the
calendar:

```bash
uv run semimc fetch --earnings      # writes config/earnings_calendar.csv
```

The provider only returns a handful of recent quarters and rarely supplies timing, so treat
the result as a starting point: review it, fix the `timing` column (`bmo` before the open,
`amc` after the close), and extend it backwards for backtesting and forwards for
forecasting. Without future-dated rows the overlay warns and injects no jumps.

## Configuration

`config/default.yaml` holds every default: the universe, benchmarks (SPY and SOXX),
horizons (21/63/126/252 trading days), 10,000 paths, a rolling five-year fit window,
CAPM-implied drift with a 5% equity risk premium, VaR at 95% and 99%, and seed 42. It is
validated with pydantic and rejects unknown keys, so a typo is an error rather than a
silently ignored setting.

## Limitations

Read these before trusting any number this produces.

- **Survivorship bias.** The universe is today's semiconductor leaders. Names that were
  prominent and then failed or were acquired are absent, so historical volatility is
  understated and drift overstated by an unknown amount.
- **The drift is an assumption, not an estimate.** Expected return comes from the
  configured policy (CAPM by default), never from the sample mean — feeding NVDA's recent
  70%-a-year history forward would project absurd prices. But nothing in the backtest
  validates CAPM either; calibration tests are far more sensitive to a distribution's width
  than to its centre.
- **Long horizons have almost no statistical power.** Monthly origins with a 252-day
  horizon overlap by eleven twelfths. Significance tests use non-overlapping origins only,
  which leaves roughly 15 observations per ticker at a year — enough to report, not enough
  to conclude. Those cells are flagged `low_power`.
- **Regime change.** Every model assumes the next period resembles a rolling five-year
  window. Export controls or an AI capex reversal would break that, silently.
- **Parameter uncertainty is not propagated.** Estimated parameters are simulated as if
  known exactly, so intervals are narrower than they should be.
- **Monte Carlo error is separate from model error.** Every `simulate` run reports a
  convergence diagnostic with standard errors for the tail statistics. That error shrinks
  with more paths; model error does not shrink at all.

## Project layout

```
config/default.yaml        every default, validated by pydantic
src/semimc/
  data/                    providers, parquet cache, validation, returns, earnings
  models/                  the six models + the shared drift machinery
  engine/                  chunked simulation, portfolio aggregation
  analytics/               VaR/CVaR, CRPS, PIT, coverage, convergence
  backtest/                walk-forward runner and scorecard
  scenarios/               stress tests and historical replays
  reporting/               matplotlib charts, self-contained HTML
  cli.py                   typer CLI
tests/                     fixtures only, no network
tasks/                     plan.md and todo.md
runs/                      one folder per run (gitignored)
```

`CLAUDE.md` records conventions and boundaries; `tasks/plan.md` records the architecture
and the decisions taken, including the ones that are reversible.

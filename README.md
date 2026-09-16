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

# Cross-validated evaluation of the regression-based drift signals (addendum Phase 4B)
uv run semimc signals --horizon 21

# Apply the fitted signal tilt on top of the configured drift policy
uv run semimc simulate --ticker NVDA --horizon 21 --tilt
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

Stated up front, because these are not the results a demo would choose. All runs use
monthly origins, 2,000 paths, and 14 tickers (ARM lacks the history).

**Only one model beat the baseline.** Over 2012–2026, GARCH-t's CRPS skill against GBM was
+0.3% to +0.5% at 21–126 days and *never* statistically distinguishable. The block
bootstrap was significantly **worse** at every horizon, consistent with its inability to
generate a day worse than the worst day it has seen.

The exception is the **earnings-jump overlay**, which over 2018–2026 beat GBM at 21 days
(CRPS skill +0.9%, Diebold-Mariano −2.93, p = 0.003) and beat plain GARCH-t by more
(DM −4.19, p < 0.0001). At 63 and 126 days it was indistinguishable. That pattern is what
you would expect: a 21-day window contains exactly one report, so the jump is a large share
of total variance; stretch the horizon and diffusion swamps it.

**The widths were well calibrated at short horizons** (90% intervals covered 88–91% at 21
and 63 days) **and overconfident at a year** (50% intervals covered ~40%).

**The drift assumption dominated every modelling choice.** Under CAPM the PIT was tilted,
not U-shaped: mean PIT rose from 0.56 at 21 days to 0.67 at 252, and at one year the median
realized return was +25% against a +1.8% median forecast. Since every model is handed the
same drift, all of them tilted identically — that is a verdict on the policy, not the
models. Backtesting the three policies head to head:

| policy | mean PIT (21d → 252d) | CRPS vs CAPM |
|---|---|---|
| `risk_free` | 0.572 → 0.715 | worse at every horizon (DM +9.2 to +12.3) |
| `capm` | 0.556 → 0.668 | — |
| `shrunk_historical` | 0.546 → 0.634 | better at every horizon (DM −4.5 to −7.3) |

`shrunk_historical` won on mean PIT, on 50% and 90% coverage, on PIT tail mass and on CRPS
at every horizon, and is now the default. **That choice was made on the same period the
scorecard reports** — selection on the test set. Semis rose sharply over this window, so a
policy leaning 25% on the realized mean was always going to look good; in a sector downturn
it will look correspondingly bad. `capm` is one flag away and is the more conservative
default. `MODEL_NOTES.md` §7 has the detail.

## What the signal evaluation found (addendum Phase 4B)

Also stated up front. `semimc signals` evaluates five candidate signals — momentum,
short-term reversal, relative strength vs. SOXX, earnings drift, and an industry-cycle
sector tilt — meant to regression-tilt the drift away from CAPM. Run 2026-09-15, real
14-name universe, 2018 onward, 5-fold walk-forward cross-validation, **pre-holdout only**
(the 24-month holdout has deliberately not been spent):

**Every signal's out-of-sample R² was negative, at both 21 and 63 days.** Predicting no
tilt at all beat every candidate, including the combined Driscoll-Kraay regression, on raw
prediction error.

**Two results were nominally significant at 63 days, not at 21**: `relative_strength`
(Clark-West t = 2.36, p = 0.009) and, on rank IC, `earnings_drift` (t = 2.34). Both are
short of the addendum's own "~3" skepticism bar for a search this size, and neither
corresponds to a positive OOS R² — distinguishable from a zero forecast is not the same
claim as economically useful.

**The combined multi-signal regression underperformed every single signal** at both
horizons, a small-sample overfitting signature. **The sector tilt found nothing** (R² ≈ 0
at both horizons) — expected, with only one sector series.

`simulate --tilt` is fully built and works — point-in-time signal store, pooled panel
regression with Driscoll-Kraay standard errors, a Fama-MacBeth robustness check, shrinkage,
a ±10% annualized cap, and a 500-set coefficient bootstrap that widens the simulated
distribution without moving its mean — but on this evidence there is no case for it by
default, and none of the commands here turn it on unless asked. `research_log.md` has the
full numbers; `MODEL_NOTES.md` §13–14 has the mechanism and the reasoning.

## Models

| name | what it adds | see |
|---|---|---|
| `gbm` | baseline: constant volatility, normal returns | MODEL_NOTES §1 |
| `bootstrap` | stationary block bootstrap of real historical days | §2 |
| `garch_t` | volatility clustering, asymmetry, fat tails | §3 |
| `earnings_jumps` | scheduled earnings gaps, overlaid on any base model | §4 |
| `factor` | market + sector factors with their own dynamics | §5 |
| `fhs` | filtered historical simulation, cross-section resampled by date | §6 |
| `merton` | unscheduled jump-diffusion, excludes earnings reaction days | §9 |
| `har` | volatility forecast from a Garman-Klass realized-variance proxy | §10 |
| `regime` | two-state Markov-switching volatility (calm / turbulent) | §11 |
| `blend` | whole simulated paths mixed from several fitted models | §12 |
| `factor_regime` | factor model with the sector factor's volatility regime-switching, shared across every ticker | §15 |

`MODEL_NOTES.md` gives the equations, the assumptions, and — for each model — where it is
known to break. §13–14 cover the Phase 4B signal store and drift tilt, which is not a
model but a modifier `simulate --tilt` applies on top of one; §15 covers the combination
ladder and `factor_regime`.

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

## Signal data (addendum Phase 4B)

The industry-cycle signal needs FRED's `IPG3344S` series, and the customer lead-lag signal
needs a hand-curated link file:

```bash
uv run semimc fetch --industry     # writes data/external/IPG3344S.csv
```

No API key needed, but read the caveat before trusting it: FRED serves the current,
*revised* vintage with no published history of earlier ones, so `release_date` in that file
is an approximation (`period + 45 days`), not a fact, and the signal carries a real
revision-bias limitation as a result — see `MODEL_NOTES.md` §13.

`data/external/supply_chain_links.csv` is hand-maintained, never inferred or scraped: each
row is one supplier→customer relationship with a source and the date it was recorded
(`as_of`), which is what makes the signal usable point-in-time despite being built from
today's knowledge of who buys from whom.

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
data/external/              versioned CSVs: industry cycle, supply-chain links
src/semimc/
  data/                    providers, parquet cache, validation, returns, earnings
  models/                  ten models + the shared drift machinery
  signals/                 point-in-time signal store, the five candidate signals
  forecasting/             panel/sector regressions, CV, tilt construction, evaluation
  engine/                  chunked simulation, portfolio aggregation
  analytics/               VaR/CVaR, CRPS, PIT, coverage, convergence
  backtest/                walk-forward runner and scorecard
  scenarios/               stress tests and historical replays
  reporting/               matplotlib charts, self-contained HTML
  cli.py                   typer CLI
tests/                     fixtures only, no network
tasks/                     plan.md, plan_addendum.md, todo.md
research_log.md             every signal specification tried, including failures
runs/                      one folder per run (gitignored)
```

`CLAUDE.md` records conventions and boundaries; `tasks/plan.md` and `tasks/plan_addendum.md`
record the architecture and the decisions taken, including the ones that are reversible.

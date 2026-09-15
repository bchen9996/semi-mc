# Implementation Plan — semimc

Source of truth: the project brief, copied into the repo root as `BRIEF.md`.
This plan records the architecture, the decisions taken, and the deviations from the brief.

## 0. Status of approval

The brief (Section 2.1) asks for plan approval before coding. The instruction that started
this session was to build it. Resolution taken: the plan is written first (this file), the
build proceeds, and every decision that would normally have been a blocking question is
listed in Section 8 below with the choice made and the reasoning, so it can be reversed
cheaply. Nothing in Section 8 is buried — all of it is repeated in the final summary.

## 1. Architecture

```
semi-mc/
├── BRIEF.md                 # the brief, verbatim, as source of truth
├── CLAUDE.md                # stack, commands, conventions, boundaries
├── README.md  MODEL_NOTES.md
├── config/default.yaml      # every default from Section 3
├── config/earnings_calendar.csv  # user-editable; template until populated
├── src/semimc/
│   ├── config.py            # pydantic v2 schema + YAML loader + override merge
│   ├── runs.py              # runs/<timestamp>/ manifests, config+seed snapshot
│   ├── data/
│   │   ├── provider.py      # DataProvider protocol; YFinance, CSV, InMemory
│   │   ├── cache.py         # parquet cache, staleness policy, metadata sidecar
│   │   ├── validation.py    # validation report (gaps/stale/outliers/short history)
│   │   ├── returns.py       # log and simple returns, calendar alignment, betas
│   │   ├── riskfree.py      # config rate (live) + historical 3M T-bill (backtest)
│   │   └── earnings.py      # earnings calendar: CSV + optional provider refresh
│   ├── models/
│   │   ├── base.py          # ReturnModel protocol, FitContext, drift re-centering
│   │   ├── drift.py         # CAPM / risk-free / shrunk-historical / manual
│   │   ├── gbm.py  bootstrap.py  garch_t.py  earnings_jumps.py
│   │   ├── factor.py        # market + sector + idiosyncratic
│   │   └── fhs.py           # filtered historical simulation (cross-sectional)
│   ├── engine/
│   │   ├── runner.py        # chunked simulation, on-the-fly path statistics
│   │   └── portfolio.py     # simple-return aggregation, rebalancing, CVaR shares
│   ├── analytics/
│   │   ├── risk.py          # VaR, CVaR, drawdown, threshold probabilities
│   │   ├── scoring.py       # CRPS, PIT, coverage, Kupiec, Christoffersen
│   │   └── convergence.py   # MC standard errors vs path count
│   ├── backtest/
│   │   ├── walkforward.py   # rolling origins, point-in-time guard, param cache
│   │   └── scorecard.py     # model x ticker x horizon table + written summary
│   ├── scenarios/stress.py  # beta-propagated shocks, vol multipliers, replays
│   ├── reporting/           # charts.py (matplotlib) + html.py (self-contained)
│   └── cli.py               # typer
└── tests/                   # fixtures only, no network
```

### Model interface (refined, as Section 5 permits)

```python
class ReturnModel(Protocol):
    name: str

    def fit(self, log_returns: pd.DataFrame, context: FitContext) -> Self: ...
    def simulate(self, n_paths: int, horizon: int, rng: np.random.Generator) -> np.ndarray: ...
```

`FitContext` is the one addition to the brief's signature. It carries the point-in-time
exogenous facts a model cannot get from its own return panel: the as-of date, benchmark
log returns, the risk-free rate in force at that origin, the earnings calendar, and
`trading_days_per_year`. Without it, CAPM drift (needs benchmark returns), the earnings
overlay (needs the calendar) and point-in-time risk-free rates cannot be computed without
smuggling in global state — which is exactly what would cause leakage. `FitContext` is
built once per forecast origin and truncated to that origin, so the leakage guard covers
it too.

### Drift policy (applies to every model — Section 8 "Always")

The configured policy produces an **annual arithmetic** expected return `mu_A`:

| policy | `mu_A` |
|---|---|
| `capm` (default) | `rf + beta_spy * ERP` |
| `risk_free` | `rf` |
| `shrunk_historical` | `w * historical_mean + (1 - w) * capm` |
| `manual` | config value per ticker |

It is converted to a **daily log** drift with `(mu_A - sigma_A**2 / 2) / 252`, using the
model's own annualized volatility for `sigma_A`. Every model demeans its training or
filtered returns and re-centers on this number, so none inherits the sample mean. This is
enforced in `models/base.py` and tested.

## 2. Phase plan and acceptance criteria

| Phase | Deliverable | Acceptance |
|---|---|---|
| 1 | Walking skeleton: NVDA/SPY/SOXX fetch+cache, GBM, `simulate` outputs, convergence, run dirs | Fan chart, terminal histogram, risk table, convergence table in `runs/<ts>/`; GBM mean/variance match closed form in tests |
| 2 | Full-universe data layer, validation report, summary stats | 15 tickers + 2 benchmarks cached; report flags gaps/outliers/short history; ARM's 2023 start handled explicitly |
| 3 | Walk-forward backtester (the scoreboard) | Coverage, PIT, CRPS + skill score, Kupiec/Christoffersen per model x ticker x horizon; look-ahead test fails on injected leakage |
| 4 | Block bootstrap, GJR-GARCH-t, earnings-jump overlay | Each scored by Phase 3; drift policy respected by all three; earnings returns stripped from base training data |
| 5 | Factor model, filtered historical simulation, portfolios | Correlation reproduced in tests; portfolio aggregation in simple returns; contribution to CVaR |
| 6 | Scenarios, HTML report, docs | `report` builds one self-contained HTML; README and MODEL_NOTES complete |

## 3. Memory and performance

Full path arrays are never materialized. `engine/runner.py` simulates in path-chunks and
accumulates terminal log returns, per-path maximum drawdown, and threshold hits on the
fly. Full price paths are retained in float32 **only** for the series being fan-charted
(10,000 x 252 float32 = 10 MB). Backtests default to 2,000 paths and cache fitted
parameters per (model, ticker, origin).

## 4. Testing

pytest, fixtures only, no network (a `network` marker is deselected by default).
Statistical tests: GBM moments vs closed form; correlation recovery; VaR/CVaR vs the
normal closed form; sample CRPS vs the normal closed-form CRPS; bootstrap draws confined
to the training window; leakage guard; split-adjustment handling; validation catches
injected gaps and outliers; CLI smoke tests on fixtures.

## 5. Risks

- **yfinance fragility.** Verified working at setup (NVDA 2024-05-01 adjusted close 82.80
  confirms the 10-for-1 split is adjusted). Raw Yahoo endpoints rate-limit (HTTP 429), so
  all access goes through yfinance with caching, plus a CSV fallback provider.
- **Earnings calendar quality.** yfinance gives only a few recent quarters. The overlay
  therefore reads a user-editable CSV; with no calendar it refuses to run rather than
  inventing dates.
- **Few independent observations at 126/252 days.** Reported sample sizes are counts of
  *non-overlapping* windows; significance tests use those only.
- **Survivorship bias.** Today's semiconductor leaders are not a 2010 portfolio. Stated in
  the report's limitations section; not fixable within this scope.

## 6. Open questions from Section 10 — answers taken

1. **Market factor SPY, sector factor SOXX.** Both are the brief's own benchmarks. SPY is
   broader than QQQ and so is a cleaner "market" — QQQ is roughly half technology and
   would absorb much of what should be the sector factor. SOXX over SMH because SMH is far
   more top-heavy in NVDA/TSM, which would make the sector factor partly an NVDA factor.
   Both are config switches.
2. **CLI + HTML is the final interface**; a dashboard stays a stretch item. The reporting
   layer returns figures and data frames rather than writing files directly, so a
   Streamlit front end could sit on top without a refactor.
3. **Flagged as questionable** — see Section 7.

## 7. Things in the brief worth flagging

- **252-day horizon significance.** With a 5-year fit window and monthly origins over the
  available history, a 252-day horizon yields on the order of 15 non-overlapping
  observations per ticker. Coverage of a 90% interval cannot be distinguished from nominal
  at that sample size. Reported, but marked low-power rather than interpreted.
- **CAPM drift with ERP 5% for semis.** Betas here run 1.5-2.2, so CAPM hands the high-beta
  names a 14-16% annual drift for a decade. That is a strong assumption, not a neutral one;
  `risk_free` is the genuinely neutral choice. Both are available and the report states
  which was used.
- **Kupiec/Christoffersen on overlapping horizons.** VaR exception tests assume independent
  exceptions. With overlapping forecast windows they are badly mis-sized, so these tests
  run on non-overlapping origins only.
- **Earnings-jump double counting.** Stripping the abnormal earnings return from training
  data (replacing it with `beta * benchmark`) removes the jump but also removes that day's
  idiosyncratic noise. The effect is small (4 days a year) and is documented in
  MODEL_NOTES.

## 8. Decisions taken without asking (all reversible)

- `pyyaml` added as a dependency: the brief mandates a YAML config but lists no YAML
  parser. No other dependency beyond Section 4 was added — the HTML report is built with
  string templates rather than jinja2 to avoid one.
- Project lives at `OneDrive/Documents/GitHub/semi-mc` as its own git repo, alongside the
  user's other project repos, rather than inside the unrelated GST research repo.
- Python pinned to 3.12 (`.python-version`): `pyarrow` has no wheels for the newest
  interpreter uv would otherwise have chosen, and building it from source failed.
- Backtest defaults: monthly origins, 2,000 paths, 5-year fit window, a minimum of 750
  observations before a ticker enters.

# Implementation Plan — Addendum

Source of truth: `BRIEF_ADDENDUM.md` (copied verbatim into the repo root), which extends
`BRIEF.md`. Where the two conflict, the addendum wins.

This file is the "assess what exists, propose how to fit the additions in" deliverable the
addendum opens with. The original `plan.md` still describes Phases 0–6 and is unchanged.

---

## 1. What already exists, and what that means for this work

The six original phases are built, backtested and committed (7 commits, 182 tests, ruff
clean). The pieces the addendum plugs into:

| existing | status | how the addendum uses it |
|---|---|---|
| `ReturnModel` / `BaseReturnModel` | stable, 6 models on it | Merton, HAR, regime, blend are four more implementations — **no interface change needed** |
| `BaseReturnModel` drift enforcement | `_fit_shape` / `_simulate_zero_mean` / `annualized_variance` | the split is exactly right for jump compensators and regime re-centring |
| `FitContext` (`context.py`) | carries point-in-time facts | **one new field**: `drift_tilt` |
| `MarketData.training_window` / `.context` | the only point-in-time choke points | signal store reads through the same `as_of` discipline |
| `run_walk_forward` | per-origin, per-ticker, one-fit-many-horizons | **one new hook**: a tilt provider called once per origin |
| `assert_point_in_time` | guards every fit | reused verbatim for signals |
| `analytics/scoring.py` | CRPS, PIT, coverage, Kupiec, Christoffersen, DM | extended with pinball, QLIKE, Clark-West, OOS R², Newey-West IC |
| `data/provider.py` + `cache.py` | **adj_close + volume only** | **must be extended to OHLC** — see risk 1 below |

**Nothing working gets rewritten.** The only edits to existing files are additive: one
`FitContext` field, one `simulate` term, four registry entries, new config blocks, an
optional backtester argument, and the OHLC columns.

### The one structural change: OHLC

HAR needs an intraday range estimator, and the cache stores only adjusted close. yfinance
already returns adjusted OHLC in the same `auto_adjust=True` call the provider makes, so
this is a schema widening, not a new data source. Cost: a one-time full refetch. Handled
with a `schema_version` in the cache metadata so an old cache is detected and refreshed
rather than silently read as missing columns.

Consistency check the addendum asks for: with `auto_adjust=True` every OHLC field is
scaled by the same factor, so the invariants `low <= min(open, close)` and
`high >= max(open, close)` must hold on every row. `validate` will assert them and report
violations rather than repairing them.

---

## 2. Phase 4 additions

### 4.7 Merton jump-diffusion (`models/merton.py`)

Per day: `r = drift + sigma_d * Z + (sum of N ~ Poisson(lambda_d) jumps Y ~ N(mu_J, sigma_J^2)) - lambda_d * mu_J`.

**The compensator falls out of the existing base class exactly.** The base class produces
`annual log drift = mu_A - V/2` where `V = annualized_variance()`. For
`E[S_T/S_0] = exp(mu_A * T)` we need `V = 2K`, where `K = log E[exp(shocks)]`:

```
K = sigma_d^2/2 + lambda*(k - mu_J),   k = exp(mu_J + sigma_J^2/2) - 1
=> V = sigma_d^2 + 2*lambda*(k - mu_J)
=> net log drift = mu_A - sigma_d^2/2 - lambda*k
```

which is the textbook Merton drift `mu - sigma^2/2 - lambda*k`. So returning that `V` from
`annualized_variance` gives the compensator with **no change to `base.py`**. Documented in
MODEL_NOTES because `V` is then the Ito-correction variance, not the log-return variance.

- **Estimation**: MLE on a Poisson mixture of normals truncated at `max_jumps_per_day = 3`,
  L-BFGS-B with bounds, multiple starting values, best log-likelihood wins.
- **Cross-check**: threshold estimator flagging `|r| > c * sigma_GARCH,t`.
- **Earnings**: reaction days are excluded from estimation so the overlay can wrap Merton
  without double counting.
- **Stability**: `semimc diagnose --model merton` refits on rolling sub-windows and reports
  the coefficient of variation of each parameter across refits, because the
  diffusion/jump split is weakly identified on daily data.

### 4.8 HAR volatility (`models/har.py`)

- **Proxy**: `RV_t = r_overnight^2 + GarmanKlass_t`, where
  `GK = 0.5*ln(H/L)^2 - (2 ln2 - 1)*ln(C/O)^2`. Rogers-Satchell available by config.
- **Earnings days**: proxy replaced by the trailing 22-day median, so earnings variance is
  not double counted with the overlay.
- **Regression**: `log RV_avg(t+1..t+h) ~ log RV_d + log RV_w + log RV_m`, a **separate
  direct fit per horizon**. Back-transform with the lognormal correction
  `exp(sigma_resid^2 / 2)`.
- **Term structure**: direct forecasts give cumulative variance `C(h) = h * Vbar(h)`; the
  per-step variance between two configured horizons is the forward difference
  `(C(h_k) - C(h_{k-1})) / (h_k - h_{k-1})`, floored at a small positive value. This
  reproduces every direct forecast exactly and gives the simulator a per-step path.
- **Shocks**: resampled standardized residuals by default (keeps real tails), Student-t by
  config.
- **Pooled vs per-ticker**: both fitted; the config picks, the backtest decides.
- **Direct scoring**: QLIKE against the realized proxy, alongside GARCH, in
  `semimc diagnose --volatility`.

### 4.9 Regime switching (`models/regime.py`)

- `MarkovRegression(k_regimes=2, switching_variance=True)`, `search_reps` random starts,
  regimes **ordered by variance** so regime 0 is always the calm one (kills label
  switching). Non-convergent fits are recorded and the cell falls back to GARCH with a
  logged warning.
- **Start state**: `filtered_marginal_probabilities[-1]`. Never smoothed. A dedicated test
  appends post-origin data and asserts the filtered probability at the origin is unchanged.
- **Drift**: default is **variance-only** (`switching_mean: false`), which keeps the drift
  contract exact. With `switching_mean: true` the regime means are re-centred so the
  *stationary* average is zero — long-run neutral, not finite-horizon neutral — and that
  difference is tested explicitly rather than folded into the shared drift test.
- **Daily vs weekly**: if either regime's expected duration is below
  `min_expected_duration_days`, refit on weekly returns and map back by dividing weekly
  variance by 5 and holding the regime fixed across each 5-day block. Which route was
  taken is recorded in `describe()`.

### 4.10 Blending (`models/blend.py`)

- Whole **paths** are drawn from each component in proportion to its weight, so each path
  keeps its model's internal structure.
- Implemented by overriding `simulate` and delegating to fitted components, so each path
  carries its own component's drift. `annualized_variance` is the weighted average, which
  makes `daily_log_drift` exactly the path-average drift — the drift contract still holds
  identically, and that is asserted.
- **Weights**: equal by default; CRPS-optimized weights (non-negative, sum to 1, SLSQP)
  estimated **only from origins strictly before the current one**.
- **Coverage**: the scorecard flags blends specifically, because mixing distributions
  widens intervals and the failure mode is over-coverage.

---

## 3. Phase 4B: signals and regression drift

```
src/semimc/signals/
  store.py      SignalStore, SignalFrame, release-date enforcement, as_of()
  build.py      momentum, reversal, relative strength, earnings drift, lead-lag
  external.py   versioned CSV loaders for data/external/
src/semimc/forecasting/
  panel.py      pooled panel regression, Driscoll-Kraay, Fama-MacBeth
  sector.py     sector-factor tilt regression
  tilt.py       TiltProvider: shrinkage, cap, coefficient bootstrap, horizon reversion
  cv.py         date-grouped, gapped cross-validation splitter
  evaluate.py   OOS R^2, Clark-West, IC + Newey-West, pinball, stability
data/external/  versioned CSVs with period, value, release_date, source_url
research_log.md every specification tried, including failures
```

### Point-in-time rule, stated once

Every signal row carries `release_date`. At origin `t` a model may use a row **iff**
`release_date <= t`, and only the latest such row per ticker. Training rows additionally
require a **fully realized target**: a row dated `d` with an `H`-trading-day forward return
is admissible only if `d + H` trading days `<= t`. Both rules are enforced in
`SignalStore.as_of` and `panel.training_rows`, and both have tests that fail on violation.

### Drift tilt plumbing

`FitContext` gains `drift_tilt: DriftTilt | None`:

```python
@dataclass(frozen=True)
class DriftTilt:
    tickers: list[str]
    point: np.ndarray  # (n_tickers,)   annualized arithmetic tilt, already shrunk+capped
    draws: np.ndarray  # (n_sets, n_tickers) coefficient-uncertainty sets, mean == point
    horizon_days: int  # tilt applies for this many steps, then reverts to CAPM
```

Applied in `BaseReturnModel.simulate`, after the base drift:

- `daily_log_drift` includes `point`, so it stays "the expected drift" and the existing
  drift test keeps working unchanged.
- `simulate` adds each path's **deviation** `draws[i] - point`, masked to the first
  `horizon_days` steps. Because `draws` is demeaned to `point` at construction, the
  deviation sums to zero exactly — so chunking still cannot change the answer, which is the
  invariant the original build got wrong once and now guards.
- The tilt is an **arithmetic** annual shift, so it needs no change to the Ito correction:
  `mu_A -> mu_A + tau` moves the log drift by exactly `tau / 252`.

### Regression design

- **Sector tilt**: time-series regression of the forward sector-factor return (SOXX
  residualized on SPY) on industry-cycle signals. One series, so power is low; reported
  with that caveat attached rather than buried.
- **Relative tilt**: pooled panel of returns net of market and sector exposure on
  cross-sectionally standardized stock-level signals. **No fixed effects** — they would
  reintroduce each stock's historical mean, the exact leak the drift policy bans.
- **Combined**: `drift = policy drift + beta_sector * sector_tilt + relative_tilt`.
- **Standard errors**: Driscoll-Kraay (`cov_type="hac-groupsum"`), `maxlags = ceil(H/21)`
  for `H`-day overlapping returns sampled monthly. Fama-MacBeth as a robustness check.
- **Models, simple to complex**: zero-signal baseline; one regression per signal; simple
  average of single-signal forecasts; then ridge/lasso; then `QuantReg` at 5/25/50/75/95
  scored by pinball loss.
- **Cross-validation**: split on **unique dates**, with a gap of at least `H` sampling
  periods between train and test folds.

### Holdout

The last 24 months are frozen. Selection, `lambda`, and every specification choice happen
on pre-holdout origins. `semimc signals` refuses to read holdout dates unless `--holdout`
is passed; the holdout result is written once, appended to `research_log.md`, and reported
whatever it says.

---

## 4. Phase 5 additions

- Regime lives on the **sector factor**, so every stock shares one regime path and the
  portfolio turns turbulent together instead of averaging out.
- Tilts feed the multi-asset path, and blending plus the scorecard extend to portfolios.

---

## 5. Answers to the addendum's open questions

### "Which industry series can actually be collected with reliable release dates?"

Honestly: **not the two the addendum names, without scraping.**

- **TSMC monthly revenue** — published ~10th of the following month on TSMC's IR site.
  No free API. Requires scraping, which is an explicit ask-first boundary.
- **SIA global semiconductor sales** — published ~first week of the following month as a
  press release, and it is a **3-month moving average**, which smooths and lags the signal
  further. No free API. Requires scraping.
- **FRED** is already an approved, key-free data path in this project (`riskfree.py` reads
  `fredgraph.csv`). `IPG3344S` (industrial production, semiconductor and other electronic
  component manufacturing) is monthly, goes back to 1972, and needs no scraping. Its
  weakness is that the free CSV endpoint serves **revised** values with no vintage, so
  release dates must be approximated by a conservative publication lag, and revision bias
  must be flagged. That is the exact limitation the addendum anticipates.

This is the first blocking question.

### "Is a 24-month holdout long enough given how many signals will be tested?"

**No, not for the longer horizon.** 24 months of month-end origins is 24 origins. After
enforcing non-overlap that leaves roughly 24 usable observations at 21 days but only about
**8** at 63 days — against 5 candidate signals plus combination methods plus `lambda`. At
8 observations, nothing short of an enormous effect is detectable, and the holdout would
mostly measure noise.

Three ways out, none free: lengthen the holdout (costs training data), report the holdout
only at 21 days (honest, narrower claim), or sample origins more frequently inside the
holdout (more observations, but overlapping and therefore not independent). This is the
second blocking question because it changes a Section 3 default.

### "Does anything look over-scoped relative to what's already built?"

Yes, three things, flagged rather than silently dropped:

1. **Signal 5 (customer lead-lag) needs prices outside the universe.** Major customers are
   AAPL, MSFT, GOOGL, AMZN, META, TSLA, DELL, HPQ, CSCO — none currently fetched. Cheap to
   add, but it widens the data footprint and the survivorship-bias problem (the customer
   list is today's, applied to history). Third blocking question.
2. **Blending multiplies backtest cost.** Scoring a blend requires every component at every
   origin. With 10 models × 177 origins × 14 tickers the full grid is hours, not minutes.
   Mitigation: blend only over a named subset, and reuse the per-origin component draws
   already computed rather than re-simulating.
3. **Quantile regression on 5 quantiles × 5 signals × 2 horizons × 177 origins** is the
   single most expensive piece of Phase 4B for the least expected return. It is built and
   scored, but it runs on a coarser origin grid by default.

---

## 6. Risks specific to this addendum

- **`statsmodels.api` cannot be imported on this machine.** Windows Application Control
  blocks its compiled `_qn` and `_kalman_filter` DLLs. Every needed submodule
  (`regression.linear_model`, `regression.quantile_regression`,
  `tsa.regime_switching.markov_regression`, `stats.sandwich_covariance`) imports fine
  individually. **Rule: never `import statsmodels.api`.** Enforced by a test.
- **Merton is weakly identified.** A bigger jump component trades off against smaller
  diffusion volatility at nearly equal likelihood. Reported, not hidden.
- **Signals will look better in-sample than they are.** Five signals, two horizons, several
  combination rules and a shrinkage parameter is a large search. Everything goes in
  `research_log.md` including failures, and t-statistics below ~3 are treated as noise.
- **Revision bias** on any macro series pulled from a revised-only source.
- **The customer list is survivorship-biased**, being today's relationships applied to
  history. `supply_chain_links.csv` carries an `as_of` per row so the bias is at least
  visible.

# semimc checklist

Kept current so work survives across sessions. `[x]` means done *and* covered by a test
or a produced artifact.

## Phase 0 — setup
- [x] uv environment, pinned deps (`uv.lock`), Python 3.12
- [x] Confirm yfinance works (NVDA/SPY/SOXX, split-adjusted)
- [x] `tasks/plan.md`
- [x] `CLAUDE.md`, `config/default.yaml`, pydantic config schema
- [x] Run-directory management (`runs/<timestamp>/`, config + seed snapshot)

## Phase 1 — walking skeleton
- [x] `DataProvider` protocol + yfinance provider + CSV/in-memory providers
- [x] Parquet cache with staleness check and metadata sidecar
- [x] Log returns, calendar alignment
- [x] Drift policies: CAPM (default), risk-free, shrunk historical, manual
- [x] GBM model with trailing realized vol (1y window) and EWMA option
- [x] Chunked simulation engine, on-the-fly drawdowns, float32 support
- [x] Analytics: percentiles, VaR/CVaR, threshold probabilities, drawdowns
- [x] Convergence diagnostic with Monte Carlo standard errors
- [x] Charts: fan chart, terminal histogram; risk table
- [x] `semimc simulate` end to end

## Phase 2 — full-universe data layer
- [x] Fetch/cache the full 15-name universe + SOXX + SPY, full history
- [x] Validation report: failures, gaps, stale/zero-volume days, outliers, short history
- [x] Common trading calendar alignment, unequal histories handled explicitly
- [x] Summary table (ann. vol, skew, excess kurtosis, betas, worst days) + correlation heatmap
- [x] `semimc fetch` and `semimc validate`

## Phase 3 — walk-forward backtester
- [x] Rolling monthly origins, tickers enter on sufficient history
- [x] Point-in-time guard + test that fails on injected leakage
- [x] Coverage (50%/90%), PIT histograms
- [x] Sample CRPS + CRPS skill score vs GBM
- [x] Kupiec POF and Christoffersen independence, on non-overlapping origins only
- [x] Non-overlapping sample sizes reported per cell
- [x] Fitted-parameter cache per origin
- [x] Scorecard output + written summary; GBM baseline established

## Phase 4 — realistic single-stock dynamics
- [x] Stationary block bootstrap with `arch` optimal block length
- [x] GJR-GARCH(1,1)-t via `arch`, x100 rescaling, conditional-vol start, mean reversion
- [x] Earnings calendar loader (CSV, editable; provider refresh optional)
- [x] Earnings-jump overlay wrapping any base model; BMO/AMC reaction-day mapping
- [x] Abnormal earnings returns stripped from base training data
- [x] All three respect the configured drift policy (tested)

## Phase 5 — dependence and portfolios
- [x] Factor model: SPY market, SOXX residualized on SPY, idiosyncratic
- [x] Filtered historical simulation: by-date cross-sectional residual bootstrap
- [x] Portfolio simulation, simple-return aggregation, optional rebalancing
- [x] Portfolio VaR/CVaR, drawdown distribution, contribution to CVaR
- [x] `semimc simulate --portfolio`

## Phase 6 — scenarios, reporting, polish
- [x] Sector shock through betas, volatility multiplier, per-ticker custom shocks
- [x] Historical replays scaled to current betas
- [x] `semimc report --run latest` self-contained HTML with limitations section
- [x] README, MODEL_NOTES, CLAUDE.md complete
- [x] `uv run pytest` green, `uv run ruff check .` clean

## Not built (stretch — only on request)
- [ ] Signal-conditioned drift/volatility
- [ ] Markov regime switching
- [ ] Options-implied vol calibration
- [ ] Streamlit dashboard

## Known gaps (deliberate, with reasons)

- [ ] **Cross-run fit caching.** Fitted parameters are cached in memory within a run (one
      fit serves every horizon, roughly a 4x saving) and every fit is recorded in the run
      manifest, but nothing is reloaded across runs. Reconstructing a GARCH from stored
      parameters risks a silent mismatch between the stored and rebuilt model, which is a
      worse failure than a slow rerun. The full 15-ticker, 3-model, 4-horizon backtest
      takes about 3.5 minutes, so the payoff is small.
- [ ] **Cross-sectional dependence in significance tests.** Non-overlapping origins fix
      dependence through time, but pooled tickers co-move (pairwise correlations ~0.5-0.7),
      so Diebold-Mariano and the PIT chi-squared remain oversized. Disclosed in every
      written summary rather than corrected; a block bootstrap over origins would be the
      fix.
- [ ] **ARM truncates the common calendar.** Whole-universe models using complete-case
      alignment get a ~3-year window instead of 5. A warning names the binding ticker.
      Dropping ARM or allowing ragged panels per model would both be reasonable; neither
      is done without a decision from the owner.
- [ ] **`earnings_jumps` is unverified on real data.** It is fully tested against fixtures
      (jump timing, variance contribution, no drift leakage, training-data stripping), but
      seeding a real calendar needs `lxml`, which yfinance uses to scrape earnings dates
      and which is not in the brief's Section 4 dependency list. The command now fails with
      an actionable message instead of a cryptic one. Two ways forward, both needing a
      decision: add `lxml`, or hand-fill `config/earnings_calendar.csv`. No dates were
      invented to work around this.

# semimc checklist

Kept current so work survives across sessions. `[x]` means done *and* covered by a test
or a produced artifact.

## Phase 0 — setup
- [x] uv environment, pinned deps (`uv.lock`), Python 3.12
- [x] Confirm yfinance works (NVDA/SPY/SOXX, split-adjusted)
- [x] `tasks/plan.md`
- [ ] `CLAUDE.md`, `config/default.yaml`, pydantic config schema
- [ ] Run-directory management (`runs/<timestamp>/`, config + seed snapshot)

## Phase 1 — walking skeleton
- [ ] `DataProvider` protocol + yfinance provider + CSV/in-memory providers
- [ ] Parquet cache with staleness check and metadata sidecar
- [ ] Log returns, calendar alignment
- [ ] Drift policies: CAPM (default), risk-free, shrunk historical, manual
- [ ] GBM model with trailing realized vol (1y window) and EWMA option
- [ ] Chunked simulation engine, on-the-fly drawdowns, float32 support
- [ ] Analytics: percentiles, VaR/CVaR, threshold probabilities, drawdowns
- [ ] Convergence diagnostic with Monte Carlo standard errors
- [ ] Charts: fan chart, terminal histogram; risk table
- [ ] `semimc simulate` end to end

## Phase 2 — full-universe data layer
- [ ] Fetch/cache the full 15-name universe + SOXX + SPY, full history
- [ ] Validation report: failures, gaps, stale/zero-volume days, outliers, short history
- [ ] Common trading calendar alignment, unequal histories handled explicitly
- [ ] Summary table (ann. vol, skew, excess kurtosis, betas, worst days) + correlation heatmap
- [ ] `semimc fetch` and `semimc validate`

## Phase 3 — walk-forward backtester
- [ ] Rolling monthly origins, tickers enter on sufficient history
- [ ] Point-in-time guard + test that fails on injected leakage
- [ ] Coverage (50%/90%), PIT histograms
- [ ] Sample CRPS + CRPS skill score vs GBM
- [ ] Kupiec POF and Christoffersen independence, on non-overlapping origins only
- [ ] Non-overlapping sample sizes reported per cell
- [ ] Fitted-parameter cache per origin
- [ ] Scorecard output + written summary; GBM baseline established

## Phase 4 — realistic single-stock dynamics
- [ ] Stationary block bootstrap with `arch` optimal block length
- [ ] GJR-GARCH(1,1)-t via `arch`, x100 rescaling, conditional-vol start, mean reversion
- [ ] Earnings calendar loader (CSV, editable; provider refresh optional)
- [ ] Earnings-jump overlay wrapping any base model; BMO/AMC reaction-day mapping
- [ ] Abnormal earnings returns stripped from base training data
- [ ] All three respect the configured drift policy (tested)

## Phase 5 — dependence and portfolios
- [ ] Factor model: SPY market, SOXX residualized on SPY, idiosyncratic
- [ ] Filtered historical simulation: by-date cross-sectional residual bootstrap
- [ ] Portfolio simulation, simple-return aggregation, optional rebalancing
- [ ] Portfolio VaR/CVaR, drawdown distribution, contribution to CVaR
- [ ] `semimc simulate --portfolio`

## Phase 6 — scenarios, reporting, polish
- [ ] Sector shock through betas, volatility multiplier, per-ticker custom shocks
- [ ] Historical replays scaled to current betas
- [ ] `semimc report --run latest` self-contained HTML with limitations section
- [ ] README, MODEL_NOTES, CLAUDE.md complete
- [ ] `uv run pytest` green, `uv run ruff check .` clean

## Not built (stretch — only on request)
- [ ] Signal-conditioned drift/volatility
- [ ] Markov regime switching
- [ ] Options-implied vol calibration
- [ ] Streamlit dashboard

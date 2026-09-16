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

## Addendum Phase 4 — more single-stock dynamics
- [x] Merton jump-diffusion: MLE on a truncated Poisson mixture, compensator falls out of
      the base class's existing `annualized_variance` hook with no interface change
- [x] Threshold jump estimate as an MLE cross-check (`threshold_jump_estimate`)
- [x] Refit-stability diagnostic across rolling sub-windows (`refit_stability`)
- [x] HAR volatility: Garman-Klass/Rogers-Satchell proxy, direct per-horizon regressions,
      log-bias (Duan smearing) correction, earnings-day variance replaced pre-fit
- [x] QLIKE scoring for volatility forecasts (`data/realized.py:qlike`)
- [x] Regime switching: two-state `MarkovRegression`, filtered (never smoothed) start
      state, variance-only by default, daily→weekly fallback below a duration threshold
- [x] Blending: whole simulated paths per component, drift contract exact by construction,
      CRPS-optimized weights via pooled-mixture-sample SLSQP (not a linear-loss shortcut,
      which would always corner-solve to the single best component)
- [x] Adjusted OHLC added to the provider/cache schema (`schema_version` bump forces a
      clean refetch of any pre-addendum cache)
- [x] Drift tilt plumbing (`context.DriftTilt`, `BaseReturnModel._path_drift_tilt`):
      per-path deviation, expires after its fitted horizon, demeaned bootstrap draws widen
      dispersion without moving the mean

## Addendum Phase 4B — signals and the regression-based drift tilt
- [x] Point-in-time signal store (`signals/external.py`) with release-date enforcement
- [x] Four ticker-level signals: momentum, reversal, relative strength, earnings drift
- [x] Customer lead-lag signal + hand-curated `supply_chain_links.csv` (21 links, `as_of`
      dated so the signal is correctly inert before it in any backtest)
- [x] Industry-cycle signal from FRED `IPG3344S` (TSMC/SIA both need scraping — an
      ask-first boundary — so neither was used; revision bias flagged, not corrected)
- [x] Pooled panel regression with Driscoll-Kraay SEs; Fama-MacBeth robustness check
- [x] Sector-tilt regression (industry cycle → SOXX residualized on SPY)
- [x] Date-grouped, gapped cross-validation (`forecasting/cv.py`)
- [x] Shrinkage + cap + date-block coefficient bootstrap → `DriftTilt`
      (`forecasting/tilt.py`)
- [x] Evaluation: OOS R² (Campbell-Thompson), Clark-West, rank IC with Newey-West,
      pinball loss, sign-flip stability check
- [x] `semimc signals` (cross-validated pre-holdout evaluation, `--holdout` for the
      one-time holdout) and `semimc simulate --tilt`
- [x] `semimc fetch --industry`
- [x] Real evaluation run on the 14-ticker universe, pre-holdout only — see
      `research_log.md`. Every signal's OOS R² was negative at 21d and 63d; nothing
      cleared the addendum's own significance bar. `--tilt` stays off by default.
- [ ] **Holdout not yet spent** against the real universe. `semimc signals --holdout` is
      built and tested against fixtures; running it for real is a research decision, not a
      build one, and is left to the project owner.
- [ ] **Simple-average combination model**, which the addendum suggests trying before a
      full multi-signal regression ("often more robust than one regression containing
      every signal"). `semimc signals` currently reports only the Driscoll-Kraay combined
      fit, which underperformed every single signal on the real data — exactly the
      overfitting pattern the simple average is meant to avoid. Worth adding before
      spending the holdout.
- [ ] Ridge/lasso combination models (scikit-learn is a declared dependency; not wired up)
- [ ] Quantile regression (`QuantReg`) at 5/25/50/75/95, scored by pinball loss against the
      simulation's own quantiles
- [ ] Shared sector-level regime switching feeding the multi-asset/portfolio simulation
      (Phase 5 addition — regime switching itself is built per-ticker; making it shared
      across the sector factor for portfolio runs is not)
- [ ] Rolling-coefficient stability charts and a 2022-downturn-vs-rally split report
      (`evaluate.py:subset_by_date_range` and `sign_flip_rate` exist; not wired into a
      report yet)

## Not built (stretch — only on request)
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
- [x] **`earnings_jumps` verified on real data.** `lxml` added; `fetch --earnings` seeded
      712 real report dates (15 tickers, 2012-2026, timing populated: 569 amc / 139 bmo /
      4 unknown, plus one future date per ticker). Backtested 2018-2026: the only model
      that beats the baseline, and only at 21 days. The calendar is tracked in git.
- [x] **Drift policy decided by backtest.** `shrunk_historical` beat `capm` and
      `risk_free` on every calibration metric at every horizon and is now the default,
      flagged as selection on the test set.

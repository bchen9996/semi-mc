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
- [x] **Simple-average combination model** (`forecasting/combine.py:fit_simple_average`),
      ridge/lasso (`fit_ridge_or_lasso`, alpha chosen by pre-holdout CV), and quantile
      regression (`fit_quantile_regressions`, `QuantReg` at 5/25/50/75/95, cross-validated
      pinball loss). All four wired into `semimc signals` as `CandidateSpec`s scored
      through one shared CV harness (`evaluate_candidate`). Built and evaluated pre-holdout
      *before* the holdout was spent, per the addendum's own ordering — see
      `research_log.md`'s 2026-09-16 entries. `simple_average` beat `combined` by a wide
      margin pre-holdout at both horizons and was, briefly, the first candidate in this
      project's search to clear every significance bar at once (63d, pre-holdout only).
- [x] **Holdout spent** against the real universe, both cells, 2026-09-16. 21d (headline,
      spec: `relative_strength`): OOS R² -0.0057, Clark-West t -0.69. 63d (low-power,
      spec: `simple_average`): OOS R² -0.0058, Clark-West t -0.94. Both null; the
      promising 63d pre-holdout `simple_average` result did not replicate. `--tilt` stays
      off by default. **Do not re-run `--holdout` against this universe/window** — see
      `research_log.md`.
- [x] **Shared sector-level regime switching** for the portfolio simulation
      (`models/factor_regime.py:FactorRegimeModel`, model name `factor_regime`). Reuses
      `FactorModel`'s own decomposition — every ticker's shock already shares the same
      simulated sector-factor path via `loadings @ factor_shocks` — so fitting the sector
      factor's volatility with `RegimeSwitchingModel` instead of GARCH makes a sector-wide
      regime switch appear in every ticker's return on the same simulated day, with no
      change to `RegimeSwitchingModel` itself (which remains per-ticker-independent, correct
      for the single-ticker `regime` model). See MODEL_NOTES §15.
- [x] **Rolling-coefficient stability charts and the 2022-downturn-vs-rally split**
      (`forecasting/stability.py`: `fama_macbeth_coefficient_path`,
      `rolling_coefficient_stability`, `market_environment_split`;
      `reporting/charts.py:rolling_coefficient_chart`). Wired into `semimc signals`,
      printed, and written to the run directory (`rolling_coefficients.csv` + `.png`).

## Not built (stretch — only on request)
- [x] **Options-implied vol calibration.** Owner decision 2026-09-16: built as a
      **live-only** override, not part of the backtester (no free historical option data
      exists — the same reason the addendum declined Heston). `data/options.py`: current
      option chain via yfinance, implied vols recomputed from bid/ask via our own
      Black-Scholes inversion (yfinance's own `impliedVolatility` field was confirmed
      unreliable on illiquid strikes during development — 1300%/870% "vols" on
      `openInterest = 1` contracts), OTM/liquid-only, variance-interpolated across the
      term structure to the requested horizon. `GBMModel.implied_annual_vol` is a
      per-ticker override set before `.fit()`, keeping `_fit_shape` itself network-free.
      `simulate --implied-vol`, requires `--model gbm` and a single `--ticker`. See
      MODEL_NOTES §16.
- [x] **Streamlit dashboard.** Owner decision 2026-09-16: build the fuller of the two
      scopes discussed -- forms that drive and run `fetch`/`validate`/`simulate`/
      `backtest`/`signals`/`scenario` from the browser, not just a viewer for existing
      runs. `dashboard/jobs.py` (no Streamlit import, covered by the normal test suite)
      launches each command as a subprocess exactly as a terminal user would, so `cli.py`
      stays the one implementation; `dashboard/app.py` is the Streamlit script itself,
      one page per command plus a Runs & Reports browser that embeds each run's
      `report.html`. `signals --holdout` is deliberately not exposed -- see MODEL_NOTES
      §17. `uv run --extra dashboard streamlit run src/semimc/dashboard/app.py`.

## Known gaps (deliberate, with reasons)

- [ ] **Cross-run fit caching.** Fitted parameters are cached in memory within a run (one
      fit serves every horizon, roughly a 4x saving) and every fit is recorded in the run
      manifest, but nothing is reloaded across runs. Reconstructing a GARCH from stored
      parameters risks a silent mismatch between the stored and rebuilt model, which is a
      worse failure than a slow rerun. The full 15-ticker, 3-model, 4-horizon backtest
      takes about 3.5 minutes, so the payoff is small.
- [x] **Cross-sectional dependence in significance tests.** Owner decision 2026-09-16:
      build the block-bootstrap-over-origins fix rather than continue only disclosing it.
      `analytics/scoring.py`: `block_bootstrap_origins`, `diebold_mariano_bootstrap`,
      `pit_bias_bootstrap_test` (the PIT correction tests mean-PIT and tail-mass bias
      against a bootstrap SE, not a dependence-corrected chi-squared -- that would need a
      Rao-Scott-style correction and the full multinomial covariance structure, unreliable
      to estimate from a few dozen origins). Wired into `dm_table`/`pit_tables`/
      `written_summary`, controlled by `backtest.n_dependence_bootstraps` (default 2000),
      seeded from the run's own seed for reproducibility. See MODEL_NOTES §7.
- [x] **ARM truncates the common calendar.** Owner decision 2026-09-16: drop ARM from
      `universe.groups.compute_fabless` (`config/default.yaml`) rather than build ragged-
      panel support. The default universe is now 14 tickers and whole-universe models get
      the full ~5-year window without a config override.
- [x] **`earnings_jumps` verified on real data.** `lxml` added; `fetch --earnings` seeded
      712 real report dates (15 tickers, 2012-2026, timing populated: 569 amc / 139 bmo /
      4 unknown, plus one future date per ticker). Backtested 2018-2026: the only model
      that beats the baseline, and only at 21 days. The calendar is tracked in git.
- [x] **Drift policy decided by backtest.** `shrunk_historical` beat `capm` and
      `risk_free` on every calibration metric at every horizon and is now the default,
      flagged as selection on the test set.

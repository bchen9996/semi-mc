# Brief Addendum: Additional Models and Regression-Based Signals

This addendum extends `semis_monte_carlo_brief.md`. Read both before doing anything. Everything in the original still applies, including the plan-first workflow, phase-by-phase reviews, and all boundaries. Where the two conflict, this addendum wins.

If some phases are already built, start by assessing what exists and propose how to fit these additions in without redoing working code. Either way, propose an updated plan and wait for my approval before writing code.

## Purpose

The original brief makes the *spread* of simulated outcomes realistic. This addendum does two things:

1. It refines that spread with unscheduled jumps, better volatility forecasts, and industry-cycle regimes.
2. It adds a disciplined way to forecast *direction*, using regression to tilt the drift away from its CAPM default.

Return predictability is weak and easy to fake. Every new model must beat the existing ones on the walk-forward scoreboard. Signals must also survive a final holdout period that nothing gets tuned on.

## Phase 4 additions: more dynamics

Add each of the following as its own model, scored by the Phase 3 backtester against GBM and the existing Phase 4 models. All of them follow the drift policy.

### Merton jump-diffusion
- **What it adds:** Brownian diffusion plus Poisson-timed jumps with normally distributed log sizes. This covers *unscheduled* gaps like export-control headlines or customer guidance cuts.
- **Relationship to the earnings overlay:** scheduled earnings gaps stay with the overlay. Exclude earnings reaction days when estimating jumps so the two models can be combined without double counting.
- **Drift:** include the jump compensator (−λk, where k = E[e^Y − 1] for log jump size Y) so the configured drift is preserved.
- **Estimation:**
  - Fit by maximum likelihood on a truncated Poisson mixture of normals, with parameter bounds and multiple starting values.
  - Cross-check against a simple threshold estimate that flags days whose returns exceed a multiple of GARCH conditional volatility.
  - On daily data, a larger jump component can be offset by a smaller diffusion volatility (and vice versa) with similar fit, so the estimates are unstable. Report how the parameters move across refits.
- **Expectation:** GJR-GARCH-t may already capture most of what jumps add. The backtest decides whether Merton earns a place.

### HAR volatility forecasts
- **Variance proxy:** build a daily variance proxy from adjusted OHLC prices. Use the overnight squared log return plus an intraday range estimator (Garman-Klass or Rogers-Satchell), because intraday ranges alone miss overnight gaps. Confirm that open, high, and low are adjusted consistently with close.
- **Regression:**
  - Regress log future average variance on log daily, weekly (5-day), and monthly (22-day) average variance.
  - Fit a separate direct forecast for each horizon, and apply a log-bias correction when converting back.
  - Compare pooled estimation across tickers with per-ticker fits.
- **Earnings days:** replace earnings reaction days' variance with a non-earnings estimate (e.g., the trailing 22-day median) so earnings variance isn't double counted with the overlay.
- **Use in simulation:** scale standardized bootstrap or Student-t shocks to the HAR-forecast volatility.
- **Scoring:** besides the backtest, score the volatility forecasts directly against the realized variance proxy using QLIKE loss, alongside GARCH's volatility forecasts.

### Regime switching (moved up from Stretch)
- **Model:** a two-regime (calm vs. turbulent) Markov-switching model with switching variance, using statsmodels `MarkovRegression`. Report the transition matrix and expected regime durations. If daily fits produce regimes too short-lived to reflect the industry cycle, try weekly returns and document how you map them to daily steps.
- **Starting state:** start each simulation from the **filtered** regime probabilities at the origin. Never use smoothed probabilities, because they incorporate future data.
- **Drift:** regime-specific means are allowed only as deviations around the configured drift. Re-center them so the long-run (stationary) average equals the configured drift. Also score a variance-only version.
- **Fitting:** use multiple random starts (`search_reps`), order regimes by variance to avoid label switching, and report any fits that fail to converge.

### Model blending
- **Method:** once several models are on the scoreboard, blend them by drawing whole simulated paths from each model in proportion to its weight, so each path keeps its model's internal structure.
- **Weights:** compare equal weights with CRPS-optimized weights (non-negative, summing to 1) estimated only from past origins. Equal weights are often hard to beat.
- **Coverage:** check the blend's interval coverage specifically, because averaging distributions tends to make ranges too wide.

## New Phase 4B: signals and regression-based drift

Insert this phase between Phase 4 and Phase 5.

### Point-in-time signal store
- **Release dates:** every signal value carries the date it became publicly known and can only be used at origins on or after that date. Add a test that fails otherwise.
- **Industry data:** these series aren't available from yfinance.
  - Store them as versioned CSVs in a repo-level `data/external/` folder, with period, value, release date, and source URL.
  - Ask me before scraping any website.
  - If only revised values are available (not the originally published ones), flag revision bias as a limitation.

### Candidate signals
1. **Momentum:** the return from 12 months ago to 1 month ago, with the most recent 1-month return as a separate reversal control.
2. **Relative strength:** the 3–6 month return relative to SOXX. Check how much it overlaps with momentum.
3. **Earnings drift:** the earnings-day abnormal return from the Phase 4 earnings module, active for about 60 trading days after each report. It stands in for an earnings-surprise measure without needing paid analyst-consensus data.
4. **Industry cycle:** year-over-year growth in TSMC's monthly revenue and in SIA's monthly global semiconductor sales, each dated by its release.
5. **Customer-to-supplier lead-lag:** lagged returns of each company's major customers, taken from a hand-maintained `data/external/supply_chain_links.csv` with supplier, customer, source, and as-of date. Customers may be outside the universe; monthly sampling keeps trading-calendar differences minor.

### Regression design

Split the forecast into two pieces so each is estimated where it has the most statistical power:

- **Sector tilt:** a time-series regression that predicts the sector factor (SOXX residualized on SPY) from the industry-cycle signals. Compute that factor in this phase; Phase 5 reuses it. Because there is only one sector series, statistical power is low, so expect little predictability.
- **Relative tilt:** a pooled panel regression across all tickers. It predicts each stock's return net of its market and sector exposures from the stock-level signals, with signals standardized across tickers on each date.
- **Resulting drift:** CAPM drift + (sector beta × sector tilt) + relative tilt.

Estimation details:

- **Sampling:** sample monthly, with forward 21- and 63-day returns as targets.
- **Re-estimation and leakage:** re-estimate every regression at each backtest origin using only data available at that point. Training rows must have fully realized targets. For example, a 63-day forward return that starts on a given date isn't known until 63 trading days later, so exclude any row whose target window ends after the origin.
- **Standard errors:** use Driscoll-Kraay standard errors (statsmodels `cov_type="hac-groupsum"`). They handle both the cross-sectional correlation among semiconductor stocks and the serial correlation from overlapping returns. Add Fama-MacBeth regressions as a robustness check.
- **Fixed effects:** don't use ticker or sub-industry fixed effects without asking. They reintroduce each stock's historical average return, which is exactly the leak the drift policy bans.

### Models, simple to complex
1. **Zero-signal baseline:** the existing CAPM drift.
2. **Single-signal models:** one pooled regression per signal.
3. **Multi-signal models:** start with the simple average of the single-signal forecasts, which is often more robust than one regression containing every signal. Then try ridge or lasso.
4. **Quantile regression:** use statsmodels `QuantReg` at the 5th, 25th, 50th, 75th, and 95th percentiles, scored with pinball loss against the simulation's quantiles. This also shows whether signals move the tails, for example a cycle downturn widening the left tail.

### Feeding tilts into the simulation
- **Shrinkage and cap:** shrink predicted tilts toward zero (drift = CAPM drift + λ × tilt) and cap the annualized tilt. Choose λ using pre-holdout origins only.
- **Coefficient uncertainty:** precompute several hundred coefficient sets by block-bootstrapping *dates* (not individual rows), then assign each simulated path one set.
- **Horizon:** apply a tilt only over the horizon it was estimated for, then revert to CAPM drift, unless longer horizons are separately estimated and validated.

### Evaluation
- **Holdout:** freeze the most recent 24 months. All signal selection, the choice of λ, and any other tuning happen before that window. Run the holdout once, at the end, and report the result whatever it shows.
- **Accuracy vs. CAPM drift:**
  - Out-of-sample R² (Campbell-Thompson).
  - The Clark-West test for nested models.
  - For relative tilts, the monthly rank information coefficient, averaged over time with Newey-West t-stats. With only 15 stocks, each individual month's value is noisy.
  - Changes in CRPS and interval coverage once tilts feed the simulation.
- **Effect size:** express each signal's impact as an annualized return tilt, not just a t-statistic.
- **Stability:** show rolling coefficient charts, flag sign flips, and split results by market environment (e.g., the 2022 downturn vs. the rally that followed).
- **Research log:** record every specification tried in `research_log.md`, including failures. After many trials, some t-statistics will look strong by chance, so treat anything below about 3 with skepticism.
- **Cross-validation:**
  - Split on unique dates, not rows. A plain `TimeSeriesSplit` on panel data would put part of a month's cross-section in training and the rest in testing.
  - Leave a gap between training and test folds at least as long as the forecast horizon, measured in sampling periods, so overlapping returns don't leak across folds.

## Phase 5 additions
- **Shared regimes:** put regime switching on the sector factor, so all stocks share one regime path and turn turbulent together in portfolio simulations. If each stock switched regimes independently, the simulation would miss selloffs that hit the whole sector at once.
- **Tilts and blending:** feed the Phase 4B drift tilts into the multi-asset simulation, and extend blending and the scorecard to multi-asset and portfolio forecasts.

## Changes to sections of the original brief

- **Section 3 (defaults):** add these defaults:
  - Signal horizons of 21 and 63 days.
  - A holdout of the most recent 24 months.
  - λ = 0.5 as a starting value, tuned before the holdout.
  - A tilt cap of ±10% annualized.
  - 500 bootstrap coefficient sets.
  - Equal blend weights.
- **Section 4 (stack):** add statsmodels (robust standard errors, quantile regression, Markov switching) and scikit-learn (ridge and lasso, cross-validation utilities, `mean_pinball_loss`) as approved dependencies.
- **Section 5 (structure):** add
  - `models/merton.py`, `models/har.py`, `models/regime.py`, and `models/blend.py`
  - `signals/` for the point-in-time store and signal construction
  - `forecasting/` for regressions, shrinkage, and the coefficient bootstrap
  - the repo-level `data/external/` folder
  - `research_log.md`
- **Section 7 (tests):** add tests confirming that:
  - Merton's simulated mean matches the configured drift.
  - HAR recovers known coefficients on synthetic data.
  - Appending post-origin data doesn't change the regime probabilities used at that origin.
  - Signals released after an origin never reach models at that origin.
  - Training targets never end after the origin.
  - Coefficient bootstraps resample dates rather than rows.
  - Blend weights are non-negative, sum to 1, and use only past origins.
- **Section 8 (boundaries):**
  - Always: log every specification in `research_log.md`, and date all external data by its release.
  - Ask first: before scraping any website, adding fixed effects, or adding signals beyond the Phase 4B list.
  - Never: use smoothed regime probabilities in forecasts, use a signal before its release date, or look at holdout results before the final evaluation.
- **Section 9 (success criteria):** add three criteria:
  - The scorecard includes the Merton, HAR, regime, and blended models.
  - A signal report shows out-of-sample R², Clark-West results, IC, effect sizes, CRPS and coverage changes, and stability for every signal.
  - The one-time holdout result is reported regardless of outcome.
- **Stretch list:** remove regime switching, which is now part of Phase 4.

## Deliberately not added (don't build unless I ask)
- **Heston stochastic volatility:** hard to pin down from daily returns alone. It's mainly useful when calibrated to option prices, and free historical option data for backtests is hard to find. Fine as a separate learning notebook if I ask.
- **Ornstein-Uhlenbeck mean reversion on individual stocks:** OU assumes prices get pulled back toward a fixed level, which individual stocks don't reliably do. It could be revisited later for price spreads between close peers.
- **Rough volatility and exotic Lévy processes:** heavy math for little expected gain with daily data.
- **Gradient boosting and neural nets:** a few thousand highly correlated stock-months are easy to overfit. Revisit only after the linear signal models are scored.

## Open questions to raise in your plan
- Which industry series can actually be collected with reliable release dates, and how far back do they go?
- Is a 24-month holdout long enough given how many signals will be tested?
- Does anything here look over-scoped relative to what's already built? Flag it.

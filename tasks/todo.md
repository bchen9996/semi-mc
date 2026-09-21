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
- [x] **Streamlit dashboard**, later replaced by an HTML dashboard. Owner decision
      2026-09-16: build the fuller of the two scopes discussed -- forms that drive and run
      `fetch`/`validate`/`simulate`/`backtest`/`signals`/`scenario` from the browser, not
      just a viewer for existing runs. Owner decision 2026-09-17: rebuild the same scope as
      a custom HTML/CSS/JS frontend (`dashboard/web/`) over a stdlib `http.server` backend
      (`dashboard/server.py`), replacing Streamlit outright -- a better-looking UI and one
      fewer dependency. `dashboard/jobs.py` is unchanged (no web-framework import, covered
      by the normal test suite) and still launches each command as a subprocess exactly as
      a terminal user would, so `cli.py` stays the one implementation. `signals --holdout`
      is still deliberately not exposed -- see MODEL_NOTES §17. `uv run semimc dashboard`.
      Motivation for the rebuild: eventually feeding a run's output into a paper-trading
      workflow against Alpaca -- not yet built, and it needs its own design pass (new
      dependency + API keys are an "ask first" item, and the Phase 4B tilt's holdout came
      back null, so "prediction" cannot mean a point forecast fed straight to an order).

## Addendum: paper trading on Alpaca (semis_paper_trading_brief.md)

Full plan: `starry-drifting-parasol.md` (Claude Code plan file, approved 2026-09-17).
Owner decision that day: T1 (relative signals), S1 (signal tilt), and S2 (long/short) are
out of scope for now -- their signal already failed its one-time holdout (see the Phase
4B addendum entry above), and the brief's own rule is "paper trading can't rescue a signal
that failed it." In scope: S0 (equal-weight control), S3 (vol-targeted control) and its
trailing-vol shadow twin, the shadow sector-timing sleeve, and theses T2/T3/T4(shadow,
no verdict before month 12)/T5.

- [x] **Phase 0 -- foundations.** No sleeve trades yet; this is the scaffolding every later
      phase builds on.
  - [x] `engine.runner.fit_at_origin` -- extracted the `context -> training_window ->
        build_model -> fit` sequence that `backtest/walkforward.py` and `cli.py::simulate`
        each used to duplicate into one shared function (the brief's own requirement: "one
        strategy module shared by the backtest, the shadow book, and the live jobs").
        `LookAheadError`/`assert_point_in_time` moved from `backtest/walkforward.py` to
        `data/__init__.py`, next to `training_window`/`context`, to avoid a
        backtest<->engine import cycle; re-exported from their old home so existing
        imports (`tests/test_backtest.py`) keep working. Verified behavior-preserving by
        running the pre- and post-refactor code on identical fixtures with a fixed seed
        and diffing cell by cell: `rows` (the actual scored output) came back
        byte-for-byte identical; only `failures` bookkeeping changed (a whole-ticker
        `training_window` failure is now attributed per model instead of once with
        `model="-"`, which no test asserted on). Pinned with
        `test_fit_at_origin_extraction_did_not_change_scored_output`.
  - [x] `PaperConfig` on the existing `Config` schema (`config.py`), not a second schema --
        `config/paper.yaml` is a full alternate config file (like `default.yaml`, with
        `reporting.runs_dir: runs/paper`), loaded via the existing `--config` flag, so it
        gets `extra="forbid"`/`Config.path()`/`--set` overrides for free. Every sleeve
        defaults `trading_enabled: false`. Scoped to what S0/S3 need; extending it for
        S1/S2 is future work, not a gap in this one.
  - [x] `execution/broker.py`: the `Broker` Protocol (account, positions, asset lookup,
        clock/calendar, submit, get-by-client-order-id, cancel, list open orders -- the
        brief's own list) and `execution/fake_broker.py`'s `FakeBroker`, an in-memory
        implementation with injectable fills and every failure mode the brief names for
        sandbox testing (timeouts after a successful submit, rate-limit errors, partial
        fills, rejections, wash-trade rejections, a closed market, a blocked account).
        `FakeBroker` is the only broker the normal test suite constructs; a real
        `AlpacaPaperBroker` is Phase 4.
  - [x] `docs/alpaca_notes.md`: Alpaca facts verified against current docs (base URL,
        `TradingClient(paper=True)`, fill-simulation behavior, wash-trade mechanics, the
        128-char `client_order_id` limit, fractional-order rules, the 15-min delayed-SIP
        Basic plan, the MCP server's `ALPACA_PAPER_TRADE` env var), with source URLs and an
        explicit list of what still needs checking against a real account (paper-account
        count limit, trade-updates stream reconnect behavior, rate limits, the wash-trade
        page itself).
  - [x] `alpaca-py` and `python-dotenv` added as dependencies (this project's first
        credentialed external service -- FRED, the only prior external API, is keyless).
        `.env.example` + `.gitignore` entry for `.env`.
- [x] **Phase 1 -- `strategy/` module.** `strategy/weights.py`: `equal_weight`,
      `scale_to_target_vol` (capped at `paper.s3_max_leverage`, no leverage by default;
      degenerate/non-positive forecasts go to cash). `strategy/forecasts.py`:
      `portfolio_vol_forecast` (fits `model_name` via `engine.runner.fit_at_origin` and
      simulates via `run_simulation`, annualizing the empirical vol of the portfolio's own
      simulated terminal value -- not a closed-form combination of per-ticker variances,
      so it reflects whatever cross-ticker dependence the model itself encodes; verified
      against GBM's closed-form `annualized_variance` for a single ticker) and
      `trailing_portfolio_vol` (the shadow twin's sizing input -- explicitly converts log
      returns to simple returns before aggregating across tickers, per CLAUDE.md's
      convention, with a regression test that would catch a caller that mixed them up).
      `strategy/decide.py`: a frozen `Decision` dataclass (`as_of`, `sleeve`, `weights`,
      `diagnostics`, `snapshot_hash` -- the SHA-256 the brief's `decide` job will freeze
      decisions with; excludes `diagnostics` from the hash on purpose, sorts tickers so
      weight order never matters) and `decide_s0`/`decide_s3`/`decide_vol_twin_shadow`
      plus one `decide(sleeve, ...)` dispatcher every later phase calls instead of
      constructing a `Decision` by hand.
- [x] **Dashboard: explanatory help text for every option**, ahead of schedule (owner
      request, 2026-09-18, not part of the phase plan). Every panel and field in
      `dashboard/web/index.html` now has a visible-without-interaction `.muted.small`
      explanation, sourced from `cli.py`'s own `Option` help strings, `config/default.
      yaml`'s comments, and `MODEL_NOTES.md` (e.g. a one-line-per-model glossary covering
      all 11 `MODEL_NAMES` under Simulate's model select, referenced rather than repeated
      under Scenario's). No `app.js`/`server.py` changes needed.
- [x] **Phase 2 -- shadow sector-timing (T4).** `strategy/decide.py::decide_sector_shadow`:
      wraps the existing `forecasting/sector.py` regression (`sector_factor`,
      `industry_yoy_growth`, `build_sector_regression_row`, `fit_sector_tilt` -- the same
      functions `semimc signals --holdout` already evaluates with) as a new `"sector_
      shadow"` sleeve. Point-in-time: refits on `forecasting.training_rows`-filtered rows
      (target realized on or before `as_of`) using `forecasting.pipeline.monthly_origins`,
      then forecasts today's sector-factor return from `industry_yoy_growth` at `as_of`
      (already point-in-time by construction via `signals.external.as_of`). The forecast is
      expressed in units of the regression's own historical target std (clipped to +/-1),
      scaling S0's gross exposure by up to the new `paper.sector_tilt_strength` (0.15
      default) and capped by `paper.s3_max_leverage`, same no-leverage/never-short bound as
      S3. A missing or too-thin industry series (`<12` usable rows, or no defined YoY
      reading) degrades to plain S0 weights (`diagnostics["tilt_applied"] = False`) rather
      than raising -- a shadow sleeve losing its input must not break a decision job.
      Deliberately has **no** `paper.sleeves` entry (unlike s0/s3): there is no config
      switch that could make it tradeable, matching the brief's "no verdict before the
      12-month review" rule -- it only ever gets compared against S0 after the fact.
      `decide()`'s dispatcher and `SLEEVES` both extended; `test_config_paper.py` pins that
      `"sector_shadow" not in cfg.paper.sleeves`.
- [x] **Phase 3 -- ledger + execution mechanics, `FakeBroker` only.**
      `live/ledger.py`: stdlib `sqlite3` (no new dependency), one `Ledger` class, one
      table per brief entity (`decisions`, `orders`, `fills`, `snapshots`, `adjustments`,
      `reconciliations`, `incidents`) and exactly one INSERT-or-SELECT method per
      operation. `decisions`/`orders`/`snapshots` are idempotent (`ON CONFLICT ...`) by
      the keys that should never duplicate (`sleeve, as_of, snapshot_hash` /
      `client_order_id` / `sleeve, as_of`); fills/adjustments/reconciliations/incidents
      are plain appends. `decisions` carries nullable `data_vintage`/`git_commit` columns
      a later phase's `live/decide.py` job populates -- the schema doesn't need to change
      when that job exists, only get filled in.
      `execution/orders.py`: broker-agnostic (`Decision`, positions, prices, equity,
      config, an optional `fractionable` map in -- `OrderRequest`s out). Deterministic
      `client_order_id` from the decision's own `snapshot_hash` (so a rerun of an already-
      executed decision is idempotent at the broker); a collared marketable limit price
      (`paper.execution.collar_bps`, already in Phase 0's config); skips a symbol's trade
      below `paper.minimum_trade_pct` of equity or with no usable price; rounds to whole
      shares when `fractionable` says no; a defensive (never-short) cap on sell qty at the
      currently held long quantity, since every sleeve's weights are non-negative by
      construction but a bug elsewhere must never turn into a short; `sequence_orders`
      puts sells before buys since this project never trades on margin.
      `execution/pretrade.py`: `check_pretrade` runs every sleeve-level gate at once
      (kill switch, sleeve `trading_enabled`, registered-account allowlist, account
      status, market clock, execution-window clock time) and returns *every* failure
      found, not just the first; `check_order_tradable` is the per-order asset check.
      The kill switch (`check_kill_switch`) is a plain sentinel file at the new
      `paper.kill_switch_path` (default `runs/paper/KILL_SWITCH`) -- its mere existence
      halts all trading, no infra and no new dependency.
      `tests/test_execution_integration.py` proves the whole offline path end to end
      against `FakeBroker`: decide -> pretrade -> build orders -> submit -> ledger, and
      that re-executing an already-filled decision places no new orders.
- [x] **Detour before Phase 4 (owner request, 2026-09-18): higher-frequency Phase 4B
      signal search + a durable power calculation.** Not part of the phase plan --
      answers "why does this take years, and can a different angle help" with this
      project's own numbers rather than a rule of thumb. See the plan file
      (`starry-drifting-parasol.md`, overwritten for this detour) and `research_log.md`'s
      2026-09-18 entry for the full result.
      `analytics/power.py` (new): `required_sample_size` (extrapolated `t ~ sqrt(n)`
      sample size to a target t-stat) and `effective_independent_units` (the
      `N/(1+(N-1)*rho)` design-effect formula) -- both pure, tested against closed-form
      pins of the real numbers (`relative_strength`@63d: t=2.03, n=127 -> ~276 dates
      (~23y); 14 semis tickers at rho=0.58 -> only ~1.64 effectively independent names).
      `SignalsConfig.origin_frequency` (new, default `"ME"`, mirrors
      `BacktestConfig.origin_frequency`) makes Phase 4B's origin sampling configurable;
      `forecasting.pipeline.monthly_origins`/`build_full_panel` gained an additive
      `freq`/`origin_freq` parameter (default preserves current behavior everywhere).
      `forecasting.cv.approx_trading_days_per_period`/`min_gap_periods` (new) replace two
      hardcoded "21 trading days/period" assumptions found stale while wiring this up --
      `cli.py`'s Driscoll-Kraay `maxlags` calculation and `signals.cv_gap_periods`'s
      correctness for a 63-day horizon at monthly origins (needs 3 periods of gap, the
      config only ever configured 1; already null and flagged `low_power`, so no past
      conclusion changes, but the new frequency work must not repeat it) -- `semimc
      signals` now warns when the configured gap is too small for the chosen
      horizon/frequency, and prints an automatic power diagnostic per signal.
      Also closed a real gap found while researching this: **`semimc signals --holdout`'s
      "refuses to run twice" claim was false** -- no code enforced it. Added an actual
      guard keyed on `research_log.md`'s own recorded `- Holdout window: ... onward`
      lines: refuses outright if a run would overlap an already-recorded window, and
      requires a new `--confirm-new-holdout-window` flag even for a genuinely later
      window (since `holdout_start` moves in lockstep with `last_date` and would
      otherwise pass a naive "later" check while overlapping ~99% of the old one). This
      protects any future `--holdout` invocation, not just this detour's.
      **Result** (research_log.md, 2026-09-18, pre-holdout CV only -- `--holdout` never
      invoked): the shorter-horizon/higher-frequency redesign does not rescue the search.
      `relative_strength`'s rank-IC t is *weaker* at 5d/10d weekly (1.60/1.48) than the
      63d/monthly result (2.03) despite ~4.4x more independent dates (553 vs 127) --
      confirming the effect size does not carry over across horizons the way the
      motivating extrapolation assumed. Every OOS R² is at or below zero except a
      rounding-level +0.0004; sign-flip rates 0.32-0.52 (near coin-flip). No candidate
      clears the pre-holdout bar that would justify spending a fresh holdout at these
      settings. The higher-frequency machinery stays in the codebase as a reusable tool
      for the next signal idea, not spent or discredited by this null result.
- [x] **Phase 4 -- real `AlpacaPaperBroker`.** `execution/alpaca_broker.py`: implements
      the `Broker` Protocol against Alpaca's real paper API via `alpaca-py` (0.44.0).
      `paper=True` hardcoded in the one `TradingClient(...)` construction site inside
      `from_env()` -- not a parameter, no code path can build a live client. `from_env()`
      loads `.env` via `python-dotenv` and raises a clear error naming `.env.example` if
      `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` are missing; the plain `__init__(client)`
      constructor (taking an already-built `TradingClient`) is what makes the unit tests
      possible with no credentials.
      Every fact behind the translation layer was verified directly (installed SDK source
      + WebFetch/WebSearch against Alpaca's docs and community sources), not assumed:
      numeric fields are strings on the wire (coerced via `_to_float`/`_optional_float`,
      with `Account`'s missing-value case raising since sizing depends on it, vs.
      `Position.market_value`'s defaulting to 0.0 since it's display-only);
      `Order.status`'s 18 real values bucket down to this Protocol's 7 via
      `_map_order_status`, disambiguating `done_for_day`/`calculated` with the order's own
      `filled_qty` rather than a fixed guess; `Calendar.open`/`.close` need `.time()`
      extraction but no timezone conversion (confirmed naive-already-ET by reading
      `Calendar.__init__`'s own source); `Clock`'s four timestamps are stripped to naive
      to match `FakeBroker`'s convention.
      **Found and fixed a real bug in already-shipped Phase 3 code**: `execution/
      orders.py`'s docstring claimed "real Alpaca" treats a duplicate `client_order_id` as
      idempotent -- verified false (HTTP 422, "client_order_id must be unique"). Corrected
      the docstring; `AlpacaPaperBroker.submit_order` catches that specific rejection and
      falls back to `get_order_by_client_id`, restoring the intended retry-safety via a
      fallback lookup rather than a passthrough.
      Deliberately does **not** duplicate `FakeBroker`'s proactive pre-submit checks
      (market open/account status/asset tradable) -- `execution/pretrade.py` (Phase 3)
      already gates the real path, and Alpaca mostly doesn't even reject those
      conditions server-side (a day order submitted while the market is closed is queued,
      not rejected, per WebSearch) -- so reconstructing them would cost network round
      trips to fabricate errors the server itself rarely produces this way.
      Also found and fixed: `pyproject.toml`'s `addopts` never actually included
      `-m "not network"`, so `CLAUDE.md`'s existing claim that the `network` marker is
      "deselected by default" was false (no test had used it yet to notice). Fixed --
      `uv run pytest` now truly excludes network tests; `uv run pytest -m network` runs
      them.
      `tests/test_alpaca_broker.py` (33 tests, no network, mocked `TradingClient` returning
      real `alpaca-py` model instances via `model_construct`): every translation path,
      the full status-mapping table, the duplicate-id fallback, error-code translation
      (429/403/404/422), `from_env`'s missing-key error and its one hardcoded `paper=True`.
      `tests/test_alpaca_broker_integration.py` (new, every test `@pytest.mark.network` +
      `pytest.mark.skipif` on missing env vars -- degrades to a clear skip, not a failure,
      confirmed by running `uv run pytest -m network` in this session): read-only checks,
      a live duplicate-`client_order_id` resubmit, and a submit/verify/cancel round trip.
      **None of these ran against a real account this session** -- the owner has not yet
      created `.env`; `docs/alpaca_notes.md`'s "still to verify" section now flags the
      duplicate-id wording as the highest-priority thing to confirm once keys exist.
  - [x] **Follow-up (owner request, 2026-09-18/19): real-account network verification.**
        Owner created `.env` with real paper-account keys. `tests/test_alpaca_broker_
        integration.py` was never actually reachable, even with real keys, because its
        `skipif` read `os.environ` at collection time and nothing had called
        `load_dotenv()` before then -- fixed by loading `.env` at module import. All 6
        tests then passed against the real account, including the highest-priority
        duplicate-`client_order_id` fallback. **New finding, corrected**: canceling an
        already-canceled order succeeds silently at the real API (idempotent), not a 422
        as originally assumed -- `FakeBroker` still raises `ValueError` for the same case
        (documented asymmetry, `alpaca_broker.py`'s module docstring and `docs/
        alpaca_notes.md`), and the integration test's wrong expectation was fixed to match
        observed reality rather than the original guess.
- [x] **Phase 5 -- live jobs & scheduling.** `live/decide.py`, `live/execute.py`,
      `live/mark.py`, `live/report.py`; new `semimc paper decide|execute|mark|report` CLI
      sub-app; job hosting is GitHub Actions (`.github/workflows/paper_trading.yml`, the
      repo's first CI).
      `PaperConfig.ledger_path` (new, `ledger/paper.sqlite3`, deliberately outside `runs/`
      since `runs/` is entirely gitignored and the ledger must survive a stateless CI
      runner between scheduled jobs) and `PaperSleeveConfig.rebalance_frequency` (new,
      default `"ME"`, same pandas-offset-alias convention as `origin_frequency`
      elsewhere) -- both resolved/consumed like every other config value, nothing
      hand-rolled off `project_root`.
      `live/decide.py::run_decide`: computes and freezes one sleeve's decision, skipping
      (not erroring) if this sleeve already decided the current rebalance period so a
      daily cron can safely drive a monthly-cadence sleeve; refuses (unless `force=True`)
      if a *different* decision already exists for the exact same day. **Found and fixed
      a real ordering bug while testing this**: the cadence-skip check originally fired
      before the differing-decision check even for a same-day rerun, making the
      differing-decision refusal unreachable dead code -- fixed by only letting a
      *different* prior day trigger the cadence skip. **Found and fixed a real pandas
      version-skew bug**: `pd.Period` still wants the pre-2.2 month/quarter/year-end
      aliases (`"M"`/`"Q"`/`"Y"`), not the `"ME"`/`"QE"`/`"YE"` aliases this project's
      `origin_frequency` convention uses everywhere else for `Timestamp`/`Grouper` --
      translated locally in `live/decide.py`, not project-wide, since every other use of
      these strings wants the newer alias.
      `live/execute.py::run_execute`: acts on the sleeve's *most recently frozen*
      decision, not one dated exactly today -- a longer-than-daily cadence means most days
      have no decision dated "today," and a daily `execute` finding nothing new to trade
      most days is the intended steady state. **Found and fixed a real cross-day
      collision bug while designing this**: `execution/orders.py::client_order_id` keyed
      only off `decision.as_of` and the symbol, so if `live/mark.py` ever canceled a stale
      unfilled order and a later day's `execute` tried to rebuild it against the same
      still-current decision, it would generate the *same* `client_order_id` as the
      canceled order -- a real Alpaca duplicate-id rejection whose fallback lookup would
      silently return the old, canceled order instead of submitting a fresh one. Fixed by
      giving `client_order_id`/`build_orders` an optional `as_of` parameter (defaulting to
      `decision.as_of`, so every existing caller/test is unaffected) that a live job
      passes as *today's* date instead, keeping same-day retries idempotent while giving
      each new day's attempt at the same decision its own id.
      `live/mark.py::run_mark`: polls fill status for every ledger-open order (recording
      only the *delta* since the last recorded fill -- `filled_avg_price` is the order's
      cumulative average, not this specific fill's own price, a documented approximation),
      cancels (never resubmits) an order that has sat unfilled past `paper.execution.
      reprice_interval_minutes` (finally giving that Phase-0 config value, previously
      unused by any code, an actual effect) so the next `execute` rebuilds the remaining
      gap at a fresh price, and reconciles the ledger's own fill-derived position view
      (`Ledger.net_filled_qty`, new) against the broker's actual positions.
      `live/report.py::build_report`/`render_markdown`: a plain ledger read, no broker
      involved, so it stays usable even when Alpaca or market data is down.
      New `Ledger` methods (`last_decision`, `update_order_status`, `net_filled_qty`,
      `snapshots_for`, `recent_orders`, `recent_fills`, `latest_reconciliation`) --
      `live/report.py`'s first draft used `ledger._conn` directly, caught and fixed before
      landing (violated the ledger's own "nothing above this layer writes SQL directly"
      rule).
      Exit-code convention across all four commands: 0 for both "did real work" and "ran
      and correctly declined" (market closed, kill switch, already decided this period,
      nothing to trade) -- these are the pretrade gates working as designed, not CI
      failures; non-zero only for a genuine unexpected exception or a reconciliation
      discrepancy.
      GitHub Actions workflow verified via `astral-sh/setup-uv`'s own current docs
      (WebFetch, since the previously-known action version had since been superseded) and
      manual YAML-parse validation (no `actionlint` available locally). Structured as
      three independent jobs (`decide-and-execute`, `mark`, `report`), never chained with
      cross-job `needs:` -- the three cron schedules are mutually exclusive per trigger, so
      a `needs:` dependency on a job gated by a *different* schedule would leave the
      dependent job perpetually skipped except on the one trigger where both matched.
      `decide-and-execute`'s own step order (fetch -> decide -> execute) provides the
      within-rebalance sequencing instead. `decide-and-execute` and `mark` share one
      `concurrency` group and serialize their own sleeve matrix (`max-parallel: 1`),
      since both write and commit `ledger/paper.sqlite3` and their cron schedules
      overlap at the top of most hours. Manually smoke-tested end to end against fixture
      data and, for `mark`, against the real paper account: monthly-cadence skip,
      same-day idempotent rerun, pretrade decline with a real account, and a live `paper
      mark` call all behaved as designed.
      Real trading stays behind three gates this phase does not touch:
      `paper.registered_accounts` (empty), each sleeve's `trading_enabled` (false), and
      the kill switch file -- plus the repository secrets the workflow needs, which only
      the owner can add.
- [x] **Phase 6 -- evaluation tooling for T2/T3/T5.** The original brief that numbered
      T1-T5 no longer exists anywhere (not this repo, not the other working directories,
      not the owner's own memory -- confirmed 2026-09-19). T2/T3/T5's definitions were
      reconstructed from what the codebase already implies
      (`strategy/decide.py::decide_vol_twin_shadow`'s own docstring already calls itself
      "S3's shadow twin (T3)") and confirmed with the owner before `THESES.md` (new) was
      written -- pre-registered before any sleeve has traded for real
      (`paper.registered_accounts` still empty, every `trading_enabled` still false).
      T1/T4 stay excluded for their existing reasons (failed holdout; no verdict before
      month 12).
      `analytics/paper_evaluation.py` (new, T2/T3): `equity_curve_from_snapshots` reads
      S0/S3's real broker-confirmed equity straight off `Ledger.snapshots_for`;
      `replay_shadow_equity_curve` (new math this phase adds) compounds
      `vol_twin_shadow`'s recorded decisions (`Ledger.decisions_for`, new) against
      realized prices, since a shadow sleeve never trades and so never has a snapshot.
      **Found and fixed a real off-by-one while hand-tracing the replay's own test**: the
      rebalance day's own return was excluded from both the outgoing and incoming
      decision's period (`> as_of` paired with `< next_as_of`), silently dropping that
      day's return from the replay entirely. Fixed by making the upper bound inclusive
      (`<= next_as_of`) -- the rebalance day's return is now correctly attributed to the
      *outgoing* weights, matching standard performance-attribution convention (the new
      weights only start earning from the next trading day).
      `compare_sleeves` reduces either kind of curve to annualized return/vol/Sharpe/max
      drawdown/VaR/CVaR (`analytics/risk.py`, unchanged) plus a paired block-bootstrap CI
      on the return and vol *difference* between two curves -- `paired_block_bootstrap`
      reuses `analytics/scoring.py::block_bootstrap_origins` by grouping contiguous
      trading-day blocks as the resampling "origin" instead of that function's original
      cross-sectional-dependence use, the same mechanism turned to a time-series block
      bootstrap. New `PaperEvaluationConfig` (`n_bootstrap`, `block_length_days`) keeps
      both tunable in config rather than hardcoded, matching every other bootstrap
      elsewhere in this project.
      `execution/market_data.py` + `analytics/execution_cost.py` (new, T5): the first use
      of `alpaca.data` anywhere in this project -- `AlpacaMarketDataClient` mirrors
      `alpaca_broker.py`'s plain-constructor-plus-`from_env()` shape exactly, reusing the
      same `.env` keys, no new secret. `execution_cost_report` prices every fill against
      an independent NBBO-midpoint arrival price at the order's own `submitted_at`, a
      source `execute` itself never consults when pricing an order in the first place.
      **Found and fixed a real bug against the real account**: the integration test's
      first run failed with "can't subtract offset-naive and offset-aware datetimes" --
      `Quote.timestamp` comes back aware UTC (confirmed live), the opposite convention
      from `alpaca_broker.py`'s naive-`Clock` handling this project had gotten used to.
      `arrival_price` now normalizes a naive `at` to UTC rather than assuming either
      convention; a subsequent real call against AAPL returned a real, sane quote.
      New `semimc paper evaluate --thesis t2|t3|t5` (`cli.py`): never refuses for lack of
      history (unlike `--holdout`'s one-time-spend guard, live P&L isn't "used up" by
      being looked at early) -- prints real numbers always, and a caveat below
      `THESES.md`'s stated minimum (3 months' overlap for T2/T3, ~20 fills for T5).
      Manually run against the real (still largely empty) ledger for all three theses:
      each reported cleanly (NaN/zero-overlap with a caveat for t2/t3, "no fills yet" for
      t5) rather than crashing or dividing by zero.
- [x] **Phase 7 -- monitoring/alerting.** Owner decision 2026-09-19: no new dependency or
      credential -- GitHub's own email-on-failed-scheduled-workflow notification is the
      alert channel. The work was making sure a real problem actually fails the job so that
      fires, and that the job's ledger writes survive even when it does.
      Found two incidents that could occur but couldn't reach a non-zero exit: `execute`'s
      per-order tradability failure (`execution/pretrade.py::check_order_tradable`, e.g. a
      halted/delisted symbol) was recorded at `"warning"` severity and `paper_execute`
      always returned exit 0 regardless; `mark`'s "open order not found at broker" was
      already recorded at `"error"` severity but `MarkResult` had no field carrying it, so
      `paper_mark` had no way to see it. Fixed: the tradability incident is now `"error"`;
      `MarkResult.orders_not_found` surfaces the missing-order case; both `paper_execute`
      and `paper_mark` now `raise typer.Exit(code=1)` on these, alongside the pre-existing
      reconciliation-mismatch exit. Left `check_pretrade`'s failures (kill switch, sleeve
      not `trading_enabled`, unregistered account, closed market, outside the execution
      window) at `"warning"` with exit 0 -- these are routine, by-design, expected
      conditions until Phase 8's rollout, not incidents worth an email.
      Also found, while tracing what happens when a step actually fails: both ledger-
      writing jobs' "Commit the ledger if it changed" step ran only on success, so the one
      run that hit a real problem was the one whose incident/reconciliation row never
      reached the repo, even though the job's own failure still correctly emailed. Fixed
      with `if: always()` on both commit steps -- doesn't mask the failure (a job's
      conclusion is "failure" if any step failed, regardless of what a later
      `if: always()` step does).
      Deliberately excluded: auto-engaging the kill switch on repeated incidents (a
      rollout/safety policy call for Phase 8, not alerting plumbing) and a heartbeat/dead-
      man's-switch for the workflow never running at all (needs an external always-on
      watcher -- exactly the new-dependency cost the owner chose to avoid; recorded below
      under Known gaps).
      Verified against the real (still all-disabled) paper account: `paper decide` then
      `paper execute` still exits 0 on the routine pretrade decline (not trading_enabled,
      unregistered account, market closed) -- no regression on the common case.
- [x] **Phase 8 -- rollout.** Dry run -> sandbox failure injection -> go-live checklist ->
      enable S0/S3 first.
      **Blocking discovery before any of this could be verified**: local `main` was 4
      commits ahead of `origin/main` -- nothing from Phases 4-7 had ever been pushed, so
      `.github/workflows/paper_trading.yml` had never once run on GitHub's own
      infrastructure. Owner decision 2026-09-19: push now (every sleeve stays
      `trading_enabled: false`, so the cron starting to fire for real declines harmlessly
      at pretrade either way).
      **Sandbox failure injection** found two real gaps by reading `execution/
      fake_broker.py`'s own docstrings against what `live/execute.py`/`live/mark.py`
      actually did with the broker calls that can raise these -- not assumed, read
      directly. `FakeBroker.submit_order`'s own docstring for a timeout is explicit ("the
      order was recorded -- look it up by client_order_id, don't resubmit"), but
      `run_execute`'s order loop had no `try/except` at all: a real timeout after a
      successful submit crashed the run before `ledger.record_order` ever ran for that
      order, and skipped every later order in the batch plus the final snapshot. Same gap
      for `RateLimitError`/`WashTradeError`/`AccountBlockedError`/`MarketClosedError`
      raised by `submit_order` itself. Fixed: each is now caught per-order (timeout
      recovers via `get_order_by_client_id` per the docstring's own instruction; the rest
      log an `"error"` incident, add to `order_failures`, and move on to the next order --
      one bad order no longer takes down the batch or skips the snapshot).
      `live/mark.py`'s reprice-cancel call (`broker.cancel_order`) had the identical gap: a
      real, narrow race (the order fills between mark's poll and its own cancel-for-
      staleness call) would raise `ValueError`/`KeyError` and crash the whole sleeve's
      `mark` run, skipping reconciliation and the snapshot. Fixed the same way -- log and
      continue.
      New tests exercise every `FakeBroker` fault mode through the `live/` layer itself
      (`tests/test_live_execute.py`, `tests/test_live_mark.py`), not just the `execution/`
      layer `tests/test_execution_integration.py` already covered.
      **Also found while running the full suite for this phase (unrelated to the fault-
      injection work itself)**: two `test_cli.py` tests that invoke `paper execute` through
      the real Typer CLI use `cli.py::_paper_now`'s real wall-clock time, with no override
      -- outside `paper.execution`'s default 10:00-15:30 window they fail on "outside the
      execution window" regardless of what they're actually testing. Fixed by widening the
      window in `_tradeable_paper_overrides` to the full day; also hit (and fixed) the YAML
      sexagesimal trap on the override values themselves (`23:59` unquoted parses to the
      int `1439`, the same reason `config/paper.yaml` quotes its own `start_time`/
      `cutoff_time`).
      `docs/go_live_checklist.md` (new): the last gate before flipping `trading_enabled`,
      pre-registration-style like `THESES.md`. Items 1-5 (paper keys, GitHub secrets set,
      a clean `workflow_dispatch` run, GitHub's failure-email setting, the kill-switch
      procedure) are this phase's scope; item 6 (`registered_accounts` + `trading_enabled`
      for the sleeve(s) the owner names) is a separate, deliberate action taken when ready
      -- not performed automatically at the end of this phase.
      **The first real `workflow_dispatch` run (checklist item 3) immediately found a
      second real bug**, this time only reachable on GitHub's actual infrastructure: all
      four `decide-and-execute` matrix entries and both `mark` entries share one ledger
      file and one `concurrency` group specifically to serialize their git commits, but the
      run still showed two entries' pushes interleave -- `decide-and-execute
      (sector_shadow)`'s own commit was rejected non-fast-forward because another entry's
      push landed on `origin/main` in between its checkout and its push. `mark (s0)`/`mark
      (s3)` separately failed outright on the expected missing-secrets `RuntimeError`
      (checklist item 2, confirmed, owner's to configure). Fixed the push race: `ledger/
      paper.sqlite3` is binary, so a plain `git pull --rebase` cannot 3-way-merge two
      independent row insertions into it -- on a rejected push, each job now re-fetches
      `origin/main`, resets to it, and re-runs its own `decide`/`execute`/`mark` command
      against that fresh ledger before retrying the commit and push (up to 3 attempts),
      reconciling at the application level instead of asking git to merge binary bytes.
      Both `decide`/`execute` were already idempotent (cadence-skip, same-decision no-op)
      for exactly this kind of safe re-run. `set -e` keeps a genuine command failure (e.g.
      missing credentials) failing the step immediately, not swallowed by the retry --
      verified locally against a real two-clone git race (reproducing the exact "[rejected]
      ... fetch first" the real run showed) before pushing this fix.
      **Owner added the two GitHub repo secrets and re-triggered `workflow_dispatch`,
      2026-09-19: all jobs green** -- `decide-and-execute`, `mark`, and `report` all
      completed cleanly for every sleeve, confirming both fixes above and closing
      `go_live_checklist.md`'s items 1-3 and 5 (item 4, GitHub's own failure-email setting,
      is owner-account state this repo's code can't inspect either way). Item 6 -- actually
      enabling `s0`/`s3` -- stays the owner's separate, explicit action; nothing in the
      paper-trading plan (Phases 0-8) is left to build before that point.
      **Owner decision 2026-09-19: enable `s3` only, not `s0`.** Investigated on request
      ("why can't both be done") before the switch was flipped: `execution/
      orders.py::target_notional` and `live/execute.py::run_execute` both size a sleeve's
      target positions off the account's *whole* equity and *whole* current positions --
      there is no per-sleeve partitioning anywhere, and `AlpacaPaperBroker.from_env()` is
      called identically regardless of `sleeve`, so every sleeve trades the same single
      paper account. Running `s0` and `s3` together would have each sizing against 100% of
      the same capital and the same overlapping 14-ticker universe, each seeing the other's
      resulting fills as "current state" to rebalance away from -- corrupting the very s0-
      vs-s3 comparison T2 exists to make. Enabling both validly and concurrently would need
      a second Alpaca paper account with per-sleeve credential routing, which does not
      exist and is out of scope here. Completed checklist item 6 for `s3` alone:
      `config/paper.yaml`'s `paper.sleeves.s3.trading_enabled` set `true` and the real
      paper account number added to `paper.registered_accounts` (the account
      `broker.get_account().account_number` already surfaced earlier this session, not the
      API key). `s0` stays `trading_enabled: false`.

## Addendum: intraday agent sleeve S4 (`BRIEF_INTRADAY.md`)

Brief filed 2026-09-19 and committed to the repo as `BRIEF_INTRADAY.md` (deliberately, so
it does not repeat `semis_paper_trading_brief.md`'s fate -- that one is cited by every
`paper`/`execution`/`live`/`strategy` docstring and was never committed at all). A
scheduled research-and-trade loop where Python owns the loop, clock, state and limits, and
a language model decides inside it; measured against a random-entry placebo on a shadow
ledger, pre-registered as `THESES.md`'s T6.

**Phase 0 -- foundations, built 2026-09-20.** No LLM call, no order, no job yet -- pure
plumbing, same shape as paper trading's own Phase 0. Full plan: the Claude Code plan file
(overwrote `starry-drifting-parasol.md`, Phase 8's old plan). Built:

- [x] `config.py::IntradayConfig` (+ `IntradayBudgetConfig`/`IntradayRiskConfig`/
      `IntradayScheduleConfig`/`IntradayOptionsConfig`), registered as `Config.intraday`
      top-level and sibling to `paper` -- not nested inside `PaperConfig`, since an LLM's
      trade proposals don't fit `Decision.weights`'s closed-form target-weight shape
      (confirmed by reading `strategy/decide.py` directly: dispatch is a flat `if/elif`
      over sleeves that each return a `pd.Series` of weights, with no notion of discrete
      proposals, thesis text, or confidence anywhere in that layer). `config/paper.yaml`
      gets a matching `intraday:` block at conservative defaults
      (`trading_enabled: false`).
- [x] `intraday/ledger.py::IntradayLedger` -- a sibling to `live/ledger.py::Ledger`, not
      new tables bolted onto it, and its own SQLite file (`ledger/intraday.sqlite3`, not
      `ledger/paper.sqlite3`): S4 writes roughly every 20 minutes all day versus s0/s3's
      monthly-decide-plus-half-hourly-mark cadence, and sharing a file would reintroduce
      the git push race Phase 8 already fixed once, at a much lower write frequency.
      Tables: `briefs`, `watchlist`, `proposals` (every proposal, accepted or rejected,
      per the brief's "blotter: every proposal including rejected ones"), `orders`/
      `fills`/`snapshots`/`reconciliations`, `reflections`, `model_calls` (what
      `budget.py` sums), `headlines_seen` (dedup), and `incidents` -- reused as its own
      copy of `live/ledger.py`'s exact shape rather than composing a second `Ledger`
      instance into the same file. Unlike `live/ledger.py`, no table carries a `sleeve`
      column: this ledger only ever holds one sleeve's data, so it would be a column
      whose value is always the same literal string.
- [x] `execution/alpaca_broker.py::AlpacaPaperBroker.from_env` and `execution/
      market_data.py::AlpacaMarketDataClient.from_env` both take `key_env`/`secret_env`
      parameters (defaulting to s0/s3's existing names, no regression) so S4 can point at
      its own `ALPACA_S4_API_KEY`/`ALPACA_S4_SECRET_KEY` pair without a second broker
      class -- resolves prerequisite 1 below for S4 specifically. Creating the second
      Alpaca paper account and adding the GitHub secrets remains the owner's action.
- [x] `intraday/budget.py` -- `check_budget`/`record_call_cost`/`spent_today` against
      `model_calls`, mirroring `check_pretrade`'s "return a reason, let the caller decide"
      shape. `max_cost_usd_per_day` ships as a $10 placeholder, explicitly not a
      researched figure -- recalibrate from real shadow-mode logging once Stage 1 runs.
- [x] `THESES.md` T6, pre-registered ahead of S4's first order, same Claim/Comparison/
      Decided-by/Minimum-history/Status template as T2/T3/T5.
- [x] `docs/alpaca_notes.md`: both options gates (account level, data feed) recorded as
      still unresolved -- moot until rollout Stage 4.

**Three corrections found by exploring the actual codebase and Anthropic's docs before
writing Phase 0's plan** (not assumed from the brief alone):

1. `.claude/agents/*.md` role files won't work under `--bare` -- confirmed in
   `code.claude.com/docs/en/headless` that bare mode never reads `.claude/agents/`. Role
   prompts will instead be plain markdown files loaded per call via
   `--append-system-prompt-file` (Phase 2).
2. An LLM's trade proposals don't fit `strategy/decide.py`'s `Decision` shape (see Phase 0
   bullet above) -- S4 gets its own parallel data model (Phase 1's `proposals.py`), while
   still reusing `execution/orders.py`'s low-level helpers (deterministic
   `client_order_id`, the collared limit price, `sequence_orders`) rather than
   reimplementing them.
3. **Where the loop runs is now decided: GitHub Actions, not a dedicated VM** (owner
   decision, 2026-09-20). This resolves prerequisite 2 below by changing the design
   instead of building around it: the actual stop-loss becomes a real bracket/stop order
   resting at Alpaca, not a polling process, so GH Actions' 5-minute cron granularity and
   stateless-between-runs nature stop being a problem -- the 20-minute `cycle` cadence
   (same shape as the existing `mark` job's proven 30-minute cron) is only for *soft*
   logic (time stops, thesis re-checks), not the safety-critical part.

**Already built and directly reusable** (verified by reading the code, not assumed):

- `execution/broker.py::Broker.get_calendar` and `AlpacaPaperBroker.get_calendar` already
  return `CalendarDay`s, so the brief's "derive every schedule time from Alpaca's market
  calendar, never hard-code dates" needs no new plumbing.
- `execution/market_data.py::AlpacaMarketDataClient` already wraps `alpaca.data`'s
  `StockHistoricalDataClient` and returns real bid/ask quotes on the free IEX feed (built
  for T5's arrival-price cost analysis). The brief's spread gate and quote-staleness gate
  need a *latest*-quote method added to that class, not a new client -- and its docstring
  already records the finding the brief's own data gate turns on (IEX has usable coverage;
  SIP/DELAYED_SIP unconfirmed, deliberately untested until IEX proves insufficient).
- `execution/pretrade.py`'s kill switch and pretrade gates, the ledger and reconciliation,
  `execution/fake_broker.py`'s fault injection, the `live/` job shape, and Phase 8's
  per-order fault isolation all transfer as-is.
- `data/options.py` exists for the brief's IV-vs-forecast-vol check, but is live-only by
  design (CLAUDE.md) and must stay out of any point-in-time path.
- `THESES.md`'s T6 slot is free (T2/T3/T5 active, T1/T4 out of scope), so the brief's
  "pre-register as T6" needs no renumbering.

**Phase 1 -- proposal schema and validator, built 2026-09-20.** Still no LLM call, order,
or job. Built:

- [x] `intraday/proposals.py::TradeProposal` -- a **pydantic** model, deliberately not a
      dataclass like every other in-process type in this project (`Decision`,
      `OrderRequest`, `Order`, `Position`): those are built by trusted internal code,
      while a proposal is untrusted external input (a Trader LLM's JSON response) that
      must be schema-validated before anything downstream trusts it, the same reason
      `config.py`'s schema is pydantic. `extra="forbid"`, a `model_validator` requiring
      `side`/`size_pct`/`limit_price`/`thesis`/`invalidation_price`/`time_stop_minutes`
      for `open`/`add` only. `instrument: Literal["equity", "option"]` accepted at the
      schema level for a future options phase, but every `option` proposal is rejected by
      the validator below while `options.enabled` is `False` (every stage through Phase
      7) -- no expiry/strike/right/structure fields yet, nothing to validate them against.
- [x] `intraday/proposals.py::ValidationContext`/`validate_proposal` -- mirrors
      `execution/pretrade.py::check_pretrade`'s shape (everything gathered once by the
      caller, every failure returned as `list[str]`, not just the first). `hold`/
      `no_trade` always pass ("first-class answers, never penalized"); `close`/`trim`
      skip every risk-increasing check since they reduce exposure ("when the budget is
      spent, the agent can still close, trim, and hold"). `open`/`add` are gated by: edge
      vs. modeled round-trip cost, `new_positions_per_day`/`round_trips_per_symbol_per_day`
      (counted from the ledger's already-*accepted* proposals today, filtered in Python --
      no new ledger method needed), per-trade risk-to-invalidation, single-name/gross/net
      concentration, a daily-loss soft/hard backstop, post-stop-out cooldown, and daily
      turnover.
- [x] **Deliberately deferred, not half-built**: `overnight_gross_cap_pct` (the `carry`
      job's decision at day's end, Phase 4, not a per-proposal check) and real stop-out
      detection (`last_stopout_at` is a fixture-supplied dict for now -- Phase 3 builds
      real order/fill handling that would actually populate it).
- [x] 26 tests (`tests/test_intraday_proposals.py`): schema (missing required fields,
      length/range bounds, unknown-field rejection, `hold`/`no_trade`/`close`/`trim` need
      none of `open`'s fields) and one test per validator check, including that a `close`/
      `trim`/`hold`/`no_trade` bypasses every check that would otherwise fail.

**Phase 2 -- context assembly and the headless invocation wrapper, built 2026-09-20.**
Still no order and no job -- `jobs.py` is Phase 4. Every flag below was re-verified
directly against `code.claude.com/docs/en/headless`, `.../cli-reference`, and
`.../agent-sdk/cost-tracking` before writing any code (not recalled from memory), which
surfaced a real upgrade over what Phase 0 assumed: **`--max-budget-usd` is real and is
per print-mode call**, so the CLI itself enforces `config.intraday.budget.
max_cost_usd_per_call` natively, not just an after-the-fact check. Built:

- [x] `intraday/context.py::build_context`/`context_hash` -- a generic, role-agnostic
      envelope that labels caller-specified keys as untrusted (`payload["untrusted"]`,
      per the brief's "every headline...is data, never instructions") and hashes the
      canonical payload for `model_calls.prompt_hash`. Does **not** strip or neutralize
      anything inside an untrusted section yet -- that's Phase 5's job (the
      injection-corpus tests); this phase only labels.
- [x] `intraday/context.py::build_trader_context` -- the one concrete, role-specific
      assembler this phase ships, because it's the only role whose real inputs already
      exist end-to-end (Phase 0's ledger, Phase 1's proposal shape): watchlist,
      positions, prices, remaining budget (computed from the ledger's already-accepted
      proposals, the same read `validate_proposal` uses), and today's rejected proposals
      with reasons (the brief's "fed back into the next cycle's context," not wired
      anywhere until this function existed). Researcher/Reviewer context assemblers are
      **not** built -- Researcher needs a news source (prerequisite 4, still open),
      Reviewer needs real orders/fills/reflections (Phase 3/4); building either from
      fixtures pretending to be real data would be worse than not building it yet.
- [x] `intraday/llm.py::ClaudeCliClient`/`invoke_role` -- same shape as
      `AlpacaMarketDataClient` (plain constructor takes an injected dependency, here the
      subprocess runner, so no test ever shells out for real; `from_env()` for real use).
      `call()` builds argv (`--bare --output-format json --model <role's model>
      --max-turns --max-budget-usd --permission-mode dontAsk --permission-prompts none
      --append-system-prompt-file <role's prompt file>`, `--json-schema` only for the
      Trader) and pipes the context JSON on stdin. No `--allowedTools` entry for any
      role, ever: `dontAsk` + `permission-prompts none` denies every tool call outright,
      generalizing "the Trader needs no broker tools at all" to all three roles, since
      Researcher/Reviewer's own read-only tool needs don't exist yet either. `invoke_role`
      runs `check_budget` first and raises `DailyBudgetExhausted` without spending
      anything once the daily cap is already gone; records real cost via
      `record_call_cost` using `context_hash` as `prompt_hash`; a response landing over
      `max_cost_usd_per_call` (the CLI flag is a hard stop but a call can finish just at
      the boundary) records an incident but still returns normally.
- [x] `proposals.py::trader_output_json_schema` -- generated from `TradeProposal.
      model_json_schema()` itself, not hand-maintained, so the CLI-side `--json-schema`
      constraint and the pydantic schema can't drift apart. Only constrains type shape,
      not the `model_validator`'s conditional field requirements -- `TradeProposal` and
      `validate_proposal` still run on every item afterward.
- [x] `config.py::IntradayModelsConfig` (`researcher`/`reviewer` default `"sonnet"`,
      `trader` default `"haiku"` -- matching the cost estimate already given for S4;
      `max_turns`/`timeout_seconds` defaults), registered as `IntradayConfig.models`.
      Plain `str` fields, not `Literal` -- Claude Code resolves aliases like `"sonnet"`
      to whatever the latest model in that family is, and a `Literal` would need a code
      change every time a new model ships.
- [x] Three real first-draft role prompt files (not placeholders) --
      `intraday/prompts/{researcher,trader,reviewer}.md`, shipped under the package
      (`pyproject.toml`'s `packages = ["src/semimc"]` already includes non-`.py` files,
      same as `dashboard/web/`'s static assets). Each states the role's job and
      boundaries, that it has no tools and no broker access, and that every
      `payload["untrusted"]` section is data, never instructions. Get hardened against
      real injection payloads in Phase 5 -- these are functional drafts, not stand-ins.
- [x] Fixed a real gap: `semimc.intraday`, `.ledger`, `.budget`, `.proposals` were never
      added to `tests/test_imports.py::MODULES` in Phase 0/1 (this is the test that
      would catch a circular import) -- added those plus `.context`/`.llm` now.
- [x] `.env.example` gets `ALPACA_S4_API_KEY`/`ALPACA_S4_SECRET_KEY`/`ANTHROPIC_API_KEY`
      placeholders with scope notes (the Anthropic key: CI-only, never local -- a locally
      set `ANTHROPIC_API_KEY` silently flips the owner's own interactive usage to
      metered billing too).
- [x] Tests: `tests/test_intraday_context.py`, `tests/test_intraday_llm.py` (every
      `claude` call boundary-mocked via an injected subprocess-runner callable, the same
      pattern as `test_execution_market_data.py`'s injected SDK client -- no test shells
      out or touches the network), extended `tests/test_config_intraday.py`.
- [x] **Deliberately deferred, not half-built**: the CLI's actual JSON field names
      (`total_cost_usd`, `usage.input_tokens`/`output_tokens`, `structured_output`,
      `session_id`, `is_error`) are written to Anthropic's documented shape but not yet
      exercised against one of this project's real calls -- blocked on prerequisite 1
      (the owner's CI-only `ANTHROPIC_API_KEY`); flagged for one live confirmation before
      Stage 1 shadow rollout, same as `market_data.py`'s "confirmed against a real
      account" pattern. Prompt-injection hardening of the three role files is Phase 5.

**Phase 3 -- order construction, the protective stop, and fault-isolated submission,
built 2026-09-21.** Still no job -- `jobs.py` is Phase 4. **Narrowed from the roadmap's
original one-line description**, each narrowing with a stated reason (see below) --
close/trim order construction, tranche-splitting, and the placebo agent move to Phase 4.

A scope correction found while researching this phase, before writing any code: the
roadmap said "real bracket/stop orders," but `TradeProposal` only has an
`invalidation_price` (a stop) -- no take-profit/target field anywhere in the schema or
the brief. Alpaca's `OrderClass.BRACKET` requires *both* legs; the correct mechanism is
`OrderClass.OTO` ("one triggers other") with a stop-loss leg only -- confirmed by
inspecting the installed `alpaca-py` package directly (`LimitOrderRequest`/
`StopLossRequest`/`TakeProfitRequest.model_fields`, `list(OrderClass)`). Also confirmed
directly against Alpaca's docs this session, not assumed: bracket/OCO/trailing-stop
orders are wash-trade-exempt (a direct quote resolves the exact follow-up
`docs/alpaca_notes.md` flagged for "Phase 3" -- OTO isn't named in that quote, so its own
exemption stays an open "still to verify" item); and whether an OTO stop-loss leg accepts
a fractional entry qty is undocumented anywhere checked, resolved conservatively (always
whole shares for this order type). Built:

- [x] `execution/broker.py::OrderRequest` gets three new optional fields
      (`order_class: Literal["simple", "oto"] = "simple"`, `stop_loss_price`,
      `stop_loss_limit_price`), every one defaulting to today's exact behavior -- the
      only other constructor (`execution/orders.py::build_orders`) never sets them, so
      s0/s3 are unaffected. `execution/alpaca_broker.py::submit_order` adds
      `order_class=OrderClass.OTO`/`stop_loss=StopLossRequest(...)` to its kwargs only
      when `order_class == "oto"`. `list_open_orders`'s existing `OrderClass.SIMPLE`-only
      filter is untouched -- S4 tracks its own open orders via
      `IntradayLedger.open_orders()` (Phase 0) instead of needing that shared method's
      filtering behavior to change. `FakeBroker.submit_order` needed no code change at
      all (it already only reads the fields it uses) -- just a docstring note that it
      fills the entry leg like a simple order and does not simulate the stop leg
      triggering later (Alpaca's own server-side job, and `AlpacaPaperBroker` is already
      only exercised by opt-in network-marked integration tests, not mirrored here).
- [x] `execution/orders.py::_collared_limit_price` renamed to `collared_limit_price`
      (public, no behavior change) so `intraday/orders.py` reuses the exact formula
      instead of a second copy.
- [x] `intraday/pretrade.py::IntradayPretradeContext`/`check_intraday_pretrade` -- mirrors
      `execution/pretrade.py`'s exact shape reading `config.intraday` instead of
      `config.paper` (not generalized into one shared function, for the same reason
      `IntradayConfig` is already a top-level sibling rather than nested in
      `PaperConfig`). Kill switch, `trading_enabled`, account match against
      `registered_account` (singular -- never interpolating the account number itself
      into the failure message, the same rule `execution/pretrade.py` already follows),
      account `ACTIVE`, market open. No configured start/cutoff execution window -- S4's
      time-of-day gating is the job schedule itself (Phase 4), not a second, overlapping
      window check here.
- [x] `intraday/orders.py::intraday_client_order_id`/`build_entry_order`/
      `submit_order_with_fault_isolation`. `build_entry_order` only builds `open`/`add`
      orders (raises `ValueError` otherwise), sizes to whole shares always (never
      notional), reuses `collared_limit_price` for the limit price (collared off
      `risk.equity_cost_bps_per_side` -- the same modeled per-side friction
      `validate_proposal`'s edge-vs-cost check already uses, rather than a second,
      separately-tuned execution parameter with no evidence behind it), and attaches a
      plain stop (not stop-limit -- a stop-limit can fail to fill in exactly the
      fast-moving market a stop protects against) at `invalidation_price`.
      `submit_order_with_fault_isolation` mirrors `live/execute.py::run_execute`'s
      per-order try/except exactly (`TimeoutError` falls back to lookup;
      `RateLimitError`/`WashTradeError`/`AccountBlockedError`/`MarketClosedError` are
      caught, logged as an incident, and return `None` rather than raising) -- one bad
      order in a cycle's batch never takes down the rest of it. Took a `cycle` parameter
      out of the plan's original signature during implementation: nothing in
      `IntradayLedger.record_order`/`record_incident`/`record_fill` takes a `cycle`
      argument, so it would have been dead weight.
- [x] **Deliberately deferred, not half-built** (each moved into Phase 4's description
      above): `close`/`trim` order construction (needs a *real* current share quantity
      from an actual broker call, and `trim`'s `size_pct` semantics -- percent of equity
      like open/add, or percent of the existing position -- are genuinely undefined
      anywhere in this project yet); tranche-splitting ("default to two tranches") --
      a per-cycle flow-control decision that belongs with `jobs.py`'s loop, not
      order-construction math with no loop to sit inside yet; the placebo agent
      (`placebo.py`) on a shadow ledger -- needs a real watchlist source (Phase 4's
      `research` job) and the brief never specifies the shadow ledger's fill mechanics.
- [x] `docs/alpaca_notes.md`: wash-trade exemption item confirmed with a direct quote
      (noting OTO isn't named in it); two new "still to verify" items (OTO + fractional
      qty; OTO's own wash-trade exemption).
- [x] Tests: `tests/test_intraday_pretrade.py`, `tests/test_intraday_orders.py` (all
      against `FakeBroker`, no network), plus extensions to `tests/test_execution_orders.py`
      (new `OrderRequest` field defaults), `tests/test_alpaca_broker.py` (OTO kwargs,
      mocked `TradingClient`), `tests/test_execution_fake_broker.py` (an OTO request
      fills its entry leg like a simple order), and `tests/test_imports.py`.

**Remaining prerequisites** -- what Phase 0 didn't (and couldn't) resolve:

1. **The owner still needs to actually create the second Alpaca paper account** and add
   `ALPACA_S4_API_KEY`/`ALPACA_S4_SECRET_KEY` as GitHub repo secrets, and a new
   `ANTHROPIC_API_KEY` (Console/metered, per the `--bare` decision below) scoped *only* to
   the CI environment -- confirmed in `code.claude.com/docs/en/authentication` that
   setting it locally would silently flip the owner's own interactive Claude Code usage to
   metered billing too. The code side (parameterized `from_env`) is done; these are
   real-world owner actions, not code.
2. ~~Where the loop runs~~ -- **resolved**, see the correction above.
3. **A per-day model-call spend cap** -- the mechanism is built (`intraday/budget.py`), but
   the $10 default is a placeholder. Recalibrate from real Stage-1 shadow-mode logging.
4. **Alpaca's news API is not wired anywhere** (no `NewsClient` under `src/`), and whether
   the Researcher reads anything beyond it and this project's own data is still
   unanswered -- still recommending "nothing beyond Alpaca news + existing data" as the
   Stage-1 default, expand later only with evidence.
5. ~~`.claude/agents/`~~ -- **resolved**, see the correction above (`--append-system-
   prompt-file`, a Phase 2 concern).
6. ~~The headless flag set beyond `--bare` is unverified~~ -- **mostly resolved in
   Phase 2**: every flag `intraday/llm.py` builds an argv from (`--output-format json`,
   `--max-turns`, `--max-budget-usd`, `--json-schema`, `--append-system-prompt-file`,
   `--permission-mode`/`--permission-prompts`) was re-verified directly against current
   docs and is wired and tested against a boundary-mocked subprocess. What's still
   unverified is the CLI's *actual* JSON output field names against one of this
   project's real calls -- that's blocked on prerequisite 1 below, not on more code.

**Rollout stages from the brief** (none started; each gated on the owner, never automatic):

- [ ] Stage 1 -- shadow, 2 weeks. Full schedule, no orders submitted, proposals filled
      against the cost model, playbook frozen for a clean baseline.
- [ ] Stage 2 -- equities, half budget, 2 weeks.
- [ ] Stage 3 -- equities, full budget.
- [ ] Stage 4 -- options in shadow, 2 weeks (priced and logged, not traded).
- [ ] Stage 5 -- options enabled, only if the shadow results and both options gates pass,
      and only with the owner's explicit approval. Never live at any stage.

## Known gaps (deliberate, with reasons)

- [ ] **Cross-run fit caching.** Fitted parameters are cached in memory within a run (one
      fit serves every horizon, roughly a 4x saving) and every fit is recorded in the run
      manifest, but nothing is reloaded across runs. Reconstructing a GARCH from stored
      parameters risks a silent mismatch between the stored and rebuilt model, which is a
      worse failure than a slow rerun. The full 15-ticker, 3-model, 4-horizon backtest
      takes about 3.5 minutes, so the payoff is small.
- [ ] **No heartbeat / dead-man's-switch for the paper-trading workflow itself.** Phase 7's
      alerting (GitHub's own failure-notification email, triggered by a `paper` CLI command
      exiting non-zero) only fires for a run that actually executed and hit a problem. It
      cannot detect the workflow being disabled, a GitHub Actions outage, or the cron simply
      never firing -- catching an *absent* run needs an external always-on watcher, exactly
      the new-dependency cost the owner chose to avoid when picking the alert channel
      (2026-09-19: GitHub's built-in email, no new secret or service). Revisit if this
      project ever adopts a paid alerting channel.
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

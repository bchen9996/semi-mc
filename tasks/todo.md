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
- [ ] **Phase 8 -- rollout.** Dry run -> sandbox failure injection -> go-live checklist ->
      enable S0/S3 first.

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

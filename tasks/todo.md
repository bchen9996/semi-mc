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
      job's decision at day's end, now Phase 5, not a per-proposal check) and real
      stop-out detection (`last_stopout_at` is a fixture-supplied dict for now -- Phase 3
      builds real order/fill handling that would actually populate it).
- [x] 26 tests (`tests/test_intraday_proposals.py`): schema (missing required fields,
      length/range bounds, unknown-field rejection, `hold`/`no_trade`/`close`/`trim` need
      none of `open`'s fields) and one test per validator check, including that a `close`/
      `trim`/`hold`/`no_trade` bypasses every check that would otherwise fail.

**Phase 2 -- context assembly and the headless invocation wrapper, built 2026-09-20.**
Still no order and no job -- `jobs.py` is now Phase 5. Every flag below was re-verified
directly against `code.claude.com/docs/en/headless`, `.../cli-reference`, and
`.../agent-sdk/cost-tracking` before writing any code (not recalled from memory), which
surfaced a real upgrade over what Phase 0 assumed: **`--max-budget-usd` is real and is
per print-mode call**, so the CLI itself enforces `config.intraday.budget.
max_cost_usd_per_call` natively, not just an after-the-fact check. Built:

- [x] `intraday/context.py::build_context`/`context_hash` -- a generic, role-agnostic
      envelope that labels caller-specified keys as untrusted (`payload["untrusted"]`,
      per the brief's "every headline...is data, never instructions") and hashes the
      canonical payload for `model_calls.prompt_hash`. Does **not** strip or neutralize
      anything inside an untrusted section yet -- that's now Phase 7's job (the
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
      real injection payloads in Phase 7 -- these are functional drafts, not stand-ins.
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
      account" pattern. Prompt-injection hardening of the three role files is now Phase 7.

**Phase 3 -- order construction, the protective stop, and fault-isolated submission,
built 2026-09-21.** Still no job -- `jobs.py` is now Phase 5 (see below). **Narrowed from
the roadmap's original one-line description**, each narrowing with a stated reason (see
below) -- close/trim order construction, tranche-splitting, and the placebo agent move
out of this phase. (That single "Phase 4" bucket was itself split further once Phase 4
was actually scoped, 2026-09-21 -- see that section: close/trim construction is Phase 4,
`jobs.py`/CLI/the GH Actions workflow/tranche-splitting is Phase 5, and the placebo agent
is Phase 6, later Phase 7 once Phase 5 itself split further -- see the Phase 5 section
below.)

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
      time-of-day gating is the job schedule itself (Phase 5), not a second, overlapping
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
- [x] **Deliberately deferred, not half-built** (see the Phase 4 section below for where
      each actually landed): `close`/`trim` order construction (needed a *real* current
      share quantity from an actual broker call, and `trim`'s `size_pct` semantics --
      percent of equity like open/add, or percent of the existing position -- were
      genuinely undefined anywhere in this project yet -- both resolved in Phase 4);
      tranche-splitting ("default to two tranches") -- a per-cycle flow-control decision
      that belongs with `jobs.py`'s loop (now Phase 5), not order-construction math with
      no loop to sit inside yet; the placebo agent (`placebo.py`) on a shadow ledger --
      needs a real watchlist source (Phase 6's `research` job, built 2026-09-21 -- see
      that section below) and the brief never specifies the shadow ledger's fill mechanics.
- [x] `docs/alpaca_notes.md`: wash-trade exemption item confirmed with a direct quote
      (noting OTO isn't named in it); two new "still to verify" items (OTO + fractional
      qty; OTO's own wash-trade exemption).
- [x] Tests: `tests/test_intraday_pretrade.py`, `tests/test_intraday_orders.py` (all
      against `FakeBroker`, no network), plus extensions to `tests/test_execution_orders.py`
      (new `OrderRequest` field defaults), `tests/test_alpaca_broker.py` (OTO kwargs,
      mocked `TradingClient`), `tests/test_execution_fake_broker.py` (an OTO request
      fills its entry leg like a simple order), and `tests/test_imports.py`.

**Phase 4 -- closing and trimming a position, and keeping it protected, built
2026-09-21.** Still no job or loop -- `jobs.py` is now Phase 5. This session's request to
plan "Phase 4" (the old bucket: close/trim, tranches, the placebo agent, `jobs.py`, CLI,
the GH Actions workflow) was split three ways after two research passes (one mapping the
existing s0/s3 job/CLI/workflow pattern, one on what close/trim/tranches/placebo actually
need): this phase is close/trim construction and keeping a position protected, buildable
now with what already exists; `jobs.py`/CLI/the workflow/tranche-splitting move to Phase
5 (needs Phase 4's primitives to call, and tranches are inherently loop-shaped); the
placebo agent moves to Phase 6 (needs Phase 5's `research` job for a real watchlist) --
**since renumbered to Phase 7 once Phase 5 itself split again and `research` became its
own Phase 6 (see below); the dependency itself is unchanged, only the numbers.**
Old Phase 5/6/7 (injection defenses; reflections/EOD/`PLAYBOOK.md`; rollout stages)
shift to 7/8/9 -- content unchanged, only numbers (**since renumbered again to 8/9/10,
see the Phase 5 section below**).

**Phase 5 itself split again once it was actually scoped (see the Phase 5 section
below)**: `research` needed a whole new data source (a news client) that didn't exist,
so it moves to its own new Phase 6, bumping placebo to 7, injection defenses to 8,
reflections/EOD/`PLAYBOOK.md` to 9 (now also owning `eod_review`/`weekly_review`'s
scheduling, not just their content), and rollout to 10.

A real gap found while scoping `close`'s mechanics, before writing any code: once an
`open`/`add` order fills, its OTO stop-loss leg becomes its own resting order at Alpaca,
and `AlpacaPaperBroker._translate_order` was dropping it entirely -- nothing could see a
filled entry's protective stop, so nothing could find and cancel it before a close or
trim. Confirmed directly against the installed `alpaca-py` package:
`alpaca.trading.models.Order.legs: Optional[List[Order]]` exists and was simply never
read. Also: only bracket/OCO/trailing-stop orders are confirmed wash-trade-exempt at
Alpaca (Phase 3's own direct quote) -- a plain "simple" close/trim order is not, so
canceling that resting leg first is a correctness requirement, not cleanup. Built:

- [x] `intraday/proposals.py`: `trim` now requires `size_pct` (previously optional and
      unused by `validate_proposal`, which skips every risk check for `close`/`trim`).
      Resolved as the same unit `open`/`add` already use (percent of equity, not shares,
      not a second meaning for one action) -- no existing test broke, every `trim`
      anywhere in the test suite already supplied a `size_pct`. `close` still needs
      nothing from the model itself; its qty/side come from the real position.
- [x] `execution/broker.py::Order` gains `legs: list[Order] | None = None` (additive,
      defaults to `None` for every order this project has ever built).
      `AlpacaPaperBroker._translate_order` now recursively translates `.legs`.
      `FakeBroker.submit_order`: an `order_class="oto"` submission now also synthesizes
      a second `Order` for the stop-loss leg (its own id, `client_order_id` suffixed
      `"::stop"`, opposite side, `type="stop"`), registers it in `self._orders` so
      `cancel_order`/`get_order_by_client_id` work on it like any other order, and marks
      it open in `_open_sides` too (so a `wash_trade_symbols`-flagged collision is
      genuinely simulated, not just structurally present) -- the first time `FakeBroker`
      models a multi-leg order's child at all.
- [x] `intraday/ledger.py`: `orders` gets a `leg TEXT NOT NULL DEFAULT 'entry'` column
      (safe to add directly -- S4 has never gone live, nothing has written this table for
      real) and `record_order(..., leg="entry"|"stop")`. New `open_stop_leg_for(symbol)`
      -- the most recently recorded still-open stop leg for a symbol, or `None`.
      Deliberately singular, not a list: nothing before Phase 5's tranche-splitting can
      produce more than one open entry (and stop) per symbol at once.
- [x] `intraday/orders.py::find_and_cancel_resting_stop` -- looks up
      `open_stop_leg_for`, cancels it at the broker if found (tolerating an
      already-canceled/missing order the same forgiving way real Alpaca's own
      `cancel_order` already is), returns its `client_order_id` or `None`.
      `build_close_order` -- qty/side from a real `Position`, never the proposal;
      `order_class="simple"`, no new stop leg. `build_trim_order` -- `size_pct` percent
      of equity, capped at what's actually held (same defensive cap
      `execution/orders.py::build_orders` already uses for its own sell-down case).
      `build_replacement_stop_order` -- a plain standalone `type="stop"` order (not
      `"oto"`) so a trimmed remainder is never left resting unprotected.
      `submit_order_with_fault_isolation` (Phase 3) extended, not re-signatured: a
      successful `"oto"` submission's `.legs` are now also recorded as their own
      `leg="stop"` ledger rows automatically.
- [x] **Deliberately not this phase**: the actual sequencing a close/trim needs
      (cancel the resting stop -> submit the close/trim -> for a trim, submit the
      replacement stop) is Phase 5's `cycle` job's work, not built here -- same reasoning
      Phase 3 left "validate -> build -> submit" sequencing to Phase 5. Multiple
      simultaneous open stop legs per symbol (once tranching exists) is a known,
      disclosed limitation of `open_stop_leg_for`'s "singular, most recent" design.
- [x] `docs/alpaca_notes.md`: no changes needed this phase (Phase 3 already recorded the
      wash-trade-exemption quote and the fractional-qty "still to verify" item this
      phase's design leans on).
- [x] Tests: extended `tests/test_intraday_proposals.py` (trim requires `size_pct`),
      `tests/test_execution_fake_broker.py` (the synthesized stop leg is real and
      cancelable), `tests/test_alpaca_broker.py` (recursive leg translation),
      `tests/test_intraday_ledger.py` (`leg` column round-trip, `open_stop_leg_for`),
      `tests/test_intraday_orders.py` (`find_and_cancel_resting_stop`,
      `build_close_order`, `build_trim_order`, `build_replacement_stop_order`). Re-ran
      `tests/test_live_execute.py` (s0/s3) to confirm `Order.legs` is a no-op for them.

**Phase 5 -- the mechanical trading loop (`jobs.py`, CLI, GitHub Actions workflow,
tranche-splitting), built 2026-09-21.** The request was "go ahead to Phase 5," which the
existing roadmap described as one bucket (all eight of the brief's named jobs, CLI
wiring, the workflow, tranches). Two more research passes (mapping the exact s0/s3 job/
CLI/workflow pattern; inventorying everything Phases 0-4 already built) found the same
kind of further split Phase 4 went through: `research` needs a whole new capability (a
news client) that doesn't exist yet, and `eod_review`/`weekly_review`'s real content was
already assigned to the old Phase 8 (reflections/EOD/`PLAYBOOK.md`), not this bucket. So
this phase narrows to the loop that runs *given* a watchlist -- `preflight`, `preopen`,
`open_batch`/`cycle` (one shared implementation), `carry`, plus a new `monitor` job -- and
`research`/`eod_review`/`weekly_review` move out entirely (see the roadmap renumbering
above the Phase 4 write-up). Tested end to end against `FakeBroker`, a fake
`ClaudeCliClient`, and a manually-seeded `watchlist` table, since `research` producing a
real one is Phase 6's job, not this one -- `ledger.watchlist_for(as_of)` is already a
decoupled read, the same way `execute.py` never cares how `decide.py`'s decision was
produced.

Three findings shaped the design, all found before writing code:

1. **The brief's "minute monitor" doesn't fit a GitHub-Actions-cron architecture as
   literally described** (no persistent process, s0/s3 don't run one either). Fix:
   invalidation/time-stop enforcement becomes its own job (`intraday/monitor.py::
   check_invalidation_and_time_stops`), cheap enough (no LLM call) to run on a tight
   5-minute cron -- closer to the brief's real safety intent than folding the same check
   into the 20-minute Trader `cycle` alone. `run_cycle` calls it first, so the Trader is
   never asked about a position that should already be gone.
2. **`IntradayPretradeContext.config`'s type hint (`IntradayConfig`) was already wrong
   before this phase** -- its body calls `context.config.path(...)` and
   `context.config.intraday.*`, which only works if the real argument is the *full*
   `Config` (confirmed: every existing test already passes the full `Config`, working by
   accident since Python doesn't enforce dataclass field types). Rather than change that
   already-shipped, already-tested contract, every `jobs.py::run_*` function takes the
   full `Config` too (matching `live/{decide,execute,mark}.py`'s own actual convention,
   which this session had initially misread as "narrow `IntradayConfig` everywhere" from
   Phase 3-4's order builders alone) and narrows to `config.intraday` itself wherever a
   Phase 2-4 helper expects the narrower type.
3. **A `FakeBroker` gap found while testing the trim-and-reprotect path**: a standalone
   `type="stop"` order (S4's own replacement stop after a trim) fell through to the same
   fill logic as a market/limit order and filled *immediately* -- inconsistent with how
   `FakeBroker` already refuses to simulate an OTO leg's own stop ever triggering. Fixed
   in `execution/fake_broker.py`: any order with `type in ("stop", "stop_limit")` now
   rests at `status="new"` regardless of whether it arrived as an OTO leg or a standalone
   request, generalizing the existing rule rather than leaving the standalone case wrong.

Built:

- [x] `config.py::IntradayRiskConfig` gains `entry_tranches` (default 2, per the brief),
      `max_spread_bps`/`max_quote_age_seconds` (both placeholders -- the brief names them
      as configurable but gives no numeric default). Added to both `config/default.yaml`'s
      and `config/paper.yaml`'s `intraday:` blocks (still hand-duplicated).
- [x] `execution/market_data.py::AlpacaMarketDataClient` gains `latest_quote` (the whole
      nearest `Quote` -- bid, ask, its own timestamp) alongside the existing `arrival_price`
      (just the midpoint), refactored to share one `_nearest_quote` lookup. The spread gate
      needs bid/ask/timestamp; a plain midpoint can't provide any of them.
- [x] `intraday/pretrade.py::check_spread_gate` -- a per-candidate (not per-sleeve) gate:
      rejects a spread over `max_spread_bps` or a quote older than `max_quote_age_seconds`,
      normalizing a naive `now`/`quote_time` to UTC the same way `arrival_price` already
      does. Called by `preopen` (log-only) and `run_cycle` (actually drops a candidate) --
      never trusted from one job to the next.
- [x] `intraday/monitor.py` (new) -- `check_invalidation_and_time_stops`: for every held
      `Position`, recovers the proposal that opened it (`ledger.latest_filled_entry_for`
      -> `ledger.get_proposal`) and closes it if price has crossed `invalidation_price` or
      `time_stop_minutes` has elapsed since the first fill. An invalidation-triggered close
      (never a time-stop close) also logs an incident prefixed `"stopout:{symbol}"` --
      the brief's cooldown rule is specifically about being stopped out, not any close.
- [x] `intraday/ledger.py`: `orders` gains `tranche_index`/`tranche_total` columns
      (default 1/1, safe to add directly). New methods: `get_proposal(id)`,
      `latest_filled_entry_for(symbol)` (the most recent *filled* `leg='entry'` row --
      deliberately excludes `leg='exit'` rows, or a close/trim would masquerade as a fresh
      entry), `pending_tranche_for(symbol)` (finds the most recent filled entry **first**,
      then checks *its own* `tranche_index < tranche_total` -- filtering that condition
      inside the SQL `WHERE` clause was tried first and is wrong: it keeps matching
      tranche 1's own never-changing row forever, even after a later tranche already
      closed out the split), `record_session_open_equity`/`session_open_equity` (a new
      `session_state` table, idempotent by `as_of` -- the day's starting equity must never
      drift once trading begins), `last_stopout_times` (reads back `monitor.py`'s own
      `"stopout:{symbol}"` incident prefix -- the first real caller of
      `ValidationContext.last_stopout_at`, previously only ever fixture-supplied).
- [x] `intraday/orders.py`: `intraday_client_order_id`/`build_entry_order` gain a
      `tranche: int = 1` parameter (appended to the id only when `!= 1`, so every existing
      id and test is unaffected) -- tranche math (dividing `size_pct` by
      `risk.entry_tranches`) happens in `jobs.py`, not here, same "primitives don't know
      about sequencing" split Phase 3/4 used. `submit_order_with_fault_isolation` gains
      `leg`/`tranche_index`/`tranche_total` parameters -- `leg` matters beyond bookkeeping:
      without it, a close/trim submitted through here would default to `leg="entry"` and
      corrupt `latest_filled_entry_for`.
- [x] `intraday/jobs.py` (new) -- `run_preflight` (a canary, not a gate: every later job
      re-checks its own gates fresh regardless), `run_preopen` (refresh + spread gate,
      logging only), `run_cycle` (shared by `open_batch`/`cycle`: pretrade gate -> monitor
      -> tranche continuation -> `build_trader_context`/`invoke_role` -> per-proposal
      validate/build/submit, reusing every Phase 2-4 primitive with no new order-
      construction math), `run_carry` (closes any position without a multi-day thesis on
      file, then closes the lowest-confidence carried positions one at a time until back
      under `overnight_gross_cap_pct`).
- [x] `cli.py::intraday_app` -- `preflight`/`preopen`/`monitor`/`open-batch`/`cycle`/
      `carry`, mirroring `paper_app`'s exit-code convention exactly (a decline/skip is
      exit 0; a real order failure raises `typer.Exit(code=1)`).
- [x] `.github/workflows/intraday_trading.yml` (new) -- one workflow, one `schedule:`
      block per job (six cron entries), the identical fetch/reset/re-run/commit/push
      retry loop `paper_trading.yml` already uses, all six jobs sharing one concurrency
      group (`intraday-trading-ledger-write`) since -- unlike `paper_trading.yml`'s two
      ledger-writing jobs -- all six of these touch the same `ledger/intraday.sqlite3`.
      `cycle`/`monitor` pass a `--cycle` label derived from the current UTC time, not a
      fixed literal, since two different runs on the same day need distinct idempotency
      keys or the second run's orders would look like duplicate resubmissions of the
      first's. UTC cron times approximate the brief's Central-time schedule, not DST-aware
      (same documented approximation `paper_trading.yml` already accepts).
- [x] **Deliberately deferred (not this phase)**: `research`/`eod_review`/`weekly_review`
      jobs and their content (Phase 6/9); `preopen`'s headline refresh (Phase 6); true
      event-triggered early cycles -- breaking news, a >1.5x-expected-move trigger --
      (`monitor`'s 5-minute cadence covers the reaction, not the trigger, until Phase 6
      exists); options (unchanged, still gated off).
- [x] Tests: `tests/test_intraday_monitor.py` (new), `tests/test_intraday_jobs.py` (new,
      the largest file this phase -- covers tranche-splitting across two `run_cycle`
      calls, the trim-then-reprotect sequence, monitor-before-Trader ordering, a malformed
      proposal, budget exhaustion, and `run_carry`'s cap logic), `tests/test_cli_intraday.py`
      (new, one smoke test per command), plus extensions to `tests/test_intraday_ledger.py`,
      `tests/test_intraday_pretrade.py`, `tests/test_execution_market_data.py`, and
      `tests/test_execution_fake_broker.py`'s docstring for the stop-order fix above.

**Phase 6 -- the Researcher's news source and the `research` job, built 2026-09-20.** The
request was "go ahead to Phase 6," the item Phase 5's own planning had already split out:
`research` needed a whole new capability -- a news source -- that didn't exist anywhere in
this repo. Confirmed directly (not assumed) before writing code: the installed `alpaca-py`
package has `alpaca.data.historical.news.NewsClient`, never used anywhere in this project
before; `intraday/ledger.py` already had unused `briefs`/`watchlist`/`headlines_seen`
tables and a `prompts/researcher.md` role prompt from Phase 0, whose contract already
matched `record_watchlist_item`'s existing parameters exactly; `intraday/llm.py::
invoke_role` already resolved `config.models.researcher` and only needed its JSON-schema
selection generalized past `role == "trader"`.

Two scope decisions, made and documented rather than silently built or silently skipped:

1. **Analyst actions are not built** -- no data source for this exists anywhere in the
   project, and the brief itself lists this as an open question ("should the Researcher
   read anything beyond Alpaca's news API and the project's own data"), not a settled
   requirement. Adding a data source is CLAUDE.md's own "ask first" boundary.
2. **"The project's forecast and volatility model" is a realized-vol snapshot
   (`data/realized.py::realized_variance`/`garman_klass` on cached OHLC), not a live daily
   re-fit of GARCH/HAR/regime models for the whole universe.** No existing job refreshes
   those models daily, and fitting them fresh every morning for the whole universe is a
   real latency/cost addition deserving its own design pass. `garman_klass` needs no fit
   and reuses data already being pulled for the earnings calendar, so it ships as an
   honest, labeled approximation rather than an omission.
3. **`preopen`'s "drops candidates whose premise broke overnight" is read as "surfaces new
   headlines," not "auto-drops via keyword/sentiment heuristics."** The brief calls
   `preopen` a *deterministic* refresh; judging whether a headline breaks a thesis is
   exactly the kind of call this project reserves for the model. `preopen` now fetches and
   logs new headlines per watchlist symbol (deduped against `headlines_seen`), and
   `run_cycle`'s `build_trader_context` call threads them into the Trader's own context so
   the model -- which already re-evaluates every 20 minutes -- can act on them instead.

Built:

- [x] `execution/news.py` (new) -- `AlpacaNewsClient`, mirroring `AlpacaMarketDataClient`'s
      shape (plain constructor over an injected SDK client, `from_env()` for real use), but
      defaulting `from_env`'s key pair to S4's own (`ALPACA_S4_API_KEY`/
      `ALPACA_S4_SECRET_KEY`) rather than s0/s3's, since nothing outside S4 will ever call
      it. Verified directly against the installed package: `NewsRequest.symbols` is one
      comma-joined string, not a list; `NewsClient.get_news` returns a `NewsSet` whose
      `.data` is always `{"news": [News, ...]}`, a flat list despite the type hint.
- [x] `config.py::IntradayResearchConfig` (new) -- `news_lookback_hours` (16),
      `preopen_lookback_hours` (2), `earnings_lookahead_days` (5),
      `max_headlines_per_symbol` (5), all starting guesses per the brief's own lack of a
      numeric default. Added to both YAML configs' `intraday:` blocks.
- [x] `intraday/ledger.py`: `headlines_seen` gains nullable `symbols`/`headline` columns
      (safe to add directly -- S4 has never gone live) -- the table previously stored only
      a dedup fingerprint, nothing to actually show the Trader. New
      `recent_headlines_for(as_of, symbols)`, filtering symbol overlap in Python after a
      broad `as_of` pull (same "filter in Python once SQL gets awkward" pattern
      `pending_tranche_for` established in Phase 5). Dedup key is Alpaca's own `News.id`
      (stringified), not a hash of the headline text, which would falsely dedup two
      different articles sharing a headline.
- [x] `intraday/context.py`: `build_researcher_context` (news untrusted; premarket moves/
      upcoming earnings/realized vol are this project's own data, not labeled untrusted --
      the same distinction `build_trader_context` already draws for its own sections).
      `WatchlistCandidate` (a pydantic model, not a `TradeProposal` -- no order/size/side
      fields) and `researcher_output_json_schema()`, generated from it the same way
      `trader_output_json_schema` is generated from `TradeProposal`. `build_trader_context`
      gains a `headlines` parameter (default `[]`), now always labeled untrusted alongside
      `watchlist`/`prices`.
- [x] `intraday/llm.py::invoke_role`'s schema selection generalizes from an `if role ==
      "trader"` ternary to also route `"researcher"` to `researcher_output_json_schema()`;
      the Reviewer (no output contract built yet) still gets none.
- [x] `intraday/jobs.py::run_research` (new) -- once per trading day: the same pretrade
      gate every job re-checks fresh, then an idempotency guard
      (`ledger.watchlist_for(as_of)` already non-empty means a repeat call -- most likely a
      retried GitHub Actions attempt -- and returns without spending anything, the same
      `ON CONFLICT DO NOTHING` reasoning `record_session_open_equity` already uses), then
      gathers news (capped per symbol via `max_headlines_per_symbol`, most recent first --
      every fetched article is still recorded in `headlines_seen` regardless of the cap),
      pre-market moves (`MarketData.spot()` against the shared cache vs. a live Alpaca
      quote), upcoming earnings (`EarningsCalendar.for_ticker`, point-in-time by
      construction), and the realized-vol snapshot, into `build_researcher_context` ->
      `invoke_role("researcher", ...)` -> `record_brief` + `record_watchlist_item` per
      candidate + `record_headline_seen` per article seen. A malformed response or
      `DailyBudgetExhausted` is caught the same way `run_cycle` already handles them.
- [x] `intraday/jobs.py::run_preopen` gains a `news_client` parameter and a
      headline-refresh step before its existing spread gate: a genuinely new article per
      watchlist symbol is recorded and logged as an info incident, never used to drop a
      candidate (see scope decision 3 above). New `PreopenResult.new_headlines` field.
      `run_cycle` now sources `ledger.recent_headlines_for` for the symbols in view and
      passes it to `build_trader_context` -- without this, `preopen`'s/`research`'s
      headline logging would be an audit trail nobody reads.
- [x] `intraday/jobs.py::run_preflight` gains a `news_client` smoke test (mirrors its
      existing market-data one) -- `research` runs less than an hour later and would
      otherwise be the first thing to discover a dead feed.
- [x] `cli.py::intraday_app` gains `research`, plus `_intraday_news_client()` threaded
      into `preflight`/`preopen`/`research`. `research` also builds a project-side
      `MarketData` via `data.load_market_data(cfg, tickers=cfg.universe.tickers)` for its
      own-data inputs.
- [x] `.github/workflows/intraday_trading.yml` gains a `research` job (cron `30 12 * * 1-5`
      = ~07:30 Central, the same fixed UTC-5 offset the other jobs already use), between
      `preflight` and `preopen`. Runs `semimc fetch` first, mirroring `paper_trading.yml`'s
      `decide` job, so the realized-vol/earnings inputs are never more than minutes stale.
      Same shared `intraday-trading-ledger-write` concurrency group as the other jobs.
- [x] **Deliberately deferred (not this phase)**: analyst actions, a live daily model
      re-fit, and keyword/sentiment-based auto-dropping in `preopen` (scope decisions 1-3
      above); `eod_review`/`weekly_review`, the placebo agent, injection-corpus tests,
      DST-aware cron -- unchanged, still Phases 7/8/9/10.
- [x] Tests: `tests/test_execution_news.py` (new), extensions to
      `tests/test_intraday_ledger.py` (`headlines_seen` columns, `recent_headlines_for`),
      `tests/test_intraday_context.py` (`build_researcher_context`, `WatchlistCandidate`,
      the trader-context `headlines` wiring), `tests/test_intraday_llm.py` (the researcher
      schema, the reviewer's lack of one), `tests/test_intraday_jobs.py` (`run_research`
      end to end against the real fixture-backed `MarketData` from `tests/conftest.py`'s
      `market` fixture, plus `run_preopen`'s headline refresh and `run_cycle`'s headline
      wiring), and `tests/test_cli_intraday.py` (one smoke test for `research`).

**Phase 7 -- the placebo agent, T6's control group, built 2026-09-21.** The request was
"go ahead to Phase 7," the item Phase 4/5's own planning had already deferred twice
(needed `jobs.py`'s loop, then Phase 6's `research` job for a real watchlist -- both now
exist). `THESES.md`'s pre-registered T6 defines it exactly: *"identical sizing, execution,
and risk controls as the real S4 loop, but entries drawn at random from the morning
watchlist instead of the Trader's judgment. Runs on its own shadow ledger every day S4
runs, never submitting real orders."*

The one real design problem this phase had that no earlier phase faced: every earlier job
gets its position/equity truth from a *real* broker; the placebo has none, and each GitHub
Actions run is a brand-new process, so something has to persist "what the shadow book
holds" between runs and compute equity/cash the way a real broker automatically would
(confirmed: `FakeBroker.equity`/`.cash` are static dataclass defaults that never move --
only `_apply_fill_to_position` touches `_positions`). Resolved two ways, both verified
against the actual code before writing anything:

1. `FakeBroker` is a plain `@dataclass` (`_orders`/`_positions`/`_open_sides` are ordinary
   fields), so it can be fully reconstructed from a snapshot. New `FakeBroker.to_state()`/
   `.from_state()` make this a proper, public, tested capability rather than reaching into
   "private" fields from another module.
2. Cash/equity are **derived, never persisted**: `IntradayLedger.total_realized_cash_flow()`
   (new -- a `fills JOIN orders` scan, signed by side) plus a configured starting equity
   plus a fresh mark of whatever's still held (falling back to `avg_entry_price` on a
   missing quote) is recomputed every call. This can never drift out of sync with a
   persisted running total, and it's correct regardless of which code path produced a fill
   -- the placebo's own entry, or `monitor.py`/`run_carry` closing one out.

Everything else the brief asks for is **reused unmodified**: `monitor.py::
check_invalidation_and_time_stops` and `jobs.py::run_carry` already operate purely through
the `Broker`/`IntradayLedger` interfaces, so calling them with a reconstructed `FakeBroker`
and a second, placebo-only `IntradayLedger` just works. `proposals.py::validate_proposal`
is reused unmodified too -- this is what makes the risk controls *identical*, not a
parallel reimplementation of them. Confirmed via `execution/pretrade.py::check_kill_switch`
(a standalone function needing no `Broker`) that the placebo needs no real broker even for
its own pretrade gate -- only the kill-switch file shared with the real loop, so an
emergency halt stops the control group too and the comparison stays clean.

Built:

- [x] `execution/fake_broker.py`: `to_state()`/`from_state()` -- JSON-safe orders
      (recursively, including an OTO entry's own resting stop leg), positions, open sides,
      account identity fields. Deliberately excludes equity/cash (see point 2 above).
- [x] `intraday/ledger.py`: new `shadow_broker_state` table (single upserted row) with
      `save_shadow_broker_state`/`load_shadow_broker_state`; `total_realized_cash_flow()`
      (confirmed a resting stop leg never produces a `fills` row, so the join needs no
      `leg` filter to be correct).
- [x] `intraday/jobs.py`: `_turnover_so_far_usd` -> `turnover_so_far_usd` (dropped the
      underscore -- `placebo.py` is a second real caller now, not `jobs.py`-private
      anymore). No behavior change.
- [x] `config.py::IntradayPlaceboConfig` (new) -- `stop_distance_pct` (0.02),
      `time_stop_minutes` (120), `starting_equity` (100,000.0), all placeholders per the
      brief's own lack of a numeric default, same caveat as `max_spread_bps`.
      `stop_distance_pct`'s default is chosen so `risk_per_trade_pct / stop_distance_pct`
      alone doesn't already exceed `single_name_cap_pct` (0.0075/0.02 = 0.375 > 0.15 would
      still bind, but a materially tighter distance would make every single proposal
      trivially over-concentrated). `IntradayConfig` gains `placebo`/`shadow_ledger_path`
      (a separate file, same reasoning `ledger_path` already has for staying separate from
      `paper.ledger_path`). Both YAML configs updated to match.
- [x] `intraday/placebo.py` (new) -- `_mark_to_market` (the one genuinely new piece of
      accounting logic this phase adds); `run_placebo_cycle` (kill switch -> reused
      monitor -> mark-to-market -> one random not-already-held watchlist symbol -> random
      side (a coin flip: no judgment means no directional view either) -> a mechanical
      `TradeProposal` sized to `min(risk_per_trade_pct / stop_distance_pct,
      single_name_cap_pct, 1.0)` -- capped against every known limit up front rather than
      relying on `validate_proposal` to reject an over-concentrated size every cycle ->
      `validate_proposal` (identical risk controls) -> reused `build_entry_order`/
      `submit_order_with_fault_isolation` on acceptance); `run_placebo_carry`
      (mark-to-market, then `run_carry` unmodified -- every placebo proposal leaves
      `horizon` unset, so `run_carry`'s own safe default already makes every placebo
      position intraday-only with no placebo-specific carry rule needed).
- [x] `cli.py`: a nested `placebo` sub-app (`semimc intraday placebo open-batch|cycle|
      carry`) -- structurally, not just conventionally, incapable of a real order: it
      never imports `AlpacaPaperBroker` or `ClaudeCliClient`, only
      `AlpacaMarketDataClient` (for quotes) and `random.Random()` (OS-seeded -- a
      predictable placebo would defeat the point of a control group).
- [x] `.github/workflows/intraday_trading.yml`: no new cron entries or jobs -- the
      `open-batch`/`cycle`/`carry` jobs each gained one more command (the placebo's own,
      right after the real one) and one more `git add` (the shadow ledger file) in the
      same commit, since the placebo shares those three jobs' exact cadence already and
      needs no LLM call and no real broker.
- [x] `THESES.md`'s T6 "Status" line updated: the placebo now runs on schedule, but S4's
      real account is still `trading_enabled: false`, so neither curve has real trades yet
      and no claim changes.
- [x] Tests: `tests/test_execution_fake_broker.py` (`to_state`/`from_state` round-trip,
      including the OTO leg and open-sides survive it, and that equity/cash are
      deliberately excluded), extensions to `tests/test_intraday_ledger.py`
      (`shadow_broker_state`, `total_realized_cash_flow` including the resting-stop-leg
      case), `tests/test_intraday_placebo.py` (new -- mark-to-market against a
      hand-computed figure, a seeded-random accepted proposal, a rejection once a budget
      cap binds, no-op on an empty or fully-held watchlist, the kill switch, a monitor-
      triggered close reflected in the ledger's own cash-flow total, and `run_placebo_carry`
      closing every position), and `tests/test_cli_intraday.py` (one smoke test per new
      command, with a fixture that makes `AlpacaPaperBroker.from_env`/`ClaudeCliClient.
      from_env` raise if ever called -- confirming "never a real order" structurally, not
      just by convention).

**Phase 8 -- injection defenses, built 2026-09-21.** The request was "go ahead to phase
8," the item the roadmap's own renumbering history had already assigned here (search
"renumbered again to 8/9/10" above the Phase 5 write-up). The codebase pointed here
directly: `context.py::build_context`'s own docstring already said *"It does not yet
strip or neutralize anything inside an untrusted section -- that's Phase 8's job."*
`BRIEF_INTRADAY.md`'s "Untrusted content and prompt injection" section is the spec: strip
or neutralize instruction-like content before it enters a prompt, treat urgency framing as
a red flag, log a suspected attempt as an incident with its source, and add it to a test
corpus.

Read every place raw external text already enters the system before writing anything, and
found two things already true and one real gap:

- `llm.py::ClaudeCliClient.call` already denies every tool call outright
  (`--permission-mode dontAsk --permission-prompts none`, no `--allowedTools` for any
  role) -- the brief's "agent's writable paths are limited to reports/ and proposals/"
  bullet is already over-satisfied: no role has any tool access at all, so there's no
  file-write surface to scope. Nothing to build.
- `proposals.py::validate_proposal` + `TradeProposal`'s schema already make *"the executor
  acts only on schema-valid proposals within limits; no text from a source can raise a
  limit, skip a check, or trigger an order by itself"* structurally true, since Phases 1/3
  -- not something this phase adds, just something worth proving with a test (below).
- The real gap: nothing scanned or redacted the free text inside an untrusted section
  before use. `jobs.py::run_research` built `news_sections` straight from
  `article.headline`/`.summary`, and both `run_research` and `run_preopen` persisted
  `article.headline` verbatim via `record_headline_seen` -- which is exactly what
  `recent_headlines_for` later feeds into the Trader's own `headlines` context section.

Built:

- [x] `intraday/security.py` (new) -- a small, inspectable, pattern-based scanner (not an
      ML classifier, matching `pretrade.py`'s own plain-rule-check style), explicitly a
      placeholder heuristic pending real Stage-1 shadow-mode logging to recalibrate
      false-positive/negative rates against, the same caveat this project already gives
      `IntradayPlaceboConfig`'s defaults. Four categories: `instruction_override`,
      `fake_role_message`, `direct_trade_command`, `urgency_framing`.
      `sanitize_text` redacts the matched *phrase*, not the whole text -- the brief asks to
      neutralize instruction-like content, not discard real market information, and a
      false positive (e.g. "insiders bought 10,000 shares") only loses one clause, not the
      whole headline. `sanitize_and_log` is the one function every real call site uses:
      redacts, then logs one `"warning"` incident per finding -- warning, not error, same
      convention as `check_order_tradable`'s own routine-not-fatal severity choice, since
      an error here would fail a whole day's research over one flagged phrase, against the
      brief's own "no missed jobs" success criterion.
- [x] `intraday/jobs.py::run_research` -- sanitizes every fetched article's
      headline/summary exactly once, up front, into an `{article.id: (clean_headline,
      clean_summary)}` lookup; both the `news_sections` comprehension (over the per-symbol-
      capped subset) and the `record_headline_seen` loop (over the full fetched list) read
      from that lookup instead of touching `article.headline`/`.summary` directly -- one
      scan and one incident per real finding, regardless of how many downstream consumers
      exist. Also sanitizes each accepted `WatchlistCandidate`'s free-text fields
      (`thesis`, `entry_trigger`, `invalidation`, `size_band`) before `record_watchlist_item`
      -- closing a second real gap: if an injected headline's instruction ever got echoed
      into the Researcher's own structured output despite the untrusted-label defense, this
      is what stops it reaching the Trader's `watchlist` context unfiltered.
- [x] `intraday/jobs.py::run_preopen` -- same treatment, simpler shape: only fetches one
      symbol's news at a time and already dedups via `headline_already_seen` before doing
      anything else, so sanitizes right after that dedup check, before both
      `record_headline_seen` and the existing `"info"` new-headline incident.
- [x] `intraday/context.py`: docstring only, no behavior change -- explains sanitization
      now happens upstream, at the single point headline text enters the system (`jobs.py`,
      immediately after `AlpacaNewsClient.get_news`), not inside `build_context` itself;
      `build_context`'s own "untrusted" labeling stays as a second, independent layer for
      whatever the pattern scanner misses, not replaced by it.
- [x] **Deliberately not this phase**: scanning `prices`/numeric fields (no free text, no
      injection surface); any change to `llm.py`'s tool-denial setup (already stricter than
      the brief asks); a config-driven pattern list (this is fixed code logic, like
      `validate_proposal`'s check *logic* itself, not a tunable business parameter like
      `stop_distance_pct`); `eod_review`/`weekly_review`/`review.py`/`PLAYBOOK.md`
      (Phase 9, unrelated).
- [x] Tests: `tests/fixtures/injection_corpus.py` (new) -- the brief's own named examples
      as `(label, text, expected_categories)` tuples (an instruction-override +
      direct-command + urgency combo, a fake system message, a pure urgency example) plus
      one ordinary clean headline as the negative control, a growable artifact per the
      brief's own "add it to the test corpus" instruction, not one-off literals in a single
      test function. `tests/test_intraday_security.py` (new) -- every corpus entry trips
      its expected categories and is fully neutralized; the clean headline produces zero
      findings and is returned byte-for-byte unchanged. Extended
      `tests/test_intraday_jobs.py`: `run_research` and `run_preopen` each redact a
      malicious headline before it reaches the captured model context payload (via the
      file's existing `_recording_llm_client` helper) and the ledger
      (`recent_headlines_for`), logging a `"warning"` incident either way; a "compromised
      model" test proves the actual "zero orders" backstop -- a schema-valid Trader
      proposal whose thesis quotes the corpus's own override phrase, sized to the schema's
      maximum (`size_pct=0.9`), is still rejected by `validate_proposal`'s
      `single_name_cap_pct` check, submitting zero orders. This is the brief's real
      guarantee; the text-pattern scanner alone could never provide it on its own against
      an actually-compromised model, only against the raw external text before any model
      call happens.
- [x] No `.github/workflows/intraday_trading.yml` change and no `THESES.md` change -- this
      phase adds no job, no cron, and doesn't touch T6's claim or status.

**Phase 9a -- mechanical reflections + the eod_review report skeleton, built
2026-09-22.** The request was "go ahead to phase 9," the roadmap's own bucket for
reflections, `eod_review`, `weekly_review`, and `PLAYBOOK.md`. Researching it directly
found this was too large for one phase, the same way Phase 4/5's own buckets kept
splitting once actually scoped -- so it split again: this phase builds everything that's
pure computation; the Reviewer's actual model call moves to **9b**, `weekly_review`/
`PLAYBOOK.md` to **10** (was the second half of "Phase 9"), and the rollout stages
renumber **10 -> 11**.

Two findings, both confirmed by reading the code rather than assumed, drove the split
and the design:

1. `BRIEF_INTRADAY.md`'s "Agent roles" section names exactly three model roles
   (Researcher, Trader, Reviewer) -- no separate "Reflector" role exists, and Reviewer's
   own description already says it "grades process." So a reflection's *qualitative*
   fields (category, process grade, lesson) are the Reviewer's job, batched into its one
   daily `eod_review` call (Phase 9b) -- not a new per-close call, which would also blow
   past the brief's own "roughly 20 cycles a day plus two long jobs" cost shape. This
   phase computes and stores everything that *doesn't* need that judgment call.
2. Finding which entry a given close belongs to can't reliably use the closing order's
   own `proposal_id`: `monitor.py`'s close reuses the *entry's* `proposal_id`, but
   `run_cycle`'s Trader-issued close/trim attaches the *new* close-action proposal's own
   id instead -- confirmed by reading both paths directly. `ledger.py::
   latest_filled_entry_for(symbol)` (already built in Phase 5) is the one uniform way to
   always find the entry, regardless of which path closed the position.

Built:

- [x] `intraday/reflect.py` (new) -- `record_mechanical_reflection`: P&L gross/net of
      modeled cost (reusing `risk.equity_cost_bps_per_side`, the same figure
      `validate_proposal`'s own min-edge gate already enforces), whether the invalidation
      level was respected, and time in trade -- all from `orders`/`fills`/`proposals`
      rows already in the ledger. Category/process_grade/lesson are left `None` for
      Phase 9b. Sums every tranche of a tranched entry (`ledger.orders_for_proposal`,
      new) rather than trusting `latest_filled_entry_for`'s own "most recent" row alone,
      which would silently miss earlier tranches' fills.
      **Found and fixed a real bug during implementation**: an invalidation-triggered
      close's exit price is, by construction, at or through the invalidation level (that
      is what fired it) -- an early version of "was invalidation respected" compared
      exit price to the level unconditionally and scored every real stop-out as a
      violation. Fixed: `close_reason == "invalidation"` is always `True` (the system did
      its job and reacted); every other close reason checks whether the exit price ever
      reached the level at all.
- [x] `intraday/ledger.py`: `orders_for_proposal(proposal_id, leg=None)` (new, for the
      tranche-summing above).
- [x] Wired into all three close-producing paths: `intraday/monitor.py`'s own close,
      `intraday/jobs.py::run_cycle`'s `trim`/`close` branches, and `run_carry`'s two close
      points -- each tagged with its own `close_reason` (`"invalidation"`/`"time_stop"`/
      `"trader_trim"`/`"trader_close"`/`"carry"`). `jobs.py::_original_proposal_payload`
      is now a thin wrapper around `reflect.py`'s own entry-lookup -- one shared
      implementation, not two independently-maintained copies of "find the entry that
      opened this position."
- [x] `intraday/eod_report.py` (new) -- `build_eod_report`/`render_eod_markdown`, mirroring
      `live/report.py`'s own shape exactly (gathers everything through `IntradayLedger`'s
      read methods, no SQL above this layer). Builds the blotter (proposals + reflections),
      P&L gross/net/pessimistic (summed directly from each reflection's own already-
      computed fields, so this can never drift out of sync with what each trade reports),
      and best-effort benchmarks for the day (SOXX buy-and-hold, S0's own day return from
      `live.Ledger`'s snapshots). "Pessimistic" has no existing convention to reuse --
      `semis_paper_trading_brief.md` (what the brief cites for it) was never committed to
      this repo (`CLAUDE.md`'s own dangling-citation note) -- so this treats it as net P&L
      under double the modeled cost assumption, a labeled interpretation, not a fabricated
      figure dressed up as the real thing. Closing market report, process scorecard, and
      playbook changes render as an explicit "not yet built (Phase 9b)" placeholder.
- [x] `config.py::IntradayConfig.reports_dir` (new, default `"reports"`) -- resolved via
      `Config.path()`, not a literal `"reports/..."` string in `cli.py`: exactly the
      failure mode `CLAUDE.md`'s own Gotchas section already documents (`signals
      --holdout` once wrote into the real repo's `research_log.md` this same way).
- [x] `cli.py`: `semimc intraday eod-review`, manual/testing use only -- **not** wired
      into `.github/workflows/intraday_trading.yml`'s cron this phase, since the brief's
      one `eod_review` slot combines this report with the Reviewer's own call, and
      scheduling (and committing) a report missing half its sections isn't worth a cron
      entry before Phase 9b completes it.
- [x] `intraday/jobs.py::_premarket_moves` -> `premarket_moves` (dropped the leading
      underscore, same reasoning Phase 7 already used renaming `_turnover_so_far_usd`) --
      `eod_report.py`'s SOXX benchmark is a second real caller; the function's own logic
      ("prior cached close to whatever quote is live right now") already gives a full
      day's return when called near/after close, not just pre-market.
      **Found and fixed a self-shadowing bug during the rename**: the local variable
      inside `run_research` was also named `premarket_moves`, so after the rename it
      would have shadowed the function itself in the very next line that used it (the
      call into `build_researcher_context`) -- caught by re-running `run_research`'s own
      tests immediately after the rename, before touching anything else, the same
      discipline that caught the identical class of bug in Phase 7.
- [x] Manually ran `semimc intraday eod-review --config config/paper.yaml` against the
      real (still nearly-empty) ledger: rendered a sane, all-"not available"/"no activity"
      report with no crash, including a real S0 benchmark computed from the real
      `paper.sqlite3` and a graceful "SOXX benchmark unavailable" message when
      `ALPACA_S4_API_KEY` wasn't set locally -- the generated file was a disposable
      verification artifact, not committed.
- [x] Tests: `tests/test_intraday_reflect.py` (new, 9 tests) -- gross/net P&L for a long
      and a short, the invalidation-respected fix's own two cases (system-triggered vs.
      discretionary), multi-tranche summing, finding the entry via `latest_filled_entry_for`
      even when the close has a different `proposal_id`, and graceful `None` returns with
      no traceable entry or no exit fill yet. `tests/test_intraday_eod_report.py` (new,
      5 tests) -- empty-day sanity, P&L summed correctly from seeded reflections,
      `s0_day_return`'s prior/today snapshot selection. Extended
      `tests/test_intraday_monitor.py`/`tests/test_intraday_jobs.py` with a reflection
      recorded (with the right `close_reason`) for a monitor close, a Trader close, a
      Trader trim, and a carry close. Extended `tests/test_cli_intraday.py` with an
      `eod-review` smoke test that explicitly redirects `intraday.reports_dir` and
      `paper.ledger_path` to `tmp_path` -- the same gotcha-avoidance the new config field
      itself exists for, verified in the test that would otherwise trip it.

**Phase 9b -- the Reviewer's real `eod_review` call, built 2026-09-21.** The request was
"go ahead and push then move on," picking up immediately after Phase 9a's commit
(`940613e`) landed. This phase gives the Reviewer (`BRIEF_INTRADAY.md`'s third named
model role) its actual call: the qualitative reflection fields Phase 9a's `reflect.py`
left `None` (`category`/`process_grade`/`lesson`), the closing market report, process
scorecard, day attribution, and a playbook proposal -- everything Phase 9a's own
`render_eod_markdown` rendered as an explicit "not yet built (Phase 9b)" placeholder --
plus wiring `eod-review` into the real 15:15 Central cron.

Two findings, both confirmed by reading the brief/code directly, shaped the design:

1. `BRIEF_INTRADAY.md`'s Files section already names `intraday/review.py` ("scoring,
   attribution, calibration") -- a planned file this project hadn't built yet. Rather
   than fold scoring/attribution/calibration into `jobs.py` or `eod_report.py`, this
   phase builds that file, matching the brief's own layout.
2. Attribution and the process scorecard split the same way reflections did in Phase 9a:
   the *numbers* (rejection rate, budget usage, invalidation adherence, a confidence-
   calibration curve, a sector/stock-specific P&L split) are all computable mechanically
   from ledger data already on hand -- no judgment call needed. `review.py` computes
   them; the Reviewer's own call narrates and grades them (`prompts/reviewer.md` updated
   to say so explicitly -- its earlier wording, written before this split existed, read
   as if it did the arithmetic itself).

**A disclosed simplification, stated up front rather than found mid-implementation**:
the brief asks to "regress the day's returns on SOXX," but the ledger retains no
intraday SOXX price *history* (only `latest_quote` at call time), so a genuine per-trade
regression against SOXX's return over that trade's own holding window isn't buildable
from data this project keeps today. `review.py::day_attribution` computes a coarser,
honestly-labeled day-level split instead: a **sector** contribution equal to each closed
position's signed entry-cost-basis notional times SOXX's whole-*day* return (a
beta-of-1 approximation, not a fitted beta), and a **stock-specific** residual (`pnl_net`
minus `sector`) that folds selection and timing into one bucket rather than fabricating a
further split neither the ledger nor a market-data feed supports yet. **Options** is
always 0.0 -- options remain off (rollout stages 4/5, not reached). Same honesty
convention as Phase 9a's "pessimistic P&L": a labeled approximation, not a fabricated
"real" number.

Built:

- [x] `intraday/review.py` (new) -- `process_scorecard` (rejection rate/reasons, budget
      discipline against the configured caps reusing `jobs.py::turnover_so_far_usd`,
      invalidation adherence, today's incidents, a running all-time confidence-
      calibration curve) and `day_attribution` (the sector/stock-specific/options split
      above).
- [x] `intraday/reflect.py`: added a `"qty"` field to the reflection content dict (the
      qty *this event* exited) -- a gap found while designing attribution: `pnl_gross`
      alone can't recover the exited qty after the fact (division by a possibly-zero
      price move), and `day_attribution` needs it times `avg_entry_price` for the
      exited slice's own signed exposure. Stored at the one point it's known
      unambiguously, the same reasoning every other reflection field already follows.
- [x] `intraday/ledger.py`: `update_reflection` (upsert-by-id, the small addition
      Phase 9a's own plan explicitly deferred here), `incidents_for` (date-filtered by
      `created_at`'s own UTC calendar date, same normalize-and-compare pattern
      `eod_report.py::s0_day_return` already uses), `all_reflections` (every reflection
      ever recorded, for the calibration curve -- `reflections_for` only ever returns
      one day).
- [x] `intraday/context.py`: `ReflectionUpdate` (the Reviewer's per-reflection grading
      output), `reviewer_output_json_schema`, `build_reviewer_context` -- mirrors
      `build_researcher_context`'s own pattern, except `untrusted_keys` covers both
      `blotter` and `news` (not just `news`), per `reviewer.md`'s own explicit
      instruction that this applies "just as much to a fill... or a reflection note as
      it does to news copy."
- [x] `intraday/llm.py::invoke_role`: fixed a stub left over from before this phase --
      the reviewer branch hardcoded `json_schema = None`; now calls
      `reviewer_output_json_schema()`, the same shape the researcher/trader branches
      already have.
- [x] `intraday/placebo.py`: `placebo_day_pnl` (today's mark-to-market equity minus the
      placebo's own recorded session-open equity, reusing `_mark_to_market` and a
      `FakeBroker` reconstructed from persisted state -- the same reconstruction every
      placebo job already does). Lives here, not in `eod_report.py`, which deliberately
      never reaches into another module's own internals directly.
- [x] `intraday/eod_report.py`: `do_nothing_return` (the fourth benchmark Phase 9a left
      stubbed -- marks every `open_batch`-cycle entry to market against its own average
      entry price, ignoring whatever happened afterward) plus `DayBenchmarks`/`EodReport`
      gaining the new optional fields (`process_scorecard`, `attribution`, and their
      narratives, `closing_market_report`, `playbook_proposal`) and `build_eod_report`
      accepting them. `render_eod_markdown` reordered to match the brief's own listed
      section order (Blotter, P&L, Attribution, Benchmarks, Closing market report,
      Process scorecard, Playbook changes) and renders real content once available,
      keeping Phase 9a's "not yet built (Phase 9b)" placeholder only when
      `build_eod_report` is called without ever running the Reviewer.
- [x] `intraday/jobs.py::run_eod_review` (new) -- the real job: skips without spending
      anything with nothing to review yet (no proposals, no reflections), idempotent per
      `as_of` (a non-null `category` on today's reflections means it already ran),
      fetches and sanitizes today's news, gathers all four benchmarks and `review.py`'s
      precomputed numbers, calls `invoke_role("reviewer", ...)`, applies
      `reflection_updates` via `update_reflection`, sanitizes every Reviewer-authored
      narrative/report/proposal string before storing or rendering it (`reviewer.md`'s
      same fill/reflection-note callout, applied to the Reviewer's own outputs this
      time), writes a non-null playbook proposal to `proposals/YYYY-MM-DD.md` (plain
      file, matching the brief's own Files section -- diffs *awaiting* weekly review,
      not a DB table), and writes the completed `reports/YYYY-MM-DD/eod.md`.
      `review.py`/`placebo.py` are imported locally inside this function, not at
      `jobs.py`'s own top level -- both modules already import *from* `jobs.py` at
      their own top level, so a top-level import back would be a real circular import;
      deferred to call time (after both are already fully loaded) is the standard, safe
      way around it.
- [x] `config.py::IntradayConfig`: `playbook_path` (default `"PLAYBOOK.md"`) and
      `proposals_dir` (default `"proposals"`) -- same config-relative-via-`Config.path()`
      reasoning as `reports_dir` (Phase 9a), not literals in `jobs.py`. Added to both
      `config/default.yaml` and `config/paper.yaml`.
- [x] `cli.py`: upgraded `intraday eod-review` (Phase 9a's report-only command) to call
      `run_eod_review` -- constructs the LLM client, news client, the paper ledger, and
      the shadow ledger the same way every other credentialed intraday command already
      does, per Phase 9a's own plan note ("9b adds the real cron job once the full
      command exists").
- [x] `.github/workflows/intraday_trading.yml`: added the `eod-review` job (~15:15
      Central, `"15 20 * * 1-5"`), structured exactly like `research`'s (claude CLI
      install step, secrets, the shared `intraday-trading-ledger-write` concurrency
      group). Also commits `reports/`/`proposals/` alongside the ledger -- both new,
      possibly-still-untracked directories, guarded with `[ -d ... ] && git add ... ||
      true` so a quiet day (nothing under either yet) doesn't trip `set -e`.
- [x] `prompts/reviewer.md`: rewrote "What you return" as the real structured-output
      contract (`reflection_updates`/`closing_market_report`/`attribution_narrative`/
      `process_scorecard_narrative`/`playbook_proposal`), and added an explicit
      "`process_metrics` is already computed for you -- narrate and grade these numbers,
      do not recompute them" instruction.
- [x] Manually ran `semimc intraday eod-review --config config/paper.yaml`: confirmed the
      command wires up correctly through Typer (`--help` renders) and fails at exactly
      the same point every other credentialed S4 command already does locally --
      constructing `AlpacaMarketDataClient.from_env` without `ALPACA_S4_API_KEY`/
      `ALPACA_S4_SECRET_KEY` set (this machine never has them, by design -- see the
      "Remaining prerequisites" list below). Confirmed via `git status` that nothing
      leaked into the real repo's `reports/`/`proposals/`/`PLAYBOOK.md`. The full,
      credential-free code path is what the test suite below actually exercises end to
      end, the same reasoning every other credentialed command's own manual-verification
      note in this file already relies on.
- [x] Tests: `tests/test_intraday_review.py` (new, 14 tests) -- `process_scorecard`'s
      rejection rate/reasons, budget discipline against configured caps, invalidation
      adherence, today's-incidents filtering, and `confidence_calibration`'s bucketing
      and multi-day span; `day_attribution`'s sector/stock-specific split for both a long
      and a short, and its graceful `None`s with no SOXX benchmark or no trades. Extended
      `tests/test_intraday_ledger.py` (`update_reflection`, `all_reflections`,
      `incidents_for`), `tests/test_intraday_context.py` (`build_reviewer_context`,
      `ReflectionUpdate`, `reviewer_output_json_schema`), `tests/test_intraday_llm.py`
      (rewrote the old "reviewer omits json_schema" stub test into one asserting the
      real schema now applies), `tests/test_intraday_eod_report.py` (updated the
      empty-day placeholder assertion now that "do nothing" renders real content), and
      `tests/test_intraday_jobs.py` (5 new `run_eod_review` tests: skip-with-nothing-to-
      review, idempotent re-run, malformed-response handling, the full happy path
      seeded via `_seed_open_position` + `run_carry` so the reflection being graded is a
      real one, and a playbook-proposal-written case). Extended
      `tests/test_cli_intraday.py`: replaced the Phase 9a placeholder-report smoke test
      with a skip-on-empty-ledger test plus a full happy-path test asserting the
      Reviewer's real sections render and the graded reflection persists.

**Phase 10 -- weekly_review + PLAYBOOK.md versioning, built 2026-09-21.** The request was
"continue with phase 10," the roadmap's own bucket for `weekly_review`/`PLAYBOOK.md`
left over once Phase 9 split into 9a/9b.

**Confirmed directly from `BRIEF_INTRADAY.md` before designing anything, the same
discipline every prior phase here has used**: the schedule table's `weekly_review` row,
unlike `research (Researcher agent)`/`eod_review (Reviewer agent)`, names no agent, and
"Agent roles" lists exactly three model roles. So `weekly_review` is mechanical, not a
fourth model call. Its brief-given job -- "where playbook changes are actually accepted
or rejected" -- collides with "Ask first: Accepting a playbook proposal" and "accepted
or rejected by me at the weekly review, not automatically": the decision is the user's,
so the Friday cron job can only *surface* pending proposals; accepting/rejecting/retiring
had to become a separate, human-invoked CLI action, run whenever the user actually does
their review (not gated to Friday, never automatic).

Built:

- [x] `intraday/ledger.py`: two new tables. `playbook_entries` -- one row per accept/
      reject decision on a proposal file (`status`, `text`, `source_proposal_path`,
      `adopted_at`, `review_date`, `resolved_at`, `resolution_reason`); retiring an
      already-accepted entry updates that same row rather than inserting a new one.
      `playbook_versions` -- one row per change to `PLAYBOOK.md` as a whole, `id`
      doubling as the version number a proposal stamps itself with, `text` a full
      snapshot (the same "snapshot per change" idiom `snapshots` already uses for
      equity) so a past version's exact wording is always recoverable. New methods:
      `record_playbook_decision`, `get_playbook_entry`, `update_playbook_entry_status`,
      `active_playbook_entries`, `all_playbook_entries`, `record_playbook_version`,
      `current_playbook_version`, and `distinct_trading_days` (every distinct calendar
      date with any recorded activity, across `proposals`/`reflections`/`briefs` -- no
      broker/market-calendar dependency).
- [x] `intraday/proposals` table gained a `playbook_version INTEGER` column, its own
      column rather than a `payload` key -- a real bug found and fixed mid-phase (see
      below).
- [x] `intraday/playbook.py` (new module) -- not one of the brief's own named files
      (`jobs.py`/`context.py`/`proposals.py`/`monitor.py`/`budget.py`/`options.py`/
      `placebo.py`/`review.py`), a disclosed departure from Phase 9b's own "match the
      brief's file list" precedent: none of those names fit a versioned-document store
      with human accept/reject/retire actions on it (`review.py` stays "mechanical
      scoring, attribution, calibration" per its own docstring). Owns
      `render_playbook_markdown`, `pending_proposal_paths` (files under `proposals_dir`
      not yet referenced by any `playbook_entries` row), `accept_proposal`,
      `reject_proposal`, `retire_entry`. The evidence bar ("at least 10 trading days or
      20 relevant trades... unless it is a bug or process fix") is enforced in code via
      `PlaybookEvidenceError`, taking the trading-day/trade counts as explicit arguments
      the *user* supplies on the command line -- never parsed back out of the proposal's
      own freeform text (`playbook_proposal` is deliberately a plain string, per Phase
      9b's own schema choice). `review_date = adopted_at + review_after_days` uses
      calendar days, not trading-session-precise arithmetic -- a disclosed
      simplification, same reasoning `IntradayResearchConfig`'s own lookback windows
      already use, and it avoids needing a `Broker`/market-calendar dependency in a
      command that otherwise only touches the ledger and the filesystem.
- [x] `intraday/review.py`: `weekly_summary` (the most recent 5 distinct trading days on
      record, not calendar Mon-Fri -- a disclosed simplification so a short week
      naturally pulls in one extra prior day instead of under-reporting; also two
      since-inception counters, bounded by `as_of` for reproducibility, so a human can
      eyeball a pending proposal's stated evidence against real totals without the code
      parsing that freeform prose) and `render_weekly_review_markdown`.
- [x] `intraday/jobs.py`: `run_weekly_review` (Friday 15:45) -- no broker, no LLM client,
      no market data, confirmed unnecessary by the "no fourth agent" finding above.
      Writes `reports/YYYY-MM-DD/weekly_review.md`; no idempotency guard needed (pure
      aggregation, no mutation, unlike `eod_review`'s reflection-grading side effects).
      `run_cycle` now stamps the current playbook version onto every recorded proposal
      (accepted or rejected). `run_preflight` reports `ledger.current_playbook_version()`
      via a new `PreflightResult.playbook_version` field -- un-skipping the brief's
      "playbook version loaded" check the function's own docstring had flagged as
      skipped "before Phase 9."
- [x] `config.py` + both YAML files: `IntradayPlaybookConfig` (`evidence_min_trading_days`
      =10, `evidence_min_trades`=20 -- the brief's own named default; `review_after_days`
      =30, a placeholder, same caveat as `IntradayRiskConfig.max_spread_bps`), mounted as
      `IntradayConfig.playbook`.
- [x] `cli.py`: `intraday weekly-review` (thin wrapper, ledger-only). New `playbook_app`
      sub-typer (mirrors `placebo_app`'s own nesting): `intraday playbook pending/accept/
      reject/retire`, all keyword-flag arguments (`--date`/`--entry-id`/...) rather than
      positional, matching this CLI's existing all-flags convention throughout.
- [x] `.github/workflows/intraday_trading.yml`: added the `weekly-review` job (Friday
      ~15:45 Central, `"45 20 * * 5"`) -- no claude CLI install step and no
      `ALPACA_S4_*` secrets (confirmed unneeded: the command opens only the ledger).
      Deliberately did **not** wire `intraday playbook accept/reject/retire` into any
      workflow -- they stay a human running the CLI locally, per the brief's own "Ask
      first."
- [x] **A real bug found and fixed mid-phase**: the first cut stamped
      `playbook_version` directly into the proposal's `payload` dict passed to
      `ledger.record_proposal`. That broke `run_cycle`'s own tranche-continuation path,
      which reconstructs `TradeProposal(**proposal_row["payload"])` from that same
      payload -- `TradeProposal` is `extra="forbid"`, so the added key raised a
      `ValidationError` there, silently caught by an existing `except ValidationError:
      continue`, and a full test-suite run caught it immediately
      (`test_run_cycle_submits_the_next_tranche_on_a_later_call` started failing with
      zero orders submitted). Fixed by giving `proposals` its own `playbook_version`
      column instead, keeping `payload` exactly `TradeProposal.model_dump()` everywhere
      it's stored and reconstructed.
- [x] Manually confirmed `semimc intraday weekly-review --help` / `intraday playbook
      --help` both render correctly, and `git status --short reports/ proposals/
      PLAYBOOK.md` stayed clean (nothing leaked into the real repo) -- this phase never
      constructs a credentialed client at all, so unlike every earlier S4 phase there
      was no local credential wall to hit; the CLI commands themselves were exercised
      directly rather than only via the test suite.
- [x] Tests: `tests/test_intraday_playbook.py` (new, 16 tests) -- rendering, pending-
      proposal discovery, the evidence gate (pass/fail/`--bug-fix`), version bumps
      across accept/reject/retire, and rejecting a retire on an unknown/already-resolved
      id. Extended `tests/test_intraday_ledger.py` (the two new tables' methods,
      `distinct_trading_days`), `tests/test_intraday_review.py` (`weekly_summary`'s
      windowing and `render_weekly_review_markdown`), `tests/test_intraday_jobs.py`
      (`run_weekly_review`'s three surfacing cases, `run_cycle`'s playbook-version
      stamp, `run_preflight`'s new field), and `tests/test_cli_intraday.py`
      (`weekly-review` and all four `playbook` subcommands, including the evidence-gate
      failure exiting 1). Full suite green (`uv run pytest`), `ruff check`/`format`
      clean.

**Phase 11 -- shadow-mode infrastructure (Rollout Stage 1), built 2026-09-22.** The
request was "go ahead to phase 11," the rollout-stages bucket below. Offered a choice
between building this infrastructure or scoping the phase down to an operational audit
(Phase 8's own precedent for paper trading -- dry run, failure injection, a go-live
checklist, with the owner's own later action to actually flip flags); the owner chose
to build the infrastructure.

**Two real gaps found by reading the code directly, not assumed, before designing
anything**:

1. Stage 1 ("everything runs on schedule; no orders are submitted; proposals are filled
   against the cost model") could not run at all: `run_research` and `run_cycle` both
   call `check_intraday_pretrade`, which fails outright unless `trading_enabled=True`
   *and* the broker's real account matches `registered_account`. There was no state
   between "the whole loop is inert" and "submits real orders to Alpaca."
2. **A second, blocking bug**: `check_intraday_pretrade` also fails whenever the market
   isn't open *right now*, and `run_research` calls the identical check -- but research
   runs at 07:30 Central, before the 08:30 open. Once `trading_enabled=True`, research
   would have failed pretrade with "market is closed" on *every single call*, live or
   shadow. Every existing `run_research` test happened to use `FakeBroker()`'s default
   `market_open=True`, which is exactly why this went uncaught until now.

Design, closely mirroring `intraday/placebo.py` (which already solves the identical
"run the real risk/execution primitives against a `FakeBroker`, no real broker at all"
problem for T6's control group):

- [x] `intraday/pretrade.py::check_intraday_pretrade` gained two independent, default-
      preserving parameters: `shadow: bool = False` (skips the account-identity/status
      checks -- meaningless against a `FakeBroker`'s placeholder identity; kill switch
      and `trading_enabled` are never skipped) and `check_market_open: bool = True`
      (skips the closed-market check -- `run_research` passes `False`, the bug fix).
- [x] `intraday/jobs.py`: `run_research` passes `check_market_open=False`; `run_cycle`
      passes `shadow=config.intraday.shadow_mode` (no new parameter on `run_cycle`
      itself -- it already has the full `Config`). `run_preflight` diffs two
      `check_intraday_pretrade` calls (`shadow=False` vs `shadow=True`) to find exactly
      which findings `shadow_mode` would suppress, and routes those to `notes` (routine)
      rather than `failures` -- preflight's own broker is always the real one (it's the
      one command shadow mode doesn't touch), so an account mismatch there is still real
      infrastructure information worth surfacing, just not worth a failure email while
      no real order was ever going to be placed against it. Kill switch/`trading_enabled`
      are untouched by this and keep flowing through the pre-existing, unchanged
      trading_enabled-based routine/failure split. `PreflightResult` gained a
      `shadow_mode: bool` field, mirroring Phase 10's `playbook_version` field.
- [x] **Promoted `placebo.py::_mark_to_market` to `jobs.py::mark_broker_to_market`**
      (same signature, unprefixed) -- the identical "second caller needs the identical
      computation" reasoning `turnover_so_far_usd` was already promoted for once;
      Stage 1's own shadow broker needs the exact recompute-from-ledger-history
      mark-to-market the placebo already built. `placebo.py` now imports it from
      `.jobs`; nothing about the logic itself was placebo-specific.
- [x] `config.py` + both YAML files: `IntradayConfig` gained `shadow_mode: bool = True`
      (safe by default, matching `trading_enabled`'s own safety-first default -- flipping
      `trading_enabled` alone must never be enough to place a real order) and
      `shadow_starting_equity: float = 100_000.0` (the real loop's own Stage-1 book size,
      the same role `IntradayPlaceboConfig.starting_equity` plays for the placebo's book,
      kept as a separate field since the two shadow books are logically distinct).
- [x] `cli.py`: new `_intraday_execution_broker(cfg, ledger, market_data, now)` --
      `_intraday_broker()` (real Alpaca) unless `cfg.intraday.shadow_mode`, in which case
      a `FakeBroker` reconstructed from the *real* ledger's own persisted
      `shadow_broker_state` row (the same generic table/methods the placebo already uses
      on its own separate ledger file) and marked to market. `open-batch`/`cycle`/
      `carry`/`monitor` all switch to this and save state back afterward when in shadow
      mode, mirroring the placebo commands' own explicit load/run/save shape.
      `research`/`preflight` are untouched -- both stay on the real broker always, since
      neither manages simulated positions and both benefit from checking real
      infrastructure health regardless of rollout stage. No workflow-file changes --
      every command's invocation is identical either way; the branch is invisible to
      `.github/workflows/intraday_trading.yml`, and `ALPACA_S4_API_KEY`/`SECRET_KEY` stay
      required regardless of `shadow_mode` (still needed for real market data/news so the
      shadow book marks against real prices).
- [x] **A real test-suite blast radius found while implementing, not just a design
      question**: `shadow_mode` defaulting to `True` meant every existing "tradeable"
      test helper across `test_intraday_jobs.py`/`test_intraday_pretrade.py`/
      `test_cli_intraday.py` would have silently started exercising the *shadow* pretrade
      path (or, for the CLI tests, a freshly-constructed shadow `FakeBroker` instead of
      the test fixture's real-broker patch) instead of what each was actually written to
      test. Fixed by adding `shadow_mode: False` to each shared "tradeable" config
      helper, keeping every existing test's original real-broker behavior intact, with
      shadow mode itself getting new, separately-labeled tests instead.
- [x] **A second, separate real bug found and fixed while wiring `run_preflight`'s own
      shadow-aware split**: an early version widened preflight's routine-vs-failure
      condition to `trading_enabled and not shadow_mode`, which incorrectly routed a
      genuine kill-switch failure to a routine note whenever `shadow_mode=True` (the new
      default) -- caught by a test written for exactly this case
      (`test_run_preflight_still_fails_the_kill_switch_under_shadow_mode`) before it
      shipped. Fixed with the diff-based approach described above, which only ever
      affects the account-identity findings `shadow` actually suppresses.
- [x] **Found mid-verification**: local `main` was 11 real commits behind `origin/main`
      -- the live GitHub Actions workflows (`intraday_trading.yml`'s preflight/research/
      open-batch/cycle/carry, and `paper_trading.yml`'s own decide/execute/mark) had been
      running unattended on schedule since Phase 10 was pushed, exactly as designed
      (`intraday.trading_enabled` stayed `false` throughout, so every S4 job declined
      harmlessly). Fast-forwarded local `main` onto `origin/main` before committing this
      phase, after first discarding a local, unintended `ledger/intraday.sqlite3` diff
      (SQLite's own idempotent `CREATE TABLE IF NOT EXISTS` schema bytes shifting from
      local manual-verification runs against a stale base -- confirmed via row counts
      that no real data was involved before discarding it).
- [x] Manually confirmed `open-batch`/`monitor`/`carry`/`preflight` each reach the
      expected local credential wall (`AlpacaMarketDataClient.from_env`/
      `AlpacaPaperBroker.from_env`) rather than crashing inside the new shadow-broker
      logic itself, and that `git status` stayed clean afterward.
- [x] Tests: `tests/test_intraday_pretrade.py` (+7: `shadow`/`check_market_open` on and
      off, defaults unchanged). `tests/test_intraday_jobs.py` (+9: the market-open fix
      proven directly, shadow-mode account-mismatch pass-through vs. non-shadow
      rejection, preflight's routine-under-shadow and still-fails-kill-switch cases,
      plus 4 `mark_broker_to_market` tests moved here from `test_intraday_placebo.py`
      since that's where the function now lives). `tests/test_cli_intraday.py` (+6:
      `open-batch`/`cycle`/`carry`/`monitor` in shadow mode with `AlpacaPaperBroker.
      from_env` patched to raise if ever called, plus shadow state persisting across two
      separate CLI invocations -- proving state survives what a real cron job's fresh
      process boundary would impose). `tests/test_config_intraday.py` (+1: the new
      fields' defaults). Full suite green (`uv run pytest`), `ruff check`/`format` clean.

**Phase 12a -- options schema, config, and the options.py gate/pricing module (Rollout
Stage 4), built 2026-09-26, ahead of Stage 4's actual turn in the rollout.** Confirmed by
reading the code directly, not assumed: nothing for Stage 4 ("options in shadow, 2 weeks
-- proposals priced and logged but not traded") existed. No `intraday/options.py` despite
the brief's own file tree naming one; `TradeProposal` accepted `instrument="option"` at
the schema level with no `expiry`/`strike`/`right`/`structure` fields despite the brief's
schema section requiring them; `IntradayOptionsConfig` had exactly one field, `enabled`;
`validate_proposal`'s entire options-awareness was a two-line blanket rejection.

Split into two sub-phases (plan: `starry-drifting-parasol.md`) since the full surface area
(schema, config, a new module, a market-data integration, execution wiring, prompts) was
too large for one reviewable diff. **12a is pure logic, no live wiring** -- Phase 12b
(real Alpaca options market data, `jobs.py` wiring, prompt updates) is still open.

Design:
- `data/options.py` gained `black_scholes_greeks` (delta/gamma/vega/theta), right next to
  `black_scholes_price` -- reused by the new module rather than re-derived, matching this
  project's "one choke point" convention generalized to option pricing math. Verified
  against finite-difference derivatives of `black_scholes_price` itself, put-call delta
  parity, and the `t<=0`/`sigma<=0` boundary (gamma/vega/theta exactly zero, delta the
  intrinsic step function) -- not just closed-form-vs-closed-form, an independent
  numerical cross-check.
- `IntradayOptionsConfig` gained `shadow_pricing: bool = False`, independent of `enabled`
  -- the same `trading_enabled`/`shadow_mode` two-flag shape Phase 11 already established
  for "may this run at all" vs. "does this run touch the real broker." Plus every numeric
  limit the brief names (two with brief-given defaults: premium-at-risk caps, DTE bounds;
  the rest disclosed placeholders matching `max_spread_bps`'s own precedent: liquidity
  minimums, net delta cap, daily theta cap, the "half the spread plus 5bps" options cost
  model, and the IV-vs-forecast margin the brief states narratively but never numbers).
  Added to both `config/default.yaml` and `config/paper.yaml`.
- `TradeProposal` gained `expiry`/`strike`/`right`/`structure` (defined-risk-only --
  `structure` has no short-premium value, so "never a naked short option" is a schema
  fact, not a validator that could be bypassed), required together exactly when
  `instrument="option"` via a new `model_validator`.
- New `intraday/options.py`: `price_contract` (recomputes IV from a quote's own midpoint,
  never trusts a feed's reported IV -- same discipline `data/options.py`'s docstring
  already established; returns NaN greeks rather than a nonsense number when the quote is
  crossed/stale), `check_liquidity`/`check_dte`/`check_premium_at_risk`/
  `check_iv_vs_forecast`/`check_earnings`/`check_portfolio_delta_cap`/
  `check_daily_theta_cap` (every brief-named gate), `should_close_for_dte` (an *exit*
  rule, wired to nothing yet -- there's never a real position to close until Stage 5),
  `portfolio_greeks` (built now for Stage 5's sake; always zero in practice during Stage
  4 since nothing executes), and `validate_option_proposal` as the one aggregator
  `proposals.py` calls. `TradeProposal`/`ValidationContext`/`IntradayOptionsConfig` are
  imported under `TYPE_CHECKING` only -- `ValidationContext` type-hints its own new
  `option_quote`/`option_pricing` fields against this module, so a module-level import in
  either direction would be circular; `validate_proposal` imports the aggregator locally,
  inside the function body, once both modules have already loaded.
- `validate_proposal`'s options gate widened from "reject if `not enabled`" to "reject
  unless `shadow_pricing or enabled`, then run the full gate suite" -- and its edge-vs-cost
  check gained an instrument-aware branch (the options cost model needs the quote's own
  spread, unlike the flat equity bps figure), both per-side figures doubled for a round
  trip the same way `equity_cost_bps_per_side` already is.
- **A design correction caught before finalizing, not after**: `check_iv_vs_forecast`'s
  IV-richness gate and `check_portfolio_delta_cap`/`check_daily_theta_cap` needed a way to
  size "how many contracts does this premium buy" without building real order-sizing
  machinery (Stage 5's job) -- resolved with a continuous (not floored) `implied_contracts`
  approximation, disclosed as a validation-time risk estimate, not the real order Stage 5
  would eventually build.

Deliberately not this phase (Stage 5's scope): real order submission (OCC symbols,
`broker.py`/`orders.py` extensions), `review.py`/`eod_report.py`'s real options P&L
attribution (stays hardcoded `0.0` -- nothing ever fills during Stage 4 either), the
market-data fetch and `jobs.py` wiring themselves (Phase 12b), and flipping either
`enabled` or `shadow_pricing` to `True` (owner-only, per the brief's own "Ask first:
enabling options").

Tests: `tests/test_options.py` (+8: greeks against finite-difference/parity/boundary
identities). `tests/test_intraday_options.py` (new, 29 tests: every gate/pricing function
in isolation, plus the aggregator). `tests/test_intraday_proposals.py` (+7: schema
requires/forbids the option fields correctly, the `shadow_pricing`-widened gate, a clean
pass with real pricing supplied, and the options-vs-equity cost model actually using the
right formula -- caught a real gap while writing this last test: an early draft's
generic-looking test didn't actually verify anything about *which* cost model ran).
`tests/test_config_intraday.py` (+7: new field defaults on both the code default and
`config/paper.yaml`). Two real test-authoring bugs caught by actually running the suite,
not just writing it: the original "no prior pricing" test for `options.py` accidentally
constructed a *genuinely untouched* scenario (which correctly reads clean, not the
"someone else already has pricing" case it meant to test) -- rewritten once the failure
explained why; a `ValidationContext` test helper passed a bare `IntradayOptionsConfig`
where `IntradayConfig` (with `.options` nested inside) was needed. Full suite green
(`uv run pytest`), `ruff check`/`format` clean.

**Phase 12b -- real Alpaca options market data, `jobs.py` execution-pipeline wiring, and
prompts (Rollout Stage 4), built 2026-09-26, same session as 12a.** Makes Stage 4
*runnable* (not yet *run* -- same "built vs. run" distinction already drawn for Stage 1;
`shadow_pricing`/`enabled` both stay `False` in `config/paper.yaml`, unchanged this phase).

Design:
- New `execution/market_data.py::AlpacaOptionsMarketDataClient` -- a third Alpaca client
  (`OptionHistoricalDataClient` for quotes/bars, plus a read-only `TradingClient` for open
  interest, which the data API doesn't expose at all: confirmed directly against the
  installed `alpaca-py` 0.44.0 models, `OptionsSnapshot` carries only `symbol`/
  `latest_trade`/`latest_quote`/`implied_volatility`/`greeks`). `quote_contract` resolves a
  proposal's own `expiry`/`strike`/`right` via `get_option_chain` filtered to an exact
  strike/expiry/type match -- Alpaca generates the OCC symbol itself as the response dict's
  key, so this project never constructs one (that stays Stage 5's job, for real order
  submission, per the plan's own scoping). Tries `feed=OptionsFeed.OPRA` first, falls back
  to `INDICATIVE` on a 401/403, and records which one actually answered on every quote --
  this *is* how Stage 4 answers its own "does the feed produce sane pricing" question, see
  `docs/alpaca_notes.md`'s new entry for the exact alpaca-py surface called. Feed-reported
  `implied_volatility`/`greeks` are never read, same "never trust a feed's IV" discipline
  12a's `price_contract` already established.
- `intraday/ledger.py`: new nullable `proposals.options_pricing_json` column (with a
  `_COLUMN_MIGRATIONS` entry, same defect class as the `playbook_version` migration a few
  commits ago) -- **the plan's own Phase 12b write-up got this wrong**: it said to fold the
  pricing snapshot into the existing `payload` dict ("no new ledger table... `payload`
  already accepts arbitrary data"), but `record_proposal`'s own docstring already warns
  exactly why that specific move is wrong: `payload` is reconstructed via
  `TradeProposal(**payload)` in more than one place (`run_cycle`'s tranche continuation,
  `reflect.py`), and that schema is `extra="forbid"`, so any extra key breaks every reader,
  not just the writer. A dedicated column (not a new table, so the plan's actual worry --
  needless schema sprawl -- still didn't happen) is the same fix `playbook_version` already
  used for the identical problem. Caught by re-reading `record_proposal`'s own docstring
  before wiring `jobs.py`, not by a failing test.
- `intraday/jobs.py::run_cycle`: gained two new, defaulted-`None` parameters
  (`market_data_pit`, `options_market_data`) so every existing equity-only caller and test
  is unaffected. Before building each proposal's `ValidationContext`, an `instrument=
  "option"` `open`/`add` proposal is priced via a new `_price_option_proposal` helper
  (fetch + `options.py::price_contract`) and given a forecast-vol reading via the *existing*
  `_realized_vol` (Garman-Klass, not a live GARCH/HAR re-fit -- reusing Phase 6's own
  already-disclosed substitution for "the project's GARCH or HAR forecast," for the same
  cost/latency reasons that phase gave, applied here at per-proposal instead of per-morning
  frequency) and `_upcoming_earnings`. The resulting pricing snapshot is recorded via the
  new ledger column on *every* option proposal, accepted or rejected -- Stage 4's whole
  purpose is judging the feed's pricing, which needs the data point on a rejected proposal
  too, not only one that also happened to clear every other gate.
- **The core invariant, and a real bug caught while wiring it, not after**: an accepted
  option proposal must never reach `build_entry_order`/`submit_order_with_fault_isolation`.
  The obvious guard ("skip if `not config.options.enabled`") isn't what got written --
  it's unconditional on that flag instead, because there is no Stage 5 order-construction
  path for options at all yet, so flipping `enabled` today wouldn't make submission work
  correctly regardless. More importantly: `validate_proposal` accepts an option `trim`/
  `close` almost for free (those actions skip every options-specific gate the same way they
  already skip every equity risk check), and `trim`/`close` are dispatched by `symbol`
  alone with no instrument check of their own -- without a guard placed *before* that
  dispatch, an option trim/close would have silently trimmed/closed the *equity* position
  of the same underlying symbol. Caught by tracing the actual dispatch code, not by a
  test failing first; a regression test now covers it directly (see Tests).
- `intraday/context.py::build_trader_context` gained an `options_available` bool
  (`shadow_pricing or enabled`) in `sections`, and `cli.py::_run_intraday_cycle` only
  builds `market_data_pit`/`options_market_data` at all when one of those is true -- so
  Stage 1-3's real, currently-running cycles pay zero extra latency or credential surface
  for this phase, matching the priority already set for the self-looping monitor job
  earlier this session ("optimizing time in market," not adding lag).
- `intraday/prompts/trader.md`: the old blanket "options are not enabled yet; do not
  propose one" replaced with conditional guidance gated on `options_available`, describing
  `expiry`/`strike`/`right`/`structure` and when to prefer `debit_vertical` over an outright
  long call/put -- the actual numeric gates stay enforced in code, the prompt only
  describes the schema and intent, same division of labor as the rest of this file.

Deliberately not this phase (Stage 5's scope, unchanged from 12a's own list): real order
submission, OCC symbol construction, `should_close_for_dte`'s call site, real options P&L
attribution, and flipping `enabled`/`shadow_pricing` to `True`.

Tests: `tests/test_execution_market_data.py` (+18: `AlpacaOptionsMarketDataClient`
construction, the OPRA-then-indicative fallback, a non-entitlement error re-raising rather
than being swallowed, missing/non-positive quotes, and open-interest/volume defaulting to
`0` on a failed lookup rather than crashing). `tests/test_intraday_jobs.py` (+4: the core
never-submits invariant on a genuinely accepted proposal, pricing recorded on a *rejected*
proposal too, a clean "no pricing available" failure with no market-data client supplied at
all, and the option-trim-must-not-touch-the-equity-position regression). `tests/
test_intraday_ledger.py` (+3: the new column's own pre-Phase-12b migration, and a plain
round-trip through `get_proposal`/`proposals_for`). `tests/test_intraday_context.py` (+2:
`options_available` false by default, true under `shadow_pricing` alone). Full suite green
(`uv run pytest`), `ruff check`/`format` clean.

**Remaining prerequisites** -- what Phase 0 didn't (and couldn't) resolve:

1. ~~The owner still needs to actually create the second Alpaca paper account~~ --
   **resolved by 2026-09-23**: `ALPACA_S4_API_KEY`/`ALPACA_S4_SECRET_KEY` were already live
   as GitHub repo secrets before this was even checked (inferred, not owner-stated --
   every S4 job since 2026-09-22 has declined with a clean "account does not match
   intraday.registered_account" ledger incident rather than crashing, which only happens
   if `AlpacaPaperBroker.get_account()` itself succeeded first; `from_env` raises
   `RuntimeError` immediately on a missing key pair, which would have shown up as a failed
   job, not a routine decline). The owner then gave the account's own number
   (`PA33HJGNHLI7`), now `intraday.registered_account` in `config/paper.yaml`. **Still
   unconfirmed**: whether `ANTHROPIC_API_KEY` (Console/metered, per the `--bare` decision
   below, scoped *only* to the CI environment -- confirmed in `code.claude.com/docs/en/
   authentication` that setting it locally would silently flip the owner's own interactive
   Claude Code usage to metered billing too) is genuinely valid -- every run so far has
   declined at pretrade before ever reaching `invoke_role`, so the first real `research`
   run under Stage 1 is the first actual test of it.
2. ~~Where the loop runs~~ -- **resolved**, see the correction above.
3. **A per-day model-call spend cap** -- the mechanism is built (`intraday/budget.py`), but
   the $10 default is a placeholder. Recalibrate from real Stage-1 shadow-mode logging.
4. ~~Alpaca's news API is not wired anywhere~~ -- **resolved** (stale note corrected
   2026-09-27): `execution/news.py::AlpacaNewsClient` is wired into `preflight`,
   `research`, and `preopen` (`cli.py::_intraday_news_client`). Still the Stage-1 default:
   nothing beyond Alpaca news + this project's own data, expand later only with evidence.
5. ~~`.claude/agents/`~~ -- **resolved**, see the correction above (`--append-system-
   prompt-file`, a Phase 2 concern).
6. ~~The headless flag set beyond `--bare` is unverified~~ -- **mostly resolved in
   Phase 2**: every flag `intraday/llm.py` builds an argv from (`--output-format json`,
   `--max-turns`, `--max-budget-usd`, `--json-schema`, `--append-system-prompt-file`,
   `--permission-mode`/`--permission-prompts`) was re-verified directly against current
   docs and is wired and tested against a boundary-mocked subprocess. What's still
   unverified is the CLI's *actual* JSON output field names against one of this
   project's real calls -- that's blocked on prerequisite 1 below, not on more code.

**Rollout stages from the brief** (each gated on the owner, never automatic):

- [x] Stage 1 -- shadow, 2 weeks. **Started 2026-09-23** (owner's deliberate action,
      `docs/go_live_checklist.md` item 6's own two-step pattern: register the account,
      then separately flip the switch). `config/paper.yaml`: `intraday.registered_account:
      PA33HJGNHLI7`, `intraday.trading_enabled: true`, `intraday.shadow_mode: true`
      (unchanged, still the default). Full schedule, no real orders submitted, proposals
      filled against the cost model (`FakeBroker`), playbook frozen for a clean baseline.
      Originally through ~2026-10-07; **recounted from 2026-09-28** (timezone bug, see below). Unconfirmed going in: whether `ANTHROPIC_API_KEY`
      is a valid GitHub secret -- see "Remaining prerequisites" item 1; the first
      `research` run under this stage is the first real test of it.

      **Found 2026-09-23, before any real activity under this stage**: GitHub's own
      `schedule` event delivery for this repo is far less reliable than the configured
      cron intervals suggest -- confirmed against both `intraday_trading.yml` and the much
      lighter, separately-grouped `paper_trading.yml`, both showing the same multi-hour
      delay and most `monitor`/`cycle` ticks never firing at all, ruling out this
      project's own shared concurrency group as the cause. This matters specifically for
      shadow mode: `FakeBroker` never auto-triggers a resting stop the way a real Alpaca
      order would (confirmed directly against its own module docstring), so an
      invalidated shadow position's actual protection is only as prompt as this gap --
      Stage 2+ is unaffected, since a real broker's own resting stop enforces regardless
      of cron reliability. Mitigated, not fixed: `check_invalidation_and_time_stops` now
      records every call's own cadence (`IntradayLedger.record_monitor_check`, a new
      `monitor_checks` table) and both `reports/.../eod.md` and `weekly_review.md` report
      the actual average/max gap between checks, so Stage 1's own numbers stay honest
      about the cadence that actually happened rather than the cadence that was
      configured. A real fix (e.g. a self-looping `monitor` job instead of relying on
      GitHub's cron for sub-15-minute cadence) is still open -- deliberately not built yet,
      since it's a real architecture change and this repo's own numbers now surface
      whether it's actually needed before investing in it.

      **Two more bugs found and fixed 2026-09-26, both first triggered by s3's own first
      real month-end rebalance (`cf4f844`, 2026-09-25) rather than anything Stage-1-specific
      to S4**: (1) `mark(s0)` reconciled s0's own (always-empty) `net_filled_qty` against
      the *whole* shared Alpaca account's positions (s0 and s3 share one account,
      `docs/go_live_checklist.md` item 6), so once s3 actually held something, every
      `mark(s0)` run failed reconciliation -- and since `paper_trading.yml`'s mark step runs
      under `set -e`, that failure aborted the script before `git commit`, so it produced
      zero ledger commits the whole time rather than a visibly failing one. Fixed by
      reconciling against `Ledger.net_filled_qty_all_sleeves()` instead
      (`b3c78fe`). (2) The real `ledger/intraday.sqlite3`'s `proposals` table predates
      Phase 10's `playbook_version` column -- `CREATE TABLE IF NOT EXISTS` never migrates an
      existing table, so the first real `record_proposal` call (any cycle that actually
      proposed something) crashed with `OperationalError: table proposals has no column
      named playbook_version`; the table has zero rows in production, confirming no cycle
      had ever gotten past this. Fixed with a small `_migrate_missing_columns` step in
      `IntradayLedger.__init__` (`6c2d372`). Both pushed; **not yet verified against a real
      run** -- both workflows' cron schedules are Mon-Fri only, so nothing runs until market
      open next week. Check the first Monday `mark (s0)` and `intraday cycle` runs actually
      commit cleanly before considering either fix confirmed.

      **A third, related bug found 2026-09-26 while auditing for other instances of the same
      two bug classes** (a targeted audit, not a re-scope -- both "Known gaps" items below
      were checked and are unrelated, deliberate owner decisions, not touched): the fix
      above (1) only stopped `mark(s0)` from *crashing* on a false reconciliation
      discrepancy -- it didn't touch this same function's own final step, which "always
      records a fresh snapshot... regardless of what else happened" (`run_mark`'s own
      docstring) using the *raw* `broker.get_account()`/`list_positions()`, unchanged.
      Since s0 and s3 share one account, that snapshot is whichever sleeve actually holds
      real positions -- s3. Before the reconciliation fix, `set -e` aborted `mark(s0)`
      before this line ever ran, so the real, committed `ledger/paper.sqlite3` was never
      actually corrupted (confirmed directly: s0's last real snapshot is 2026-09-23, the
      day before s3's rebalance, and nothing since -- the reconciliation crash was silently
      protecting this line from ever executing). But now that `mark(s0)` reconciles clean,
      the very next successful run would have started writing s3's real equity/cash/
      positions into s0's own permanent snapshot history, mislabeled as s0's -- corrupting
      the entire s0-vs-s3 comparison this shared-account setup exists to make, silently
      (no crash, no incident, just wrong data accumulating every 30 minutes). Fixed:
      `run_mark` now compares this sleeve's own fills (`net_filled_qty`) against the
      account-wide total (`expected`, already computed above for reconciliation) -- equal
      means nobody else has ever traded here, so the broker's real state genuinely is this
      sleeve's own and gets recorded as before; otherwise it carries that sleeve's own last
      recorded snapshot forward *verbatim* (equity, cash, and whatever positions it last
      held), or records nothing if it has no prior snapshot at all rather than invent a
      starting figure. **Deliberately not gated on `config.paper.sleeves[sleeve].
      trading_enabled`** -- an earlier draft of this fix used that flag directly, but a
      config flag answers "may this sleeve place new orders," not "does the account's real
      state still belong to it": a sleeve disabled at a future review checkpoint
      (`paper.review_months: [3, 6, 12]`) while still holding a real, market-moving position
      from before must keep reading the broker directly, not freeze at a stale snapshot the
      moment it's disabled. The fills-based comparison handles that case correctly without
      any special-casing, since disabling a sleeve doesn't erase its historical fills.
      Verified directly against the real `ledger/paper.sqlite3` (read-only query, not just
      synthetic tests): `net_filled_qty("s0")` is `{}` against a combined total that already
      includes s3's real fills, so the very next `mark(s0)` will correctly take the
      carry-forward branch and reproduce the existing 2026-09-23 snapshot (`$100k`, flat) --
      no manual ledger edit needed.

      **One more hardening pass, same day, before pushing**: the sole-trader comparison
      initially used exact dict equality (`net_filled_qty(sleeve) == expected`) on two dicts
      of floats built from separately-filtered SQL queries with no `ORDER BY` -- SQLite makes
      no row-order guarantee between them, so float summation order (and therefore rounding)
      could in principle differ even for identical underlying fills. Switched to the same
      `position_tolerance_shares` tolerance the reconciliation check three lines above already
      uses, rather than relying on exact float equality holding by luck. Also confirmed
      `live/execute.py`'s own unconditional `broker.get_account()`/`list_positions()` calls
      are *not* exposed to this bug class -- its `check_pretrade` call returns early whenever
      `not sleeve_config.trading_enabled`, before either call is ever reached, so a disabled
      sleeve never gets far enough into `execute` to read the other sleeve's data. `mark`
      needed this fix specifically because it has no equivalent gate.

      Also not yet verified against a real scheduled run, same Monday caveat as the two
      fixes above.

      **Audit closed 2026-09-26**: traced every place in the paper-trading pipeline that
      constructs a real `AlpacaPaperBroker` or reads `broker.get_account()`/
      `list_positions()`, to confirm the two fixes above (mark's reconciliation and
      snapshot) were the complete list, not just the two that happened to surface via a
      real screenshot. `cli.py` constructs a real broker in exactly two commands, `paper
      execute` and `paper mark` -- `paper decide`/`paper report`/`paper evaluate` never do
      (confirmed by reading each: `decide.py` and `strategy/decide.py::decide` are a pure
      function of market data, no broker/positions input at all; `report.py` and
      `analytics/paper_evaluation.py` (T2/T3) and `analytics/execution_cost.py` (T5) all
      read only the ledger's own already-attributed `orders`/`fills`/`snapshots` rows,
      correctly filtered by `sleeve` through the ledger's own internal joins, never
      re-derived from the broker's live, unpartitioned account state). `execute.py` is
      structurally immune (its `check_pretrade` call returns early for a disabled sleeve
      before either broker call is ever reached). That leaves `mark.py` as the only
      function that ever touches the broker's real account state unconditionally, for every
      sleeve, regardless of `trading_enabled` -- both of its own instances of the bug are
      now fixed. Also directly confirmed why the snapshot fix mattered:
      `analytics/paper_evaluation.py::evaluate_t2` (T2, "does S3 improve on S0?") calls
      `equity_curve_from_snapshots(ledger, "s0")`/`"s3"` straight off the exact table the
      fix touches -- left broken, T2 would eventually have compared S3 against a corrupted
      copy of itself.

      **The self-looping `monitor` job (referenced above as still-open) was built
      2026-09-26**, once the deferred question -- "does the real cadence actually justify
      this" -- had a real answer: the `monitor_checks` table's own 2026-09-24/25 data showed
      ~165-172 minute gaps against a 5-minute configured cadence, both days, nothing
      (monitor, cycle, or open-batch) firing in between. `.github/workflows/
      intraday_trading.yml`'s `monitor` job now loops internally (check, commit/push-retry,
      sleep 5 minutes, repeat) for up to ~5.5 hours per invocation or until the trading
      day's own close, instead of exiting after one check -- so a single successful
      GitHub-scheduled firing can now cover the rest of the session on its own, rather than
      needing ~80 separate firings a day to each land on time. Pulled `monitor` out of the
      shared `intraday-trading-ledger-write` concurrency group (a 5.5-hour-long job would
      otherwise block `cycle`/`carry` from running at all for that whole window) -- safe
      because every iteration already does its own fetch/reset/push-with-retry, the same
      pattern that already handles concurrent writers everywhere else in this workflow. A
      single failed iteration doesn't abort the loop (logged, retried in 5 minutes); 3
      *consecutive* fully-failed iterations does exit 1, so a transient push race stays
      quiet but a genuine persistent bug still trips GitHub's failure email. Verified with a
      stubbed-git/uv dry-run harness of the loop's control flow (always-run-once at the
      deadline/close boundary, normal multi-iteration looping, and the 3-consecutive-
      failures exit), not yet against a real scheduled run -- same Monday market-open
      caveat as the two fixes above.

      **`cycle`'s range widened the same day**, for the same reason: `13-19` UTC maps to
      Central `07:00-13:55` under CST (starting 2026-11-01), cutting off ~55 minutes before
      the real ~14:50 close -- widened to `13-20` (matching `monitor`'s already-existing
      upper bound), covering Central `07:00-14:55` under CST and `08:00-15:55` under CDT
      (the extra hour past close under CDT is harmless, same routine-decline tolerance
      every job here already relies on). `monitor` itself needed no change -- its own
      `13-20` range already reaches Central `14:55` under CST, a 5-minute margin, not the
      lost hour originally (and incorrectly) suspected.

      **DST reminder for 2026-11-01** (CST begins, Central shifts to UTC-6): every
      single-fire job below shifts a full hour earlier in Central-clock terms if left
      alone -- confirmed by precise computation, not assumption. Two are operationally
      serious: `open-batch` would fire at 07:30 Central, a full hour before the market
      opens, so pretrade declines and the day's designated opening cycle never runs that
      day; `carry` would fire at 13:45 Central, closing out positions over an hour before
      the actual close. `eod-review`/`weekly-review` would review a still-open session.
      Deliberately not auto-fixed with a runtime guard: GitHub's own schedule delivery for
      this workflow is already confirmed (this session, `monitor_checks` telemetry) to
      sometimes land hours late regardless of DST, so a tolerance-based local-time check
      wouldn't reliably distinguish "wrong season" from "just late" and would add real
      complexity to jobs that submit/close real position-affecting actions (once
      `shadow_mode` is eventually turned off) for only a partial fix. Chosen instead: the
      same accepted approximation already live today in `paper_trading.yml` for real s0/s3
      trading (its own header: "~9-10am ET depending on DST") -- a plain manual cron edit,
      twice a year. On 2026-11-01, shift every one of these crons and their matching `if:`
      condition by +1 hour UTC (both the `schedule:` entry and the job's own `if:` string,
      same pattern used for `cycle`'s fix above):
      ```
      preflight:      "45 11 * * 1-5" -> "45 12 * * 1-5"
      research:       "30 12 * * 1-5" -> "30 13 * * 1-5"
      preopen:        "15 13 * * 1-5" -> "15 14 * * 1-5"
      open-batch:     "30 13 * * 1-5" -> "30 14 * * 1-5"
      carry:          "45 19 * * 1-5" -> "45 20 * * 1-5"
      eod-review:     "15 20 * * 1-5" -> "15 21 * * 1-5"
      weekly-review:  "45 20 * * 5"   -> "45 21 * * 5"
      ```
      Shift them all back by 1 hour UTC again the following spring (2027's DST start,
      typically the second Sunday of March) to return to the values already in this file.

      **Stage 1's first three days produced no usable evidence -- a timezone bug, found
      and fixed 2026-09-27** while building the readiness report below, whose first run
      against the real ledger showed zero proposals on every shadow day and a wall of
      `"<SYMBOL>: no quote available"` warnings for liquid names (AMD, TSM) at midday.
      Cause: `cli.py::_intraday_now` returned naive *Central* wall-clock time, but every
      timezone boundary it reaches reads a naive datetime as UTC
      (`AlpacaMarketDataClient._nearest_quote`, `pretrade.py::check_spread_gate`, the
      monitor's time-stop, `reflect.py`). So a 12:45 CT lookup asked IEX for quotes at
      12:45 *UTC* (07:45 CT, pre-market), got nothing, and every candidate was dropped
      before the Trader could propose it. The same mismatch shows in `monitor_checks.
      checked_at` (12:45+00:00) sitting five hours behind its own row's `created_at`
      (17:45+00:00). Fixed at the source: `_intraday_now` now returns naive UTC (nothing
      in the intraday layer reads its wall-clock fields; still naive so FakeBroker state
      stays naive-to-naive comparable; zero fills in either ledger, so nothing stored
      needed migrating). Two consequences of the same mismatch fixed with it: the
      post-stop-out cooldown (`proposals.py`) subtracted the incident log's aware-UTC
      `created_at` from a naive `now` -- a `TypeError` on the first cycle after any real
      stop-out, hidden because every test used an aware `NOW`; and, from Stage 2 on, real
      Alpaca fills (aware UTC) would have made the monitor's time-stop elapsed time five
      hours short. **Owner decision 2026-09-27: Stage 1 is counted from 2026-09-28**
      (the first session after this fix), since nothing before it exercised the proposal
      path at all -- earliest end 2026-10-09, and promotion to Stage 2 additionally needs
      `semimc intraday readiness --since 2026-09-28` to pass its evidence checks, not just
      the calendar. Before then: a mid-week readiness run to confirm proposals appear and
      "no quote available" is gone, and the missing-`eod.md` cause found from the Actions
      run history. **The same bug class in s0/s3, fixed the same day** (owner-approved):
      `live/mark.py`'s reprice check computed `now - order.submitted_at` with
      `_paper_now`'s naive-Eastern `now` against Alpaca's aware-UTC `submitted_at` -- a
      `TypeError` whenever a real order was still open at mark time, hidden from the suite
      by FakeBroker's naive timestamps. Fixed with `live/mark.py::as_paper_local`, which
      reads a naive value as `paper.execution.timezone` local time before comparing
      (`_paper_now` itself left local: `execute`'s `market.spot(as_of=now)` wants the
      exchange-local date). The `filled_at` fallback in `mark`/`execute` (used only when
      the broker reports a fill without a timestamp) now stores that same aware value,
      so the column never mixes naive-local with aware-UTC.

      **Stage readiness report built 2026-09-27** (`intraday/readiness.py`, `semimc
      intraday readiness [--since DATE] [--as-of DATE]`, writes `reports/YYYY-MM-DD/
      stage_readiness.md`). Every other S4 report covers one day or five; the Stage 1 ->
      2 decision is about the whole shadow period, and nothing put it in one place. Pure
      ledger aggregation (real + placebo), no broker/market data/model call, never flips
      config or recommends a stage -- run on demand, not on a schedule. Evidence checks
      map onto the brief's success criteria: activity every weekday (holidays can't be
      told from missed runs without a market calendar -- reported as "holiday or missed
      run", not guessed), research + Trader ran on every active day, an `eod.md` on every
      active day, no error incidents, proposals produced, closed trades >=
      `playbook.evidence_min_trades` (a disclosed reuse of the brief's own playbook
      evidence threshold -- it names no separate promotion threshold), placebo comparison
      available, peak daily spend within `budget.max_cost_usd_per_day`. Plus a day-by-day
      table, rejection reasons (split on `"; "`), incidents grouped by failure mode, and a
      spend summary for recalibrating the $10 placeholder. New `IntradayLedger.briefs_for`.
      Its first real run (2026-09-23..25) passed 2/8, and surfaced two more findings
      beyond the timezone bug: **no `eod.md` has ever been committed** (`reports/` holds
      only one `weekly_review.md`) -- **explained and fixed the same day**: not a failed
      job, a clean skip. `run_eod_review` returned "nothing to review yet today" whenever
      a day had no proposals and no closes, which the timezone bug made every day; it
      exited 0 with nothing to commit. Beyond that one cause, the skip was a design flaw:
      a legitimately quiet day also lost its report and its placebo comparison, which the
      brief wants in every daily report. Now a quiet day writes the mechanical report
      (blotter, P&L, benchmarks incl. placebo, scorecard numbers, monitor cadence) and
      skips only the Reviewer's model call, with each Reviewer-only section saying so
      rather than showing the false "not yet built" placeholder; and every job that did run landed 3-5 hours after its cron (research at
      ~17:29 UTC vs. 12:30 scheduled, both days), so research and open-batch both ran
      after midday Central, not before the open. Opening a ledger applies pending schema
      migrations (as every command does), so running this locally modifies
      `ledger/*.sqlite3` -- `git checkout -- ledger/` afterwards rather than committing a
      locally-migrated copy over CI's.
      **External triggering built 2026-09-27** (`docs/external_scheduler.md`). The
      scheduling data showed GitHub delivers this repo's `schedule` events 4-5 hours late
      for *both* workflows -- every morning job landed in a burst around 17:10-18:20 UTC
      (research ~12:30 CT, open-batch ~13:00 CT, paper decide ~14:00 ET, with ~1.5 h of
      margin before its 15:30 ET cutoff). Built: a required `job` choice input on both
      workflows' `workflow_dispatch`, with each job running only for its own name (this also
      fixes a footgun: a manual "Run workflow" click used to fire every job at once); a
      once-per-day guard on `open-batch` (real and placebo) and on the Reviewer call in
      `eod-review`, since a late cron copy of either used to do real work a second time
      (a second opening batch with fresh picks; a second paid Reviewer call); and the monitor
      loop now stops at 15:00 Central, not UTC hour 20 (which would have cut the last hour
      from 2026-11-01). **Owner steps pending**: a fine-grained token (Actions read/write,
      this repo only) and the cron-job.org entries, per the doc. **Actions minutes**: the repo
      is private (metered: 2,000/month on Free, 3,000 on Pro). **Owner decision
      2026-09-27: `monitor` gets no schedule** -- its cron is removed and it runs only on
      demand, since the loop was ~385 of the full schedule's ~500 minutes a day; every
      `cycle` already runs the same invalidation/time-stop check, so shadow stops are
      enforced within ~20 minutes (a conservative bias on Stage 1's stop-outs; irrelevant
      from Stage 2, where real stops rest at Alpaca). Remaining schedule: ~120 min/day,
      ~2,500/month.
      **External triggers live 2026-09-29** (cron-job.org; first on-time session
      2026-09-30). Setup took two days of owner-side fixes: jobs defaulted to GET (GitHub
      answers 404), then lost three of four headers on re-save (401), plus a body typo
      (422) -- all on cron-job.org, none in the repo. 2026-09-28 and -29 therefore ran on
      late crons only (research 14:48 and 13:13 CT) and are not clean days.
      **Owner decision 2026-09-29: Stage 1 ends on consecutive clean trading days, not
      the calendar.** Promote after **3** consecutive clean days if the streak includes
      accepted trade proposals (which the owner reads first), or **5** if it doesn't.
      Trade proposals means open/add/trim/close: `hold`/`no_trade` answers pass
      validation trivially and are counted separately (the first version counted them,
      and 2026-09-29's three "accepted proposals" were all holds). Why: Stage
      2 is still paper money, so the shadow stage's job is proving the plumbing and that
      the agent proposes sensibly; the 20-closed-trade threshold judges the strategy and
      can keep accumulating during Stage 2. A clean day: research finished before the
      open; the open-batch Trader call within 10 min of the open; cycle checks across the
      whole session with no gap over 30 min; no error incidents (warnings are fine); an eod
      report filed under that date; spend within the cap; the placebo ran. A trade on any
      one day is not required. A failed day resets the streak; market holidays and outages
      the owner judges outside the project's control go in
      `intraday.readiness.excluded_days` (neither count nor reset -- a written record, not a
      silent skip). `semimc intraday readiness` scores each day and the streak
      (`intraday/readiness.py`, "Stage 2 clean days"); run it after the close. Known gaps:
      `carry` leaves no trace on a quiet day, so the eod report stands in for it; a
      half-day session fails the coverage check and needs excluding by hand. (A third, a
      job run after 19:00 CT being dated the next day, was fixed 2026-10-01; see below.)
      **2026-09-30: first clean day (streak 1), but no trades.** Every external trigger
      fired on the minute. The Researcher's watchlist was one name (JBL, from the news,
      not in the universe), and the Trader answered hold/no_trade 16 times, nearly every
      answer citing "no volume data" or "no opening range data": its context held one
      midpoint per symbol, so a trigger like "holds direction on above-average volume
      after 15-30 minutes" could never be checked. The Researcher's brief opened with
      "data is thin", and two inputs really were empty. Fixed the same day (owner-
      approved): (1) `_upcoming_earnings` used `date > today`, so a same-day report was
      dropped -- MU reported after that close and the Researcher was told nothing was
      coming up (the options earnings gate shared the helper and was missing same-day
      prints too); (2) pre-market moves were empty for every ticker, most likely because
      a +/-5 minute window rarely holds an IEX pre-market quote -- a missing quote now
      falls back to the latest trade printed that New York day, and research logs an
      `info` incident saying how many tickers got a move and why the rest didn't; (3) the
      Trader gets a new `session` section (`intraday/session.py`): open, opening range,
      high/low, VWAP and IEX relative volume against the same time of day over the last
      10 sessions, from 5-minute IEX bars. Unverified against the real account until the
      2026-10-01 runs (`docs/alpaca_notes.md`). `carry` and `eod-review` had no
      cron-job.org entry that day, so both ran hours late (carry at 18:13 CT) -- harmless
      with no positions, but once the agent trades, a late carry leaves intraday positions
      open past the close. **All 11 cron-job.org entries live 2026-09-30** (the eight
      intraday and three paper jobs in `docs/external_scheduler.md`, each test-run to a
      204), so 2026-10-01 is the first session with every job triggered on time.
      **Date fix 2026-10-01 (owner-approved).** Those evening test runs (23:52 CT) all
      wrote rows dated 2026-10-01: `_intraday_as_of`/`_paper_as_of` took `pd.Timestamp.now()`,
      which on GitHub's runners is UTC, already tomorrow from 20:00 New York time. Harmless
      that night (a quiet-day eod.md and paper snapshots the next real run overwrites), but
      a late cron copy could take the next day's run-once slot. Both now default to the
      session date (`cli.py::_trading_day`): the New York date, rolling over at 04:00
      (pre-market) rather than midnight, so anything between the close and 04:00 belongs to
      the session that just ended. The plain New York date was the first version and would
      not have caught these runs: 23:52 CT is 00:52 in New York. Nothing is scheduled
      between midnight and 04:00 New York (preflight, the earliest job, is 07:45). The bot
      commit messages in all three workflows use the same rule; the cycle/monitor commit
      times stay UTC.
      **2026-10-01: not a clean day (streak back to 0).** Every external trigger fired on
      time, and the session data worked: the Trader's holds now cite the opening range and
      VWAP ("inside OR and below VWAP (609.26)") instead of "no volume data". But the
      12:40 and 13:40 CT cycles failed. The Trader proposed MU at 2% and 1.5% of $100k;
      split into two tranches, each piece ($1,000 and $750) was less than one $1,081 share,
      so `build_entry_order` raised, the cycle exited 1, and `set -e` stopped the workflow
      before its commit. Both cycles' rows were lost (the proposals, their model-call cost,
      the check itself), leaving a 40-minute gap (12:20 to 13:00 CT). Separately, GitHub's
      late copy of paper decide failed for s3 and vol_twin_shadow ("a different decision
      already exists"). The 10:05 ET decisions had been built on that day's partial price
      bar (data vintage 2026-10-01, the bar ~35 minutes old), and by the afternoon the bar
      had changed. Nothing traded twice. s3's 10-01 decision (4.31% per name, 60% invested)
      stands; it placed no orders, every holding being within `minimum_trade_pct` of target.
      Also seen: IEX spreads on MU and AMD ran 75-470 bps against the 50 bps gate most
      cycles, and pre-market moves priced 3 of 14 tickers. Fixed the same day
      (owner-approved): (1) every ledger-writing step in both workflows commits what the
      command wrote and then exits with its status, so a failure still emails but never
      loses rows (a failed paper decide still skips execute); (2) `validate_proposal`
      rejects an equity entry worth less than one whole share, with one share's fraction of
      equity in the reason; `run_cycle` splits an entry into at most as many tranches as it
      has whole shares, and a later tranche buys at least one share if the price rose since
      the first; `trader.md` now says `size_pct` is a fraction and entries are whole shares;
      (3) `run_decide` sees only closes before `as_of` (`MarketData.before`), so every run
      for a day computes the same decision and a late duplicate is a quiet no-op.
      `data_vintage` now records that last close, not the newest row in the cache.
      **Spread gate fix 2026-10-01 (owner-approved, the free option).** The IEX quote is
      one exchange's thin book, not the market's spread: it put MU/AMD 75-470 bps wide
      while the consolidated spread is a few cents, and Alpaca paper fills against the
      consolidated quote. So the gate blocked trades that would have executed fine, and
      the IEX midpoint was a skewed price for sizing, stops and shadow fills. Now the
      spread is judged on the free plan's 15-minute-delayed consolidated quote
      (`intraday.risk.spread_feed: delayed_sip`), and every S4 price (candidates,
      positions, stops, carry, the shadow and placebo marks) is the latest IEX trade
      within 5 minutes, else the IEX quote midpoint (`jobs.py::current_prices`).
      Freshness is judged on that price. Falls back to the old IEX check, with a warning,
      if the delayed feed can't be read. Known limits: in the first ~15 minutes after the
      open the delayed quote is a pre-market one, usually wider, so a name can still miss
      the open batch; a sudden spread blowout inside those 15 minutes goes unseen. The
      paid alternative, owner's call before Stage 2 or Stage 4: Algo Trader Plus
      (real-time consolidated data, which would also settle Stage 4's options-data
      question), then `spread_feed: sip`. The eod "do nothing" benchmark still marks at
      the IEX quote midpoint (it runs after the close, when no trade is within 5 minutes).
- [x] **Paper execute and mark fixes, 2026-10-02 (owner-approved).** GitHub's late copy
      of decide-and-execute ran at 13:56 CT. Decide skipped correctly (the 10-01 fix), but
      execute bought 2.84 QCOM (~$528) against the 10-01 decision: execute re-targeted every
      holding on every run, and QCOM had dipped past the 0.5% `minimum_trade_pct` line
      since the on-time 10:05 ET run. The module docstring assumed later runs would be
      no-ops "since positions already sit at target", which stops being true once prices
      move: in practice s3 was rebalancing its drift daily, unlike the monthly backtests
      behind it. Four minutes later mark(s0) failed on a QCOM reconciliation mismatch: it
      compares the whole shared account against every sleeve's fills, but only polled s0's
      own orders, so s3's not-yet-recorded fill looked like phantom shares (mark(s3)
      recorded it 20 seconds later and reconciled clean). Fixed: (1) `run_mark` records
      fills for every sleeve's open orders before reconciling (fills only; reprice cancels
      stay with the owning sleeve); (2) `run_execute` does the full rebalance once per
      decision, recorded in a new `executions` ledger table even when no orders were
      needed. Every later attempt, a later day or a late duplicate, only retries what that
      rebalance left unfinished (`unfinished_symbols`: an order that ended unfilled, or
      one that couldn't be placed or priced). Drift waits for the next decision. An order
      already placed toward a decision counts as an attempt, which covers the 10-01
      decision (QCOM filled, no executions row): its next execute skips.
- [x] **2026-10-02: clean day (streak 1), and the agent's first trade.** The spread fix
      worked (no spread failures, no fallback to IEX). At 09:01 CT the Trader bought
      NVDA ("gap held through 30-minute opening range on 1.76x volume", the session data
      at work): 1.5% of $100k, 6 shares in two tranches of 3. A 60-minute time stop closed
      it at $234.56, one cent above its invalidation price: -$20.93 net, graded a timing
      miss (process 3, invalidation respected). The week's placebo: 10 closed trades,
      -$1,487 net. **Bug the trade exposed, fixed 2026-10-03 (owner-approved):** each
      tranche carries its own OTO stop, but a close or trim cancelled only the most recent
      one (`open_stop_leg_for`, singular by a Phase 4 assumption its own docstring said to
      revisit once tranches existed), and never marked even that one cancelled in the
      ledger. So the NVDA close left the first tranche's 3-share sell stop resting with no
      position behind it. Harmless on the shadow broker, which never triggers stops; on a
      real account it would have opened a short had NVDA traded down to $234.55, and a
      trim could leave more stop shares than shares held. Now `cancel_resting_stops`
      cancels every open stop leg for the symbol and records each one's new status
      (`open_stop_legs_for`), and each cycle cancels any resting stop with no position
      and no working entry behind it, with a warning (`_cancel_orphaned_stops`; it clears
      the leftover NVDA leg on the next cycle). Also 2026-10-03: pre-market moves now take
      the more recent of an IEX trade and a delayed consolidated trade
      (`_todays_trade_prices`), since IEX alone priced 3 and 2 of 14 tickers on Oct 1-2.
- [x] **2026-10-03: exits made safe for the real broker before Stage 2 (owner-approved).**
      Every close and trim cancelled the position's stops and sent the exit straight
      after. The shadow broker cancels instantly; Alpaca cancels asynchronously (the stop
      sits in `pending_cancel`, still holding its shares), so on the real account the
      exit could be rejected "insufficient qty available". That rejection is a plain
      `BrokerError`, which `submit_order_with_fault_isolation` didn't catch, so it would
      have ended the whole cycle with the stops already cancelled: a position with no
      stop until the next cycle, and the rest of the batch skipped. Now every exit (Trader
      close and trim, monitor invalidation/time stop, both carry closes) goes through
      `orders.py::submit_exit`, which: waits up to 5 s for each cancel to complete
      (`cancel_resting_stops`; an unmeasured guess) and holds the exit back with an error
      incident if one hasn't; re-reads the position, so a stop that fired first is never
      sold twice (closing the old quantity would open a short), and records that stop's
      fill; and, if the exit doesn't go out, re-places a stop at the entry's invalidation
      price (or logs "position unprotected" when there is none). Any `BrokerError` on
      submit is now an incident, not a crash. The monitor reports a failed exit as
      `exits_failed` instead of "closed", and the cycle adds no tranche to it. Also fixed
      on the way: a trim too small for a whole share left the stops cancelled. `FakeBroker`
      gained `cancel_delay_lookups`, `enforce_held_qty` and `trigger_stop` to test this;
      all three are off or unused by default, so Stage 1 runs exactly as before.
- [x] **Stage 2 blocker, found and fixed 2026-10-03 (owner-approved): S4 never recorded a
      fill that arrived after submission.** It recorded fills only from the submit
      response (`submit_order_with_fault_isolation`). The shadow broker fills instantly,
      but on this account all 15 paper-sleeve orders came back from `submit_order` still
      open, filled 1-4 s later, and were recorded only by the next `mark`
      (`docs/alpaca_notes.md`). On the real account every entry would have stayed
      "accepted" in the ledger: the monitor would find no filled entry and never enforce
      a time stop; the next tranche would never go out; closes would get no reflection
      (`record_mechanical_reflection` runs right after the submit and records nothing
      without an exit fill); a stop firing between cycles would go unrecorded and skip the
      stop-out cooldown. Now `orders.py::sync_open_orders` asks the broker about every
      order the ledger shows open, records status changes and new fills (each increment
      once), and writes the reflection for any exit or stop leg that has filled since,
      using the exit's new `orders.exit_reason` column (migrated onto the real ledger;
      stop legs count as `"invalidation"` and also log `stopout:SYMBOL`). It runs first in
      `run_cycle`, the `monitor` command and `run_carry`, and in `eod-review` (which now
      builds the broker), since carry's 14:45 closes fill after the day's last cycle.
      Found while building it, also fixed: `submit_exit` now cancels every order still
      working in the symbol, not just the stops (an exit whose limit missed holds the
      shares, so a retry would be rejected; a working entry tranche is an opposite-side
      order); a second exit in the same cycle gets a fresh id (`-r1`), since the broker
      answers a reused id with the old cancelled order; and the cycle adds no tranche to
      a position the monitor just closed, which on the real account is still listed for a
      few seconds after the close goes out. `FakeBroker.fill_order` fills a working order
      later, for tests. Expected Monday in shadow mode: the first cycle's sync marks the
      second leftover NVDA stop cancelled (it already is on the shadow broker), so the
      orphan sweep's warning reads "canceled 1 resting stop(s)", not 2.
- [x] **A carried position had no resting stop the next day: found 2026-10-03, fixed
      2026-10-05 (owner chose option A of three: re-place each cycle; the others were a
      good-till-cancelled stop at carry, or leaving it).** Every S4 stop is a `day` order
      (the OTO leg, the trim replacement, the re-placed stop), so Alpaca expires it at the
      close and nothing put it back: all next day only the cycle's 20-minute check
      guarded the position. Shadow mode couldn't show this (the shadow broker never
      expires anything). Now every cycle, the 08:32 open batch first, runs
      `jobs.py::_protect_unguarded_positions`, the mirror image of the orphan sweep: any
      held shares no open stop leg covers get a `day` stop at the entry's invalidation
      price (info incident "placed one at ..."). Skipped: a symbol with an entry or an
      exit still working, and a position already through its invalidation (the
      monitor's to close). No invalidation price on file: a warning each cycle. Also
      covers an exit held back while a cancel completed and a replacement stop the broker
      refused. Limits: a stop only works in regular hours, so nothing guards an overnight
      gap, and the first ~2 minutes after the open are uncovered; coverage is read from
      the ledger, so it depends on the sync seeing expired stop legs (the leg-lookup
      question in `docs/alpaca_notes.md`). `FakeBroker.expire_day_orders` models the
      close in tests.
- [x] **2026-10-05: GitHub runner outage skipped carry; the clean-day check now catches a
      missed carry (owner-approved).** GitHub's Actions incident (opened 14:11 CT, "delays
      in assigning GitHub-hosted runners") failed run #197, almost certainly the 14:45
      carry ("The job was not acquired by Runner of type hosted even after multiple
      attempts"), and pushed the 14:40 cycle to 14:50. The agent's first shadow trade of
      the day, TSM (opened 10:20 CT, added 12:01 CT, `horizon: intraday`, 5 shares), was
      left open overnight; tomorrow's 08:32 open batch closes it on its expired time stop.
      Not re-run after the close: carry would have filled at an after-hours price. The
      clean-day check couldn't see this, since carry records nothing on a day it has
      nothing to close. Now `readiness.py` checks carry by its effect: a position still
      held at 16:00 New York, rebuilt from the ledger's fills, whose latest entry has no
      multi-day or multi-week horizon (carry's own rule) fails the day ("TSM still held at
      the close on an intraday thesis; carry should have closed it"). Applied to the real
      ledger it flags only 2026-10-05, so the streak restarts from 2026-10-06; the
      earliest the Stage 2 rule can pass is after 2026-10-08's close (3 clean days with
      trades), unless the owner lists 2026-10-05 in `readiness.excluded_days`.
- [x] **2026-10-06: the API account ran out of credits mid-session; three fixes
      (owner-approved).** The last Trader call that worked was 13:02 CT. Every later
      cycle failed at its Trader call (the monitor, stop sweeps and placebo still ran
      first, so positions stayed guarded), and eod-review failed at the Reviewer call
      before writing anything, so the day has no report. The logs said only "trader call
      exited 1". S4's own spend is small ($3.95 over 120 calls since 2026-09-24, at most
      $1.11 a day), so whatever drained the balance wasn't mainly S4. Fixes:
      (1) `llm.py`: a failed call's error now carries the CLI's own reason (the JSON
      result text, else the last stderr/stdout line, one line, at most 200 characters),
      and `invoke_role` records it as an error incident before re-raising, so the job
      still fails and alerts. (2) `run_eod_review` writes the mechanical report whenever
      the Reviewer can't narrate: the daily budget is spent, the call fails (then the job
      still fails, after writing it), or the response is malformed (that last case used
      to skip the report too). No Reviewer call is recorded on a failure, so a re-run
      once the account is fixed narrates the day in full. The report line is now "No
      Reviewer narration today: ...". (3) `IntradayLedger.incidents_for` dates incidents
      by trading session (new `semimc/trading_day.py`: New York date, 04:00 rollover,
      now also behind the CLI's default `--as-of`), not by UTC date: a late job at 19:57
      CT used to count against the next day. Applied to the real ledger, Monday's two
      after-hours TSM errors (a late cycle and the late carry, both unable to price
      after the close) move from 2026-10-06 back to 2026-10-05. Today's failed Trader
      calls predate fix (1), so they left no incidents: once eod-review is re-run,
      2026-10-06 would read clean in the readiness report though the Trader was down
      from 13:20 CT. **Owner decision 2026-10-07: 2026-10-06 is in
      `readiness.excluded_days`** (both configs), since the ledger shows the six
      in-session cycles from 13:20 to 14:40 CT ran with no Trader call (every cycle on
      every other day had one). Credits running out wasn't outside the project's
      control, but with 2026-10-05 already failed, excluding the day and failing it give
      the same result: the streak starts 2026-10-07, so the rule can pass after
      2026-10-09's close at the earliest (3 clean days with an accepted trade proposal),
      else after 2026-10-13's (5 without). Considered and not chosen: counting 10-06
      clean (one day sooner); a readiness check for a Trader call on every in-session
      cycle (same dates, a change to the 2026-09-29 rule); and excluding 2026-10-05 as
      GitHub's outage, which would pass the rule at once on 09-30 to 10-02, days that ran
      before the 2026-10-03 fill-sync, safe-exit and stop re-placement fixes Stage 2
      depends on.
- [x] **Late duplicate runs after the close (found and fixed 2026-10-06).** GitHub's own
      cron copy of `cycle` landed after the close every trading day since 2026-09-29
      (16:00-18:00 CT), and its copy of `carry` around 18:16 CT. In shadow mode the
      market check read the shadow broker's clock, which was always open, so each late
      cycle ran in full: 7 Trader calls on after-hours quotes, one of which (2026-10-05,
      16:06 CT) logged Monday's TSM "no current price" error, and any of which could have
      bought at a stale price that nothing closed until the next morning. On the real
      account (Stage 2) the clock is real, but `run_cycle` recorded the "market is
      closed" decline as an error, so every late copy would have failed the day. Owner
      chose the fix: the shadow broker now carries the real exchange clock
      (`FakeBroker.clock`, set in `cli.py::_intraday_execution_broker` from
      `_intraday_market_clock`, which reads the S4 account's clock and hands the broker to
      nothing else; fills stay simulated), and `run_cycle` records a closed-market decline
      as an info incident ("cycle_2256 skipped: market is closed") with no Trader call.
      Every other pretrade finding stays an error. `carry` is unchanged on purpose: if the
      on-time carry was missed, a late one still has work to do (on the real account the
      close queues for the next open), and after an on-time carry it finds nothing. The
      GitHub crons stay as a fallback. Market holidays now skip their cycles too (they
      used to run full cycles in shadow), so a holiday still belongs in
      `readiness.excluded_days`.
- [x] **Model spend cut (owner decision 2026-10-06).** Stage 1 ran about $1 a trading day
      (ledger, matching the Console), $3.95 in total from 2026-09-24. The Trader was ~85%
      of it, and its cost per call roughly tripled on 2026-10-02 (from ~$0.017 to
      ~$0.044) when it got session data: output went from ~1,800 to ~6,400 tokens a call
      while its saved answer stayed a few hundred tokens, so most of the bill was extended
      thinking, which Claude Code turns on by default and bills as output. Two changes:
      (1) `intraday.models.trader_max_thinking_tokens: 1500`, passed as
      `MAX_THINKING_TOKENS` to the Trader's `claude` call only (the "sonnet" alias is
      Sonnet 5.5 on the API, which ignores it; 0 turns thinking off, and the API minimum
      is 1,024). Each `model_calls` row now also records `num_turns`, so if output stays
      high it shows whether extra turns, not thinking, are the cause. (2) Cycles every 30
      minutes instead of 20 (the brief's figure): 12 cycles plus the open batch, down
      from 18 plus it. The real cadence is the cron-job.org `cycle` job, which the owner
      changed to `*/30 9-14 * * 1-5` the same evening (docs/external_scheduler.md); the
      GitHub backup cron, `schedule.cycle_interval_minutes`, `trader.md` and the readiness gap limit
      (`max_cycle_gap_minutes`, 30 -> 45, still one missed trigger) follow it. Shadow
      stops are now enforced within ~30 minutes. Expected: ~$0.35-0.40 a day, ~$8 a
      month. Check the first day's `model_calls` output tokens to confirm the cap took.
      **Confirmed 2026-10-07:** Trader output ~2,500 tokens a call (was ~6,400), $0.018 a
      call (was $0.044), `num_turns` 2-4; S4 ledger total $0.34 (Trader $0.235, Reviewer
      $0.077, Researcher $0.031). The owner saw $0.62 for the day. Two leads, neither
      confirmed: the last four Trader calls (13:30-14:31 CT) report $0.002-0.003 for
      1,500-3,300 output tokens, below Haiku's output price alone, so the CLI's
      `total_cost_usd` (installed fresh each run) may undercount; and re-run Trader calls
      on rejected pushes (next entry) were billed but never ledgered.
- [x] **2026-10-07: no errors, but not clean -- the 12:00 CT cycle never ran; two
      workflow fixes 2026-10-08 (owner-approved).** A 61-minute gap between cycle checks
      (11:31 -> 12:32 CT; the limit is 45) fails the day, so the streak starts
      2026-10-08: the earliest Stage 2 pass is after 2026-10-12's close with an accepted
      trade proposal, else 2026-10-14's. cron-job.org's history shows the 12:00 trigger
      sent cleanly and no run committed, so GitHub dropped it. Likely cause (unconfirmed
      -- the Actions tab would show a cancelled run): a concurrency group holds one
      pending run, and a newer arrival cancels it silently; GitHub's own cron copies of
      the morning jobs land 4-6 hours late, mid-session (preopen/open-batch at 18:25-21:07
      UTC every day that week; preflight/research copies leave no commit), so one waiting
      in `intraday-trading-ledger-write` could knock out the dispatch. A late `cycle` copy
      also opened a MU trade at 14:07 CT, six minutes after the on-time cycle chose
      no_trade (the Reviewer flagged it). Fix 1: the crons for preflight, research,
      preopen, open-batch and cycle are removed; cron-job.org is their only trigger (its
      failure emails are the alarm). carry, eod-review and weekly-review keep theirs (late
      copies land after the close and still have work if the on-time run failed). Found
      on the way, fix 2: on a rejected push every job reset to the latest main and re-ran
      its whole command -- for `cycle` a second Trader call, billed but only the last
      attempt ledgered, and on the real account the first attempt's orders would be at the
      broker with their ledger rows discarded. Since 2026-10-06 every cycle fires in the
      same minute as the two paper marks' pushes (one in three did at the 20-minute
      cadence). Now each job commits once and pushes through
      `.github/scripts/push_ledger.sh`, which rebases onto the latest main; exit 3 means
      another run changed the same binary ledger, which fails an intraday job (the group
      should make it impossible) and re-runs a paper job (its sleeves share
      `ledger/paper.sqlite3`, and decide/execute/mark are idempotent).
      `tests/test_push_ledger.py` drives the script against local repos and pins the
      remaining crons. Consequence: the `monitor` loop (outside the group, manual only,
      last run 2026-09-25) must not run during a session -- its push would now fail a
      cycle instead of being re-run around (docs/external_scheduler.md). Unchanged:
      `forecast_journal.yml` keeps its deliberate re-run (no orders, no model call, runs
      after hours).
- [x] **2026-10-08/09/12: three clean days, no trades -- the Researcher's triggers had
      outgrown the Trader's data; market panel built 2026-10-12 (owner chose both fixes).**
      Every cycle answered `no_trade` (27, 32, 22 a day) after accepted trades on most
      earlier days. On 10-08 and 10-12, 19 of 27 and 21 of 22 answers cited data the Trader
      couldn't see (2-3 a day before 10-07): from 10-07 the Researcher's triggers compared
      names with peers and sector ETFs ("ASML green while AMAT/LRCX trade red",
      "outperforms SMH", "SOX/QQQ stabilizing") or named yields and Iran headlines, and
      session data covered only watchlist names and positions. 10-09 was different: the
      triggers were checkable and didn't fire (inside the opening range on low volume).
      Not the thinking cap or a model change: the trigger style changed in the 07:30 CT
      Researcher run, before the Trader's cost oddity began. Fixes: (1) each cycle's
      session data also covers the market panel, the universe plus
      `intraday.session_data.panel_benchmarks` (SOXX, SMH, QQQ, SPY; owner-confirmed), in
      the same bars request (`jobs.py::market_panel`; alpaca-py follows page tokens, so the
      larger request comes back whole); (2) session data gains `prior_close` and
      `change_from_prior_close_pct` (the previous session's last regular-session IEX trade,
      within cents of the official close), since "green/red on the day" was the part of
      the 10-12 trigger the panel alone wouldn't have answered; (3) the Researcher gets a
      `trader_view` section built from the same constants the Trader's context uses
      (`context.py::TRADER_SECTIONS`, `session.py::SESSION_FIELDS`, the panel), and its
      prompt says to write triggers only in those terms, naming what the Trader can't see;
      tests pin both constants to the real outputs, so the description can't drift;
      (4) `validate_proposal` rejects an open/add in a name that is neither on the cycle's
      spread-gated watchlist nor held (`ValidationContext.tradable_symbols`): the Trader
      now sees names the Researcher never vetted, and the placebo, which draws from the
      same watchlist, stays a fair comparison. The Trader prompt explains the panel is for
      comparison. Cost: about 18 more symbols of session data per Trader call, a few
      thousand input tokens. Streak: 3 clean days through 10-12 with no accepted trade
      proposal; the rule passes after 10-13's close if that day is clean and has one, else
      after 10-14's (5 clean days). Verified: 1,223 tests passed; the 11
      `regime`/`factor_regime` model tests, blocked by a Smart App Control burst on
      statsmodels' `_kim_smoother` from 19:07 CT on 2026-10-12 (still at 20:39), passed
      unchanged on 2026-10-13. **2026-10-13, first day with the panel: it worked.** Both
      triggers were written in panel terms (MU "above the opening range high and VWAP,
      relative volume above 1.2, while SOXX is green on the day"; AMAT "...while SMH is
      flat or green and LRCX is not making new lows"); 15 of 25 Trader answers cited a
      panel name, 4 mentioned missing or weak data (21 of 22 on 10-12). Still no trade, on
      the merits: SOXX red all day (-2.46% at its low), MU inside its opening range. Clean
      day, streak 4 with no accepted trade proposal: a clean 10-14 passes the rule (5).
- [x] **2026-10-14: clean, streak 5 -- the Stage 2 rule passes** (5 clean days without an
      accepted trade proposal, 10-08 to 10-14). Promotion stays the owner's decision. Still
      no trade (35 no_trade): all three triggers (AMAT, LRCX, ASML) were long breakouts that
      also needed equipment peers green on the day. Why the agent stays quiet (owner asked
      2026-10-13): every candidate since 10-08 has been a 4-5-condition long breakout;
      the most common failures were price inside the opening range, low volume (10-09) and
      the sector red (all 25 answers on 10-13); 2-3 names a day gave few chances; and in
      practice it is long-only (the schema allows shorts, but neither prompt mentions them
      and no short entry has ever been sent). Sitting out paid: over 10-08 to 10-13 the
      placebo, trading random entries in the same names under the same risk rules, closed
      14 trades for -$805 net. **Owner chose option A of four (2026-10-14):** the
      Researcher prompt now asks for four to six candidates instead of "a short list",
      with no padding and, where possible, triggers that don't all hinge on the same
      condition. Not chosen: short setups (B; the short path is untested end to end and
      needs margin/borrow on the real account -- test in shadow first if wanted), simpler
      triggers (C; lowers the bar the experiment measures), and leaving it (D; the
      playbook's 20-trade evidence threshold would never be reached). Takes effect with
      the 2026-10-15 07:30 CT research run, the same day Stage 2 could start: read day
      one's trade count with both changes in mind.
- [ ] Stage 2 -- equities, half budget, 2 weeks. **Owner's go 2026-10-14; starts
      2026-10-15** (06:45 CT preflight). `config/paper.yaml`: `intraday.shadow_mode:
      false`, so open-batch/cycle/carry/monitor submit real orders to PA33HJGNHLI7 (owner
      confirmed the number 2026-10-14; code has never checked it, since shadow mode skips
      the account check -- a wrong number fails preflight and every cycle refuses to
      trade, it can't reach another account). Half budget = `new_positions_per_day` 3,
      `daily_turnover_pct` 0.75, `gross_cap_pct` 0.75, `overnight_gross_cap_pct` 0.50;
      `risk_per_trade_pct` and `single_name_cap_pct` unchanged, so each trade is full
      size. The placebo keeps running on its own shadow ledger. After the first real
      trade, check incidents for the two Alpaca behaviors no test could pin: the OTO
      stop leg found by `client_order_id`, and cancel timing (the 5-second wait is a
      guess). The brief sets no promotion rule for Stage 3; the owner set one on
      2026-10-16, under the Stage 3 entry below.
  - [x] **2026-10-15, day 1: real orders worked; not a clean day (3 errors).** Preflight
        passed live, so PA33HJGNHLI7 is the account the keys open (equity at open
        $100,000.00). The Researcher listed 5 candidates (option A's first day: TSM, TXN,
        ADI, MU, NVDA). Opened TSM and NVDA at 10:01 CT and added to TSM at 11:01, which
        spent the half budget's 3 new positions; the NVDA add was rejected for it, and
        MU's trigger fired at 11:30 and 13:30 with no slot left (MU rose about 6% off its
        low). Both closed on time stops at 13:30: gross +$0.74, -$6.05 after modeled
        cost. Placebo: 3 trades (MU, ADI, NVDA at about $12,000 each, against the
        agent's $1,700-3,400), +$1,101.79 net. **Both unverified Alpaca behaviors
        worked**: each OTO stop leg came back with its own id, was canceled, and the
        exits filled within a second, with no incident. **The 3 errors**: every quote
        went about 4 minutes stale at the 12:00 CT cycle and missing at 12:30 and 13:00
        (the 12:30 session data repeats 12:00's prices, so bars stalled too; no Alpaca
        status notice found), so the monitor couldn't price the due time stops: NVDA's
        (from its second tranche's 10:30 fill, due 12:01) and TSM's (due 13:01). They
        closed at 13:30, one hour and 30 minutes late, resting stops in place
        throughout. **Owner chose option A (2026-10-15)**: on the real account, a due
        time stop or carry close with no current price now goes out as a market order,
        only while the broker's clock says the market is open, never for an entry
        (`monitor.py`, `jobs.py::run_carry`, `orders.py::build_close_order`). Shadow
        and placebo `FakeBroker`s keep the old behavior. Not chosen: a 5-minute-bar
        fallback (B; the bars stalled too) and leaving it (C).
  - [x] **2026-10-16 (owner's go): `run_carry` does nothing while the market is
        closed.** It never checked the clock, and GitHub's late copy of `carry` lands
        around 17:40 CT every day: on a day a position was still open then, its limit
        close would have waited at Alpaca and filled at the next open, priced off
        after-hours trades. Now it sends nothing; the next session's monitor closes the
        position at a current price. A held position with no multi-day thesis is an
        error incident naming it, anything else an info line. The placebo's
        `FakeBroker` has no real clock and always reads open, so it is unchanged.
  - [x] **Early closes: the last cycle before the bell runs carry (2026-10-16, owner
        chose both options).** NYSE closes at 13:00 ET (12:00 CT) on 2026-11-27 and
        2026-12-24, but `carry` is scheduled at 14:45 CT, after the bell, so a position
        still open at 12:00 would have been held overnight (over the weekend on 11-27).
        The owner added one-off cron-job.org `carry` runs at 11:45 CT on both dates.
        In code, `run_cycle` reads Alpaca's calendar each cycle
        (`early_close_before_next_cycle`): on an early-close day, the cycle whose
        successor would land less than 15 minutes before the bell (carry's lead on a
        regular day) runs `run_carry` after the monitor and skips the Trader, since an
        intraday entry then would only be closed again minutes later. With a 30-minute
        cadence that is the 11:30 CT cycle. An info incident records what it closed and
        carried. Not covered: the placebo (its `FakeBroker` calendar always closes at
        16:00) and shadow mode, which rely on the scheduled carry, and so on the
        owner's one-off runs for these two dates.
  - [x] **2026-10-16: the Researcher may list stocks outside the universe (owner
        decision).** Owner asked why the real account traded only TSM and NVDA (10-15
        TSM/NVDA, 10-16 NVDA, stopped out at Alpaca about 10:10 CT for -$29.86 net: the
        first broker-side stop-out, and the cooldown recorded). No code restricts names;
        the funnel does: the Researcher's 2-5 names a day (MU 11 of 17 days, NVDA 7;
        QCOM and KLAC never), the same long-breakout trigger on nearly every name
        (opening-range high + VWAP + relative volume) in a sector down about eight
        sessions in a row, the half budget's 3 slots (the 10-15 TSM add took MU's), and
        the Trader passing on poor risk/reward. Checking that, nothing kept the
        watchlist to the 14 either (SMCI 09-25, JBL 09-30, both from the news). The owner
        chose to keep it open: the Researcher prompt now says outside names are allowed
        when the news makes a case, that its news, pre-market moves, earnings and
        volatility inputs cover the universe only (an outside name arrives via an
        article about a universe stock), and that the spread/quote-age gate applies each
        cycle. `test_run_cycle_trades_a_watchlist_name_outside_the_universe` pins it.
  - [x] **2026-10-16: reflections miscounted trades and misstated P&L (fixed; owner's
        go).** Found by the Reviewer's weekly note ("three lots of one position").
        Reflections are what every closed-trade count reads (readiness, eod report,
        calibration, the playbook's 20-trade threshold). Two bugs in `reflect.py`:
        (1) the entry price came from the latest entry's proposal alone, so an open
        plus an add was priced at the add's fills (10-16 NVDA: $217.33 against a true
        $217.49; 10-15 TSM; 10-05/06 TSM); (2) `sync_open_orders` wrote one reflection
        per filled stop leg, and each tranche and add carries its own, so the 10-16 NVDA
        stop-out was three trades. Now a position is every entry since the symbol was
        last flat (`_position_entries` walks the fills), one sync's stop legs for a
        symbol make one reflection (`also_closed_by`, listed in `client_order_ids`, which
        `unreflected_exits` also reads) with one `stopout:` incident, and the modeled
        cost covers the exited shares on both sides (a trim used to charge the whole
        entry again). The ledger's 9 reflections were recomputed from the fills and the
        10-16 legs merged (one-off script, kept ids 1-7): 7 trades, gross -$29.09 ->
        -$32.39. Alpaca's own account figures were never affected. Report files already
        written (eod.md for 10-06, 10-15, 10-16; this week's weekly_review.md) keep the
        old numbers.
  - [x] **2026-10-16: a later tranche no longer restarts the time stop (owner chose
        option A of three).** The monitor measured `time_stop_minutes` from the latest
        entry order's fill, and an entry goes in as tranches a cycle apart, so every
        90-minute time stop ran about 120 (10-15 NVDA: tranches at 10:01 and 10:30 CT,
        due 12:01 instead of 11:31; it would have closed in the 12:00 cycle, which still
        had a price, instead of 13:30). Now it counts from the first fill of the latest
        proposal, across all its tranches (`monitor.py::_first_fill_of_proposal`); an add
        is a new decision with its own time stop and still restarts it. Not chosen: from
        the position's first fill, adds included (B), and leaving it (C). Remaining
        lateness is the cadence: time stops are checked only at cycles, every 30
        minutes, so one fires up to 30 minutes after it is due.
  - [x] **2026-10-17: an add no longer uses a new-position slot (owner chose option C of
        three; in effect from 2026-10-20).** An add counted as a new position and as one
        of its symbol's round trips. On 10-15 TSM's add spent the half budget's last
        slot, so NVDA's add was rejected and MU's trigger at 11:30 found no slot; on
        10-16 NVDA's open and add spent both its round trips, so it could not re-enter
        after the stop-out. Now `new_positions_per_day` counts and gates opens only
        (`proposals.py`, mirrored in `context.py`'s `budget_remaining` and
        `review.py`'s scorecard), and an add still uses a round trip, so the per-symbol
        cap allows at most one add per name per day. Applies to the placebo too (same
        `validate_proposal`). Not chosen: keep both (A); adds count toward neither (B),
        which leaves adds bounded only by the single-name cap.
  - [x] **2026-10-19, day 3: one trade, AVGO, -$10.12 gross (-$14.45 net); S3 made
        +$1,070.** All of the day's gain was overnight: SOXX gapped +3.63% and fell 1.83%
        from the open to the close, and all 14 names gapped up and fell during the
        session. S4 holds nothing overnight and was about 2.7% invested at its peak, for
        74 minutes. The trade: an opening-range breakout on weak volume (0.69) at 10:31
        CT, an add at 11:01, a failed break by 11:30. **An 11:45 CT carry closed it** at
        337.65: one of the owner's cron-job.org early-close backstops fired on a regular
        day. The stops (336.60/336.90) would have filled by 12:30, about $7 worse. Placebo
        -$442. Stage 2 so far: S4 -$50 net, S3 -$1,166. First live reconciliation ok;
        one reflection for the 4 entry fills; the Researcher stayed in the universe.
  - [x] **2026-10-19: a mistimed carry does nothing.** `run_carry(near_close_only=True)`
        skips, with a warning incident, while the close (broker calendar) is more than
        60 minutes away; both `carry` commands set it, and the early-close handover in
        `run_cycle` doesn't need it. An unreadable calendar is a warning and carry goes
        ahead. The placebo's fake calendar always closes at 16:00 ET; on an early close
        it follows the real cycle instead (the 2026-10-20 entry below).
        **Owner to check** the two cron-job.org backstops' schedules.
  - [x] **2026-10-19: no further tranche once a trade is halfway to its stop (owner chose
        option B of three).** Tranche continuation sent the next piece regardless; on
        10-19 AVGO's add bought its second half at 337.77 after the breakout failed (339.10
        paid, stop 336.90). Now `run_cycle` skips the rest of a proposal's tranches when
        the price has moved against it by more than half the distance from the average
        paid so far to the invalidation price, and records `tranche_skipped:{id}` (info),
        which keeps it skipped (`ledger.skipped_tranche_proposals`). In Stage 2 so far it
        would have skipped 1 of 6 second tranches (+$0.24). Not chosen: always send (A);
        skip on any move below the first fill (C), which would have skipped 5 of 6 and
        in practice halved most positions.
  - [x] **2026-10-19: the eod report's SOXX benchmark was about 0% every day from
        10-01.** eod-review's own `semimc fetch` runs after the bell, so the cache's
        "prior close" was already today's close, and the benchmark measured today's close
        to an after-hours quote (+0.03% on 10-19, a +1.73% day). It now measures from the
        last close before today (`MarketData.before`), and the report adds SOXX open to
        close (`eod_report.py::open_to_close_return`), so the overnight gap shows. The
        day's attribution now uses the open-to-close move: S4 holds nothing overnight.
        Reports from 10-01 to 10-19 keep their wrong SOXX line and sector split.
  - [x] **2026-10-19: a tranched entry is rounded to whole shares once (owner's go).**
        Each tranche used to be rounded down on its own, so Stage 2's entries bought 79%
        of their approved notional; 3 of the first 6 two-tranche entries lost a share to
        it (AVGO 2 + 2 of 5.9 shares, NVDA 4 + 4 of 9.2). Now `run_cycle` rounds the
        whole position once and splits its shares, smaller piece first (5 go in as 2 + 3,
        `jobs.py::_tranche_qty`); the last tranche buys the rest of the whole position
        at its own price. The odd share goes last so a trade stopped early by the
        halfway rule holds less. Never more than the approved size, except the existing
        one share a later tranche buys after a price rise has used up the rest.
  - [x] **2026-10-20: "do nothing" holds the day's entries, not the open batch's (owner's
        go).** It read "not available" in all 14 eod reports to 10-19: the open batch never
        opened a trade (34 proposals, none an entry), since it runs at 08:32 CT and every
        trigger waits on the 30-minute opening range. Now every entry accepted today is
        held untouched from its fill to the after-close quote, the same question (did
        managing positions during the day help?). 10-19 would have read about -$35 for
        AVGO held to its 334.59 close, against the -$10 it made.
  - [x] **2026-10-20: the placebo follows the real cycle on an early close (owner's go).**
        Its fake broker always reads the market as open, so on 11-27 and 12-24 it would
        have kept trading after the 13:00 ET close on after-hours quotes, and the carry
        guard (60 minutes before a 16:00 close, on its fake calendar) would have held its
        book past the bell. The placebo still never touches the real account: it reads
        the real ledger, which it already opened for the watchlist. Where the real cycle
        just declined on a closed market (`jobs.py::closed_market_note`) the placebo
        declines; where it ran carry before an early close (`early_close_note_prefix`)
        the placebo runs its own. Holidays already needed nothing (no research, no
        watchlist).
  - [x] **2026-10-20, day 4: three names, -$37.81 gross (-$46.97 net); placebo -$1,013.**
        AVGO (open 2 + 3 shares, add 1 + 1), NVDA (open 4 + 5, add 2 + 2) and LRCX (2)
        opened by 09:31 CT, all bought near local highs. LRCX's second tranche was
        skipped by the halfway rule, and the Trader closed it at 10:01 before its earnings
        on 10-21. AVGO's add hit its own stop at 10:12 CT and the rest hit the time stop
        at 11:00; NVDA's time stop at 12:00. The 11:45 CT carry fired again and did
        nothing ("carry skipped at 12:45 New York"), on both books, with 13 NVDA shares
        held. Both adds were allowed by the 10-17 budget rule and lost about $8.6 between
        them. Reconciliation ok, a clean day (third in a row). SOXX -0.38% close to
        close, +1.01% open to close.
  - [x] **2026-10-20: two benchmark fixes and the trade count, from day 4.** (1) The
        SOXX close-to-close figure ran to a live quote at 15:15 CT, which after the bell
        is the after-hours market (501.60/502.15 against a 500.01 close: -0.01% for a
        -0.38% day). Both SOXX figures now come from the price cache's daily bars
        (`eod_report.py::close_to_close_return`). (2) "Do nothing" still read "not
        available" on its first day with entries: after the bell IEX quoted none of
        AVGO, NVDA or LRCX. It now prices each at its last 5-minute IEX bar before the
        bell (`jobs.py::session_closes`; within 0.03% of the official closes that day).
        10-20 would have read -$62.28 against the -$37.81 gross the managed trades lost.
        (3) Readiness counted each reflection as a closed trade, and a position can
        close in steps: AVGO's add stopped out, then the rest on a time stop, two
        reflections for one trade. The Stage 3 rule would have read 8 closed trades and
        passed with 7. It now counts positions closed, flat to flat
        (`reflect.py::round_trip_count`), shown as "7 closed (8 exits)". The playbook's
        20-trade evidence check counts the same way; P&L, win rate and calibration stay
        per exit.
  - [x] **2026-10-21, day 5: one trade, TXN, +$2.58 gross (-$0.19 net).** 52 of 58
        Trader answers were no_trade. Every trigger on the watchlist needed a breakout
        above the opening range on relative volume of 1.1-1.2 or more. SOXX was +2.07%
        close to close but +0.12% open to close, and NVDA, MU, AMAT and AMD sat inside
        their ranges at 0.72-1.12 all day; only TXN (1.40-1.48) triggered. The halfway
        rule skipped its open's second tranche at 11:30 CT; the Trader added at 12:01
        when the trigger fired again, and the time stop closed all 6 shares at 13:30.
        Stage 3 rule 5 of 6 (8 closed trades; 5 of 10 sessions).
  - [x] **2026-10-21: the Researcher writes a mix of setups, shorts included (owner's
        go).** All 52 watchlist candidates since Stage 1 were long setups, nearly all
        breakouts on heavy volume, so a quiet or gap-driven session left nothing to trade
        and all 19 entries were buys. `researcher.md` now asks for a mix (breakouts,
        pullbacks to VWAP, failed breakouts and fades, shorts) and for volume bars no
        higher than the setup needs; `trader.md` says how a short is proposed. A short's
        code path had run only on the placebo's fake book (27 shorts); reviewed for the
        real account: the entry is an OTO sell with a buy stop, the monitor, closes,
        trims, carry and reflections all read the position's side. Added: an equity
        entry's invalidation must sit on its losing side (`validate_proposal`), and
        reconciliation and the risk checks' signed notional sign a position from its
        side, whatever sign Alpaca reports (`jobs.py::_signed`). Shorting is enabled on
        PA33HJGNHLI7 (owner, from its settings page, 2026-10-21; the local `.env` keys open
        the S3 account PA3P3XIM4N7V, so the S4 account can't be read from here). Every
        universe stock and SOXX is `shortable` and `easy_to_borrow` (asset fields, the
        same for every account; read 2026-10-21). Still unverified until the first real
        short: Alpaca's handling of a short OTO's buy stop.
        `reviewer.md` now tells the placebo and "do nothing" apart, which the Reviewer
        had mixed up two days running.
  - [x] **2026-10-22: code sizes every stock entry from its stop (owner chose option B
        of three).** Over Stage 2's 17 entries the Trader's own `size_pct` averaged 2.0%
        of equity (about $2k), losing about 0.03% of equity at a typical 1.3% stop,
        while `risk_per_trade_pct` (0.75%) allowed the 15% single-name cap on every
        one. The average matched the prompt's own example ("0.02 is 2%"), and 21 of 32
        Researcher size bands said "small". Now `proposals.py::stop_sized_pct` sizes
        each equity open and add to lose `risk.target_risk_per_trade_pct` (new, 0.001)
        of equity at its invalidation, capped by the room left under the single-name cap;
        the Trader's `size_pct` is replaced before validation, and the ledger records the
        size used. At Stage 2's stops (0.6-3.5%, median 1.3%) that is 3-15%, about 7-8%
        typical; three positions about 22% gross, and all three stopping out about 0.3%
        of equity. Options not chosen: A, no change (Stage 3 doubles the count limits
        only); C, 0.25% at the stop (15%, the cap, nearly every trade). The placebo now
        sizes by the same rule from its 2% stop (5%, was 15%), so its P&L compares like
        for like; its figures from before 10-22 are at three times the risk.
        `trader.md` drops the 2% example and says why the invalidation must not be
        moved to change the size. Watch: whether the Trader starts tightening stops.
- [ ] Stage 3 -- equities, full budget. **Promotion rule set 2026-10-16, before Stage 2's
      results (owner chose option A of three):** promote once (1) Stage 2 has run 10
      sessions (`readiness.stage_3_sessions`; from `stage_2_started` 2026-10-15, so
      10-28 at the earliest), (2) the ledger matched the account at every close since
      reconciliation began, (3) the latest clean-day streak is at least 3 days and
      includes accepted trades (a day lost to an outside data outage may be excluded,
      as 10-06 was), (4) at least 8 closed trades since Stage 2 began
      (`stage_3_min_closed_trades`), (5) each main exit path has worked on the real
      account: a stop and a time stop (both seen), plus carry or a Trader close (owner
      may waive: 90-minute time stops rarely leave anything for either), and (6) no day
      at the daily hard-loss limit. P&L is not a criterion. If not all met on 10-28,
      Stage 2 continues day by day. Not chosen: calendar only (B; could promote on 3-5
      trades) and 20 trades plus 5 clean days (C; likely early November at half
      budget). The readiness report has a "Stage 3 rule" section checking these.
      **Built with it: S4 reconciliation.** Nothing in S4 ever recorded one, so
      preflight's "yesterday reconciled" check (the brief's) passed vacuously, on the
      real account too. `jobs.py::reconcile_positions` compares the ledger's net fills
      per symbol with the broker's positions; `run_eod_review` runs it after its sync,
      every run. A mismatch is an error incident and fails the next preflight; an
      unreachable broker is a warning and leaves the day unreconciled. First real run:
      eod-review on 2026-10-19.
- [ ] Stage 4 -- options in shadow, 2 weeks (priced and logged, not traded). **Built
      ahead of turn, 2026-09-26**: schema/config/gates (Phase 12a) and the real Alpaca
      options market-data fetch plus `jobs.py` wiring (Phase 12b) are both done -- Stage 4
      is *runnable* once the owner sets `intraday.options.shadow_pricing: true`, not yet
      *run*, the same "built vs. run" distinction already drawn for Stage 1.
- [ ] Stage 5 -- options enabled, only if the shadow results and both options gates pass,
      and only with the owner's explicit approval. Never live at any stage.

## Forward forecast journal (added 2026-09-27)

- [x] **Built** (`journal.py`, `semimc journal record [--month-end-only]` / `semimc journal
      score`, `.github/workflows/forecast_journal.yml`). Why: every favourable backtest
      number is at least mildly contaminated -- `drift.policy` was chosen on the same
      2012-2026 window it is reported on (MODEL_NOTES section 7), and the Phase 4B holdout
      is spent -- so the only clean evidence left is forecasts written down before their
      outcomes exist. Each month-end, the workflow fits every `journal.variants` entry
      (gbm, bootstrap, and garch_t under `shrunk_historical`/`capm`/`risk_free` -- the
      drift question put to a clean test) for the 14-ticker universe at the latest close
      and appends 99 quantiles of the simple return per horizon to `journal/forecasts.csv`
      (append-only, git-tracked: the commit is the pre-registration timestamp). Guards:
      no backdating (latest trading day only); refused once the next session could have
      opened (next weekday 09:30 New York -- the exact contamination test, replacing a
      first-draft calendar-day age limit); an already-recorded origin is skipped, never
      overwritten; `--month-end-only` records only on the last weekday of a month (a month
      ending on an exchange holiday is skipped, a few times a decade, rather than recorded
      a session late -- no market calendar in this project). Scoring reuses the
      backtester's outcome definition from current adjusted prices and reports mean CRPS
      (quantile-grid approximation, `analytics/scoring.py::crps_from_quantiles`, within
      ~1% of the closed form for a normal), skill vs `journal.baseline`, mean PIT,
      coverage, and the independent-origin count -- **no significance tests until that
      count reaches a couple of dozen** (two years of month-ends at 21 days; longer
      horizons slower). ~300 KB of CSV per month-end. Verified end to end on the real
      universe locally (280 forecasts, 9 s, origin 2026-09-25 -- a trial file, not
      committed: the first real entry is the workflow's 2026-09-30 run).

## Known gaps (deliberate, with reasons)

- [x] **`garch_t` simulated occasional explosive paths at long horizons -- fixed
      2026-09-27** (owner chose option (b)). Fits with `nu` below 4 (AMD at 2018-11-30:
      alpha 0.32, nu 3.3) or persistence at 1 (INTC at 2025-04-30) produced a few absurd
      paths (a 7,800x day in one reproduction) that dominated the backtest's energy-form
      CRPS while barely moving quantiles. Now each simulated conditional variance is
      capped at `models.garch_t.variance_cap_multiple` (2.0, fixed before backtesting)
      times the fit's in-sample peak. Backtest before/after: 252-day mean CRPS 0.473 ->
      0.334 (gbm 0.333), every other number unchanged past the fourth decimal
      (research_log.md). Not addressed: single extreme t innovations at `nu` near 3 can
      still produce a very large one-day move (the cap bounds variance, not innovations).
- [x] **Parameter uncertainty tried, no improvement** (2026-09-27): `garch_t_pu`
      (MODEL_NOTES section 18) draws each path's GARCH parameters from the MLE's asymptotic
      covariance, restricted to the stationary region. Backtested at 21/63/126/252 days
      against `garch_t` and gbm: median CRPS within noise, coverage marginally narrower,
      mean PIT unchanged (research_log.md). Kept as an opt-in model; not a default, not a
      journal variant.

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

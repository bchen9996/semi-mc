# CLAUDE.md

Working notes for this repo. Read `BRIEF.md` for what the project is for and
`BRIEF_ADDENDUM.md` for the Phase 4/4B extensions; this file is the how.
`BRIEF_INTRADAY.md` is the S4 intraday-agent brief -- filed 2026-09-19, **not started**,
see `tasks/todo.md`'s S4 section for its prerequisites. Note that
`semis_paper_trading_brief.md`, cited by every `paper`/`execution`/`live`/`strategy`
docstring, was never committed and does not exist in this repo -- those citations are
dangling, and `tasks/todo.md`'s paper-trading section plus `THESES.md` are the closest
written record of what it said.

## Stack

Python 3.12 (pinned in `.python-version` — `pyarrow` has no wheels for the newest
interpreter), managed with `uv`, packaged as `semimc` in a `src/` layout. Dependencies are
pinned in `uv.lock`.

numpy · pandas · scipy · arch (GARCH, block-length estimator) · statsmodels (Driscoll-Kraay,
quantile regression, Markov switching — **never `import statsmodels.api`**, see Gotchas) ·
scikit-learn (pinball loss) · matplotlib · pyarrow · pydantic v2 (config validation) ·
typer (CLI) · pyyaml · yfinance · lxml (earnings-date scraping via `pandas.read_html`).
Dev: pytest, ruff. The dashboard (`src/semimc/dashboard`) is stdlib `http.server` plus a
static HTML/CSS/JS frontend — no extra dependency, no install step.

## Commands

```bash
uv run pytest                                      # all tests, no network
uv run ruff check . && uv run ruff format .        # lint + format
uv run semimc fetch [--risk-free] [--earnings] [--industry]   # download / refresh cached data
uv run semimc validate                             # data quality + universe statistics
uv run semimc simulate --ticker NVDA --horizon 126 --model garch_t
uv run semimc simulate --portfolio "NVDA=0.5,AMD=0.5" --horizon 126
uv run semimc simulate --ticker NVDA --horizon 21 --tilt   # + the Phase 4B drift tilt
uv run semimc backtest --models gbm,bootstrap,garch_t --horizons 21,63
uv run semimc signals --horizon 21 [--holdout]      # evaluate the Phase 4B signals
uv run semimc scenario --name sector_shock_25
uv run semimc report --run latest
uv run semimc models                                # list available models
uv run semimc dashboard [--port 8765] [--no-open]   # HTML dashboard, browser front end
uv run semimc journal record [--month-end-only]      # forward forecast journal: write today's forecasts
uv run semimc journal score                         # score journal forecasts whose horizon has passed
```

Every command takes `--config PATH` and repeatable `--set key.path=value` overrides
(parsed as YAML, so use forward slashes in paths).

Regenerate test fixtures with `uv run python tests/fixtures/make_fixtures.py`.

## Conventions

- **Type hints throughout. NumPy-style docstrings that state array shapes and units.**
- **Log returns for simulation, simple returns for portfolio aggregation.** Never mix
  these. A portfolio's log return is not the weighted average of its constituents' log
  returns.
- **Shapes.** `ReturnModel.simulate` returns `(n_paths, horizon, n_tickers)` daily log
  returns. Always.
- **One `np.random.Generator`, passed explicitly.** Never seed globally. A run must be
  reproducible from the seed recorded in its manifest.
- **Vectorize.** Loop over time steps only where a model genuinely requires it (the GARCH
  recursion). Never loop over paths.
- **Chunk.** 10,000 paths × 252 days × 15 tickers in float64 is ~300 MB. Simulate in
  chunks, reduce on the fly, keep full paths only for the series being charted, in float32.
- **Comparisons and comments explain *why*, not what.** The non-obvious maths is in
  `MODEL_NOTES.md`; code comments point at it rather than restating it.

## Architecture rules

- **`config.py` is the only place defaults live.** The pydantic schema uses
  `extra="forbid"`, so an unknown key is an error — a silently ignored `drift.policy` would
  invalidate every downstream result.
- **`MarketData.training_window` and `MarketData.context` are the only places a fit window
  is cut.** That is what makes the point-in-time guarantee checkable in one place.
- **Models never decide their own drift.** `BaseReturnModel` imposes the configured policy.
  The default is `shrunk_historical`, chosen by backtest over 2012-2026 — which is
  selection on the reporting period, and is flagged as such in MODEL_NOTES section 7.
  `capm` is the conservative alternative and is one flag away.
  Subclasses implement `_fit_shape`, `_simulate_zero_mean`, and `annualized_variance`.
- **The provider interface is the only route to price data.** Nothing imports yfinance
  outside `data/provider.py` (and the optional earnings/FRED/options helpers:
  `data/earnings.py`, `signals/external.py`, `data/options.py`). `data/options.py` is
  live-only by nature (today's option chain, no historical equivalent exists) and is
  never called from anywhere point-in-time-sensitive -- only `simulate --implied-vol`.
- **Charts return Figures; they do not write files.** The caller decides where output goes,
  which is why the same code serves the CLI, the HTML report and a notebook.
- **A drift tilt is a separate, auditable layer, not a change to the drift policy.**
  `FitContext.drift_tilt` (a `DriftTilt`) is applied in `BaseReturnModel.simulate` on top
  of `daily_log_drift`, which stays exactly what the configured policy alone implies. This
  is what lets `--tilt` be optional everywhere and the policy stay recoverable from a
  fitted model even when a tilt is active.
- **Signals and regressions never bypass `MarketData`.** `build_full_panel`
  (`forecasting/pipeline.py`) is the only place a Phase 4B panel is assembled in a loop; it
  calls `build_regression_row`, which calls `signal_panel`, which is the only caller of the
  four ticker-level signal builders. One choke point per data path, same as `training_window`.
- **The dashboard drives the CLI; it does not reimplement it.** `dashboard/web/app.js`
  builds a `semimc <command> ...` argument list from form input, the same way a terminal
  user would, and POSTs it to `dashboard/server.py` (stdlib `http.server`), which hands it
  to `dashboard/jobs.py` to run as a subprocess. `simulate`/`backtest`/`signals`/`scenario`'s
  actual logic lives in exactly one place, `cli.py`, same reasoning as every other choke
  point above. `jobs.py` has no web-framework import so it stays covered by the normal test
  suite with no extra install step — there is no optional `dashboard` dependency any more.

## Boundaries

**Always**

- Keep fitting strictly point-in-time. Every fit goes through `training_window`, and
  `assert_point_in_time` guards the backtester.
- Use adjusted prices. NVDA and AVGO both split 10-for-1 in 2024; unadjusted data reads
  those as a 90% crash.
- Apply the configured drift policy to every model. None may inherit the sample mean.
- Make every run reproducible from its saved config and seed.
- Log every data fix, fill or exclusion. `validation.py` only ever *reports*.
- Run `pytest` and `ruff` before ending a phase, and update this file when conventions
  change.

**Ask first**

- Dependencies beyond the list above, paid data sources, or API keys.
- Changing the model interface, the default assumptions (drift, fit window, universe), or
  the scoring metrics.
- Changing the phase order in `tasks/plan.md`.
- Scraping any website. TSMC monthly revenue and SIA global sales were both ruled out for
  this reason; the industry-cycle signal uses FRED instead (`signals/external.py`).
- Adding fixed effects to the Phase 4B panel regression, or signals beyond the addendum's
  list of five.

**Never**

- Let post-origin data reach a fit or a model choice. The one deliberate, documented
  exception is the earnings *schedule* — see `FitContext.truncated` and MODEL_NOTES §4.
- Hit the network in tests, or commit secrets.
- Silently fill, drop or winsorize data.
- Present output as a price target or point prediction. Always distributions and
  probabilities.
- Tune a model on the same periods used to report its backtest results without saying so.
- Use a Phase 4B signal's value, or a supply-chain link's existence, before its own
  `release_date`/`as_of` — enforced in `signals/external.py:as_of` and
  `signals/supply_chain.py:links_known_at`.
- Use a smoothed regime probability as a simulation's starting state. Filtered only.
- Look at the Phase 4B holdout result before the final, one-time evaluation
  (`semimc signals --holdout`). Not yet run against the real universe — see
  `research_log.md`.

## Testing

`tests/fixtures/*.csv` are committed synthetic data with known properties: a GJR-GARCH with
known parameters, Student-t innovations, a market and sector factor, scheduled earnings
jumps, adjusted OHLC generated from a Brownian bridge scaled to a trailing realized-vol
proxy (so Garman-Klass has something genuine to measure), and one deliberately corrupted
file (`BROKEN.csv`) carrying an unadjusted 10-for-1 split, a gap, a stale run and a
zero-volume day. Regenerate with `uv run python tests/fixtures/make_fixtures.py`.

Tests never touch the network: the test config sets `data.provider: csv` pointed at that
directory. A `network` marker exists and is deselected by default.

Tolerances are justified by Monte Carlo error, not tuned until green. Where a statistic is
compared to a closed form, the closed form is written out in the test.

## Gotchas found the hard way

- **Chunk size must not change results.** Subtracting a draw's *realized* mean before
  adding drift makes output depend on how paths were grouped. Models draw shocks that are
  zero-mean by construction; the drift is exact in expectation, not per sample.
- **Cache staleness compares the requested start date, not the first row returned.** ARM
  listed in 2023 and will never have 2000 data; comparing against its first row marked it
  stale on every run and refetched it forever.
- **Truncating the earnings calendar to the origin silently disables the overlay.** The
  schedule is known ahead; the reaction is not. Estimation uses `calendar.truncated(as_of)`.
- **A Cholesky factor applied to Student-t draws breaks the marginals.** Use a Gaussian
  copula: correlate normals, push through `Phi`, then the `t` quantile function.
- **GARCH needs returns rescaled by 100** or the optimizer stalls on a flat likelihood.
  `MarkovRegression` (the `regime` model) hits the identical failure mode at the identical
  scale — non-finite filtered probabilities while *reporting* convergence — and is fixed
  the same way.
- **Near-integrated GARCH fits are common.** At persistence ≥ 1 the long-run variance does
  not exist; the code falls back to the one-step forecast and warns.
- **Never `import statsmodels.api`.** It pulls in the Kalman-filter state-space code, whose
  compiled `_kalman_filter`/`_qn` extensions are blocked by Windows Application Control on
  this machine. Every submodule actually used here (`regression.linear_model`,
  `regression.quantile_regression`, `tsa.regime_switching.markov_regression`,
  `stats.sandwich_covariance`) imports fine on its own — import those directly.
  `tests/test_imports.py` runs every module in a fresh subprocess and would catch a
  regression here. The blocking itself is Smart App Control (see the "DLL load failed" gotcha below);
  the Code Integrity log shows `_kalman_filter` and `_qn` each blocked once, on 2026-09-15.
  Keeping `statsmodels.api` out still helps: every extra compiled module loaded is one more
  file Smart App Control can refuse.
- **`uv run semimc ...` can fail with `Failed to spawn: semimc ... An Application Control
  policy has blocked this file. (os error 4551)`** on this machine -- the same Windows
  Application Control mechanism as the `statsmodels.api` gotcha above, this time blocking
  the compiled console-script entry point (`.venv/Scripts/semimc.exe`) itself rather than
  a compiled extension. Confirmed persistent (not a one-off transient flag), first seen
  2026-09-19. Workaround: `uv run python -m semimc.cli ...` instead of `uv run semimc
  ...` -- runs the identical `typer` app without going through the blocked `.exe`.
  Unaffected: the GitHub Actions workflow (`.github/workflows/paper_trading.yml`), which
  runs on Linux (`ubuntu-latest`), where this Windows-only policy doesn't exist. Same
  mechanism as the next gotcha: the Code Integrity log records Smart App Control blocking
  `semimc.exe` twice on 2026-09-19, with no later attempts logged, since everything has
  used `python -m` since then.
- **A test fails with `ImportError: DLL load failed ... An Application Control policy has
  blocked this file`: re-run it before debugging anything.** Diagnosed 2026-09-29 from the
  Windows Code Integrity log (`Microsoft-Windows-CodeIntegrity/Operational`, event 3077).
  **Smart App Control** is on for this machine. It blocks unsigned compiled files that
  Microsoft's cloud reputation service doesn't vouch for, and many `.pyd` extensions in
  `.venv` are unsigned. The blocks come in **bursts**: one file refused for roughly 20-40
  minutes, then allowed again with nothing changed. On 2026-09-29, 13 tests (the
  `signals` CLI tests and `test_signals_forecasting.py`) failed on
  `sklearn/preprocessing/_csr_polynomial_expansion` from 17:16 to 17:50 and passed
  unchanged once the burst ended. Since 2026-09-15 it has hit about 18 different files:
  scipy (7), scikit-learn (3), statsmodels (3), matplotlib, curl_cffi, `pytest.exe` and
  `semimc.exe`. So it is not one bad file, and dropping a dependency won't fix it (scipy
  and statsmodels are blocked too, and the project can't run without them). Smart App
  Control has no per-folder exclusions. The only complete fix is turning it off (Windows
  Security → App & browser control → Smart App Control), an owner decision, since on some
  Windows 11 versions it can't be turned back on without resetting Windows. Owner's
  choice as of 2026-09-29: left on. To check whether a failure is this and not a real
  one, look for the blocked path in that log:
  `Get-WinEvent -LogName Microsoft-Windows-CodeIntegrity/Operational -MaxEvents 50 |
  Where-Object Id -eq 3077`. GitHub Actions runs on Linux and is never affected.
- **A demeaned series divided by a time-varying scale is not zero-mean.** HAR's
  standardized residuals looked demeaned (numerator was `x - x.mean()`) but dividing by a
  scale that correlates with return sign reintroduces a small mean, and bootstrapping that
  sample as innovations injected real drift into every simulated path. Fixed by
  demeaning-and-rescaling *after* cross-ticker alignment, the same pattern `fhs.py` already
  used — see MODEL_NOTES §10.
- **HAR needs OHLC; an older cache silently doesn't have it.** `PriceCache` carries a
  `schema_version` in its metadata (currently 2, added for adjusted open/high/low) so a
  pre-addendum cache is detected as stale and refetched rather than read with columns that
  don't exist.
- **`semimc signals --holdout` wrote into the real repo's `research_log.md` during a
  fixture-based CLI smoke test**, because it resolved the log path from `cfg.project_root`
  directly instead of through `cfg.path()`, and `project_root` is not one of the things a
  `--set` override can redirect (`load_config` sets it *after* applying overrides). Fixed
  by adding `signals.research_log` (default `"research_log.md"`, resolved like every other
  config-relative path) and using `cfg.path(cfg.signals.research_log)` instead — tests
  redirect it with an absolute `-s` override. Any future command that writes outside
  `reporting.runs_dir` needs the same pattern, not a hand-rolled path off `project_root`.
- **A GitHub concurrency group holds one *pending* run; a newer arrival cancels it,
  silently.** `cancel-in-progress: false` only protects the *running* job. On 2026-10-07 a
  dispatched intraday `cycle` most likely vanished this way behind GitHub's own hours-late
  cron copies, so five intraday crons were removed (2026-10-08). Anything added to
  `intraday-trading-ledger-write` must not arrive while a dispatched job may be waiting.
- **A workflow job never re-runs its command on a rejected push.** It commits once and
  pushes through `.github/scripts/push_ledger.sh`, which rebases onto the latest main.
  The old reset-and-re-run loop repeated each cycle's Trader call, and on the real account
  would have discarded the ledger rows of orders already sent. Exit 3 means another run
  changed the same (binary) ledger: intraday jobs fail on it; paper jobs, whose sleeves
  share one ledger and are idempotent, re-run. `tests/test_push_ledger.py` drives the
  script against local repos and pins which jobs keep a cron.

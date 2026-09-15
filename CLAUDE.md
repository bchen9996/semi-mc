# CLAUDE.md

Working notes for this repo. Read `BRIEF.md` for what the project is for; this file is the
how.

## Stack

Python 3.12 (pinned in `.python-version` — `pyarrow` has no wheels for the newest
interpreter), managed with `uv`, packaged as `semimc` in a `src/` layout. Dependencies are
pinned in `uv.lock`.

numpy · pandas · scipy · arch (GARCH, block-length estimator) · matplotlib · pyarrow ·
pydantic v2 (config validation) · typer (CLI) · pyyaml · yfinance. Dev: pytest, ruff.

## Commands

```bash
uv run pytest                                   # all tests, no network
uv run ruff check . && uv run ruff format .     # lint + format
uv run semimc fetch [--risk-free] [--earnings]  # download / refresh cached data
uv run semimc validate                          # data quality + universe statistics
uv run semimc simulate --ticker NVDA --horizon 126 --model garch_t
uv run semimc simulate --portfolio "NVDA=0.5,AMD=0.5" --horizon 126
uv run semimc backtest --models gbm,bootstrap,garch_t --horizons 21,63
uv run semimc scenario --name sector_shock_25
uv run semimc report --run latest
uv run semimc models                            # list available models
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
  Subclasses implement `_fit_shape`, `_simulate_zero_mean`, and `annualized_variance`.
- **The provider interface is the only route to price data.** Nothing imports yfinance
  outside `data/provider.py` (and the optional earnings/FRED helpers).
- **Charts return Figures; they do not write files.** The caller decides where output goes,
  which is why the same code serves the CLI, the HTML report and a notebook.

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

**Never**

- Let post-origin data reach a fit or a model choice. The one deliberate, documented
  exception is the earnings *schedule* — see `FitContext.truncated` and MODEL_NOTES §4.
- Hit the network in tests, or commit secrets.
- Silently fill, drop or winsorize data.
- Present output as a price target or point prediction. Always distributions and
  probabilities.
- Tune a model on the same periods used to report its backtest results without saying so.

## Testing

`tests/fixtures/*.csv` are committed synthetic data with known properties: a GJR-GARCH with
known parameters, Student-t innovations, a market and sector factor, scheduled earnings
jumps, and one deliberately corrupted file (`BROKEN.csv`) carrying an unadjusted 10-for-1
split, a gap, a stale run and a zero-volume day.

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
- **Near-integrated GARCH fits are common.** At persistence ≥ 1 the long-run variance does
  not exist; the code falls back to the one-step forecast and warns.

# Project Brief: Monte Carlo Forecasting Engine for Semiconductor Stocks

Read this entire brief before doing anything. It is the source of truth for the project: keep it in the repo, and if scope or decisions change, propose an update to it rather than drifting from it.

## 1. Objective

Build a Python research tool that uses Monte Carlo simulation to produce probabilistic forecasts for U.S.-listed semiconductor stocks: distributions of future prices and returns, probabilities of reaching price levels, and downside risk (VaR, CVaR, drawdowns) for single stocks and portfolios.

Guiding principle: Monte Carlo can't say where a stock *will* go, only the range of where it *could* go under a model's assumptions. So the real product is **calibrated** forecasts. If a model says there's a 90% chance a stock finishes the quarter inside a range, that should happen roughly 90% of the time out-of-sample. Every model is judged by walk-forward backtests against a simple baseline, not by how convincing its charts look.

This is a passion project I want to understand deeply and be able to explain, so clear, documented reasoning matters as much as features.

## 2. How to work with me

1. **Plan before coding.** Propose an implementation plan: architecture, dependencies, tasks per phase with acceptance criteria, risks, and every assumption you're making. Ask only questions that genuinely change the design. Don't write code until I approve.
2. Save the approved plan to `tasks/plan.md` and a checklist to `tasks/todo.md`, and keep the checklist current so work survives across sessions.
3. **Work phase by phase** (Section 6). End each phase with passing tests, the exact commands for me to try what you built, and a short summary of decisions and surprises. Then stop for my review. After I approve, commit the phase with a descriptive message.
4. When a modeling or data choice is ambiguous, lay out the options and trade-offs and ask. Don't guess.
5. Document the reasoning: docstrings for non-obvious math, and a `MODEL_NOTES.md` covering each model's equations, assumptions, parameters, and known failure modes in plain language.
6. Prefer simple, readable, tested code over clever abstractions. No features beyond this brief without asking.

## 3. Defaults (configurable in `config/default.yaml`)

| Setting | Default |
|---|---|
| Universe | Compute/fabless: NVDA, AMD, AVGO, QCOM, MRVL, ARM · Foundry/IDM: TSM, INTC · Memory: MU · Equipment: ASML, AMAT, LRCX, KLAC · Analog: TXN, ADI |
| Benchmarks | SOXX (sector), SPY (market) |
| Forecast horizons | 21, 63, 126, 252 trading days |
| Simulated paths | 10,000 for forecasts (fewer allowed inside backtests) |
| Fit window | Rolling 5 years of daily data |
| Drift | CAPM-implied: risk-free rate + β × equity risk premium (ERP = 5%) |
| Risk-free rate | Config value for live forecasts; historical 3-month T-bill series (e.g., from FRED) for backtests so drift is point-in-time |
| Risk levels | VaR and CVaR at 95% and 99% |
| Random seed | 42 |

## 4. Tech stack and commands

- Python 3.11+, packaged as `semimc` in a `src/` layout. Use `uv` if installed (otherwise venv + pip) and pin dependencies.
- numpy, pandas, scipy, `arch` (GARCH models and bootstrap utilities), matplotlib (plotly optional), pyarrow (parquet cache), pydantic (config validation), typer (CLI), pytest, ruff.
- Data: `yfinance` for prototyping, behind a `DataProvider` interface so a paid API or CSV import can be swapped in later. Confirm it works during setup. If it's broken or rate-limited, tell me and propose alternatives instead of hacking around it.

Target commands (adjust in your plan if needed, then record the final versions in `CLAUDE.md`):

```bash
uv run pytest                                   # all tests, no network
uv run ruff check . && uv run ruff format .     # lint + format
uv run semimc fetch                             # download / refresh cached data
uv run semimc simulate --ticker NVDA --horizon 126 --model garch_t
uv run semimc backtest --models gbm,bootstrap,garch_t --horizons 21,63
uv run semimc scenario --name sector_shock_25
uv run semimc report --run latest
```

## 5. Project structure and code style

```
semi-mc/
├── CLAUDE.md            # stack, commands, conventions, Section 8 boundaries; create early, keep updated
├── README.md            # setup, usage, limitations, "educational project, not investment advice"
├── MODEL_NOTES.md       # math, assumptions, failure modes for each model
├── config/default.yaml
├── src/semimc/
│   ├── data/            # providers, parquet cache, validation, returns, earnings calendar
│   ├── models/          # base interface, gbm, bootstrap, garch_t, earnings_jumps, dependence
│   ├── engine/          # simulation runner, chunking, portfolio aggregation
│   ├── analytics/       # percentiles, probabilities, VaR/CVaR, drawdowns, convergence
│   ├── backtest/        # walk-forward runner, scoring
│   ├── scenarios/       # stress tests, historical replays
│   ├── reporting/       # charts, HTML reports
│   └── cli.py
├── notebooks/           # exploration only; import from the package, no core logic here
├── tests/               # includes small fixture datasets
├── tasks/               # plan.md, todo.md
└── runs/                # one folder per run (gitignored)
```

Every model implements one interface so models are interchangeable in forecasts and backtests (refine it in your plan if needed):

```python
class ReturnModel(Protocol):
    name: str

    def fit(self, log_returns: pd.DataFrame) -> Self:
        """Estimate parameters from daily log returns (index: dates, columns: tickers).

        Must only ever receive data available at the forecast origin.
        """

    def simulate(self, n_paths: int, horizon: int, rng: np.random.Generator) -> np.ndarray:
        """Return simulated daily log returns with shape (n_paths, horizon, n_tickers)."""
```

Conventions: type hints throughout; NumPy-style docstrings that state array shapes and units; vectorized NumPy, looping over time steps only where a model requires it (e.g., GARCH recursions); one `np.random.Generator` passed explicitly, never global seeding. Full path arrays get big fast (10,000 paths × 252 days × 15 tickers in float64 is about 300 MB), so simulate in chunks, compute path statistics like drawdowns on the fly, and support float32.

## 6. Build phases

Build the scoreboard (Phase 3) before the sophisticated models, so every model after the baseline has to earn its place.

### Phase 1: Walking skeleton (one ticker, end to end)
- Fetch and cache adjusted daily prices for NVDA, SPY, and SOXX. Adjusted prices are mandatory: NVDA and AVGO both split 10-for-1 in 2024, and unadjusted data would read those splits as crashes.
- Daily log returns → GBM baseline with trailing realized volatility (1-year window; EWMA as an option) and configurable drift: CAPM-implied (default), risk-free, historical mean shrunk toward CAPM, or manual. Never default to the raw historical mean; the AI-driven run-up since 2023 would project absurd growth.
- Get the log-space drift right (μ − σ²/2) and annualize with 252 trading days.
- `semimc simulate` outputs: a fan chart (5th/25th/50th/75th/95th percentiles), a terminal price histogram with today's price marked, and a risk table (probability of gain, probability of losing more than 20%, probability of finishing above a user-supplied price, VaR, CVaR, median max drawdown).
- Convergence diagnostic: show how the 5th percentile and CVaR stabilize as the path count grows, with Monte Carlo standard errors.
- Every run writes `runs/<timestamp>/` containing outputs, a config snapshot, the seed, and the data date range.

### Phase 2: Full-universe data layer
- Extend fetching, caching (refetch only when stale), and validation to the full universe, pulling the full available daily history.
- Validation report: failed tickers (delisted or renamed), gaps, stale or zero-volume days, outliers, and short histories (ARM has only traded since September 2023). Never silently drop or fill; log every fix.
- Align tickers on a common trading calendar and handle unequal history lengths explicitly.
- Summary table: annualized volatility, skew, excess kurtosis, betas to SPY and SOXX, worst one-day moves, plus a correlation heatmap.

### Phase 3: Walk-forward backtester (the scoreboard)
- Rolling forecast origins (monthly by default) across as much history as the fit window allows; tickers enter once they have enough history.
- At each origin: fit only on data up to that date, simulate each horizon, and score against the realized outcome. Write a test that fails if any post-origin data reaches `fit`.
- Metrics for each model × ticker × horizon:
  - Coverage of the central 50% and 90% intervals versus nominal.
  - PIT histograms (the realized outcome's percentile within the simulated distribution; should be roughly uniform).
  - CRPS computed from samples, plus a CRPS skill score versus the GBM baseline.
  - VaR exception backtests: Kupiec proportion-of-failures and Christoffersen independence tests.
- Overlapping horizons produce dependent errors: use non-overlapping windows for significance tests or clearly caveat them. Report sample sizes; 126- and 252-day horizons will have few independent observations, so don't overinterpret them.
- Cache fitted parameters per origin, and profile before optimizing.
- Output a model-comparison scorecard, run first on GBM to establish the baseline.

### Phase 4: Realistic single-stock dynamics
Semiconductor stocks have fat tails, volatility clustering, and big earnings gaps. Add each feature as its own model and score it with the Phase 3 backtester. Every model must follow the configured drift policy: demean historical or filtered returns before resampling and re-center on the configured drift, so no model quietly inherits the sample mean.
- **Stationary block bootstrap** of historical returns, with block length from `arch`'s optimal block length estimator (document the choice).
- **GJR-GARCH(1,1) with Student-t innovations** via `arch`: simulated volatility starts at today's conditional volatility and mean-reverts. Rescale returns (e.g., ×100) for optimizer stability and convert back.
- **Earnings-jump overlay** that wraps any base model: build an earnings calendar (from the provider if reliable, otherwise a CSV I can edit), map before-open versus after-close reports to the correct reaction day, estimate each ticker's empirical distribution of earnings-day abnormal returns (return minus β × benchmark return), and inject a jump on each scheduled earnings date inside the horizon. Strip the abnormal earnings component from the base model's training data (e.g., replace reaction-day returns with β × benchmark return) so jumps aren't double counted.
- For each model, report whether it beats the baseline, where it fails (e.g., 2020 and 2022 stress periods, earnings windows), and why.

### Phase 5: Multi-asset dependence and portfolios
Semis move together, and their correlations spike in selloffs. Implement and backtest two approaches:
- **Factor model:** a market factor (SPY), a sector factor (SOXX returns residualized on SPY), and idiosyncratic residuals, each with its own dynamics.
- **Filtered historical simulation:** GARCH-filter each ticker, bootstrap standardized residual vectors *by date* (the whole cross-section at once, preserving correlation and tail co-movement), then rescale with simulated conditional volatilities.
- Portfolio simulation from user weights (equal-weight default): aggregate with simple returns, not log returns; optional periodic rebalancing; portfolio VaR/CVaR, drawdown distribution, and each holding's contribution to CVaR. Backtest portfolio forecasts too.

### Phase 6: Scenarios, reporting, polish
- Stress scenarios: an instantaneous sector shock propagated through betas; a volatility multiplier for N days; custom per-ticker shocks (e.g., a hypothetical export-control hit to names I tag as China-exposed); historical replays such as March 2020 or 2022, scaled to current betas.
- `semimc report` builds a self-contained HTML report per run: forecasts, risk tables, scenario results, the backtest scorecard, and a limitations section (survivorship bias from using today's semiconductor leaders, regime changes, parameter uncertainty).
- Finish the README (setup, usage, example outputs, limitations) and `MODEL_NOTES.md`.

### Stretch (only if I ask)
- Signal-conditioned drift or volatility (e.g., momentum, sector trend, earnings proximity). A signal stays only if it improves out-of-sample CRPS and coverage over the unconditioned model.
- A Markov regime-switching model for the semiconductor cycle.
- Calibrating volatility to options-implied volatility.
- A Streamlit dashboard.

## 7. Testing strategy
- pytest with fixture files only (no network), seeded RNGs, and explicit tolerances.
- Statistical correctness: GBM simulated log-return mean and variance match closed-form values; correlated simulations reproduce a target correlation matrix; VaR/CVaR functions match closed forms for a normal distribution; sample-based CRPS matches the closed-form normal CRPS; bootstraps draw only from the training window.
- Leakage: the Phase 3 look-ahead test.
- Data: split-adjustment handling, and the validation report catches gaps and outliers injected into fixtures.
- Smoke tests that every CLI command runs end to end on fixture data.

## 8. Boundaries

**Always**
- Keep fitting strictly point-in-time.
- Use adjusted prices; log returns for simulation, simple returns for portfolio aggregation.
- Apply the configured drift policy to every model; none may silently inherit the historical sample mean.
- Make every run reproducible from its saved config and seed.
- Log every data fix, fill, or exclusion.
- Run tests and ruff before ending a phase; update `CLAUDE.md` when conventions change.

**Ask first**
- Adding dependencies beyond Section 4, paid data sources, or API keys.
- Changing the model interface, default assumptions (drift, fit window, universe), or scoring metrics.
- Changing the phase order.

**Never**
- Let post-origin data leak into fitting or model selection.
- Hit the network in tests or commit secrets.
- Silently fill, drop, or winsorize data.
- Present outputs as price targets or point predictions; always show distributions and probabilities.
- Tune a model on the same periods used to report its backtest results without flagging it.

## 9. Success criteria
- `semimc simulate` produces a fan chart, terminal distribution, and risk table for any ticker or weighted portfolio in the universe, in under a minute on a laptop at 10,000 paths (excluding data download).
- `semimc backtest` produces a scorecard comparing every model with the GBM baseline on coverage, PIT, CRPS skill, and VaR tests, with an honest written summary of which models are calibrated and where they break.
- All tests pass without network access, and ruff is clean.
- `README.md`, `CLAUDE.md`, and `MODEL_NOTES.md` are complete enough for someone new to reproduce every result.

## 10. Open questions to raise in your plan
- Market factor: SPY or QQQ? Sector factor: SOXX or SMH?
- Is CLI plus HTML reports the right final interface, or should the plan leave room for a dashboard?
- Flag anything in this brief that looks inconsistent, over-scoped, or statistically questionable.

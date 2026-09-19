# Paper-trading theses

Pre-registered before any sleeve has traded for real (`paper.registered_accounts` is
empty and every sleeve's `trading_enabled` is false as of this writing) -- the entire
point of writing this down now, not after there is a result to be tempted by.

The original brief that first numbered these theses T1-T5 no longer exists as a document
(not in this repo, not recoverable). T2/T3/T5 below were reconstructed from what the
codebase already implies -- `strategy/decide.py::decide_vol_twin_shadow`'s own docstring
already calls itself "S3's shadow twin (T3)" -- and confirmed with the project owner
before being written here. Confirmed 2026-09-19.

Each thesis has a **minimum history** before its comparison is treated as anything more
than descriptive, tied to `paper.review_months[0]` (currently 3 months). This is a
caveat, not a hard refusal the way `semimc signals --holdout` refuses a second run:
live P&L doesn't get "spent" by being looked at early the way a holdout window does, so
the risk here is premature *decision-making* (turning off a sleeve after two bad weeks),
not statistical contamination. `semimc paper evaluate` prints the numbers regardless and
adds the caveat below the minimum.

## T2 -- does vol-targeting improve on the equal-weight control?

**Claim**: S3 (vol-targeted, scaled toward `paper.s3_target_vol` using a Monte Carlo
forecast, capped at `paper.s3_max_leverage`) achieves better risk-adjusted outcomes than
S0 (equal-weight, no sizing at all) once both are traded for real.

**Comparison**: `analytics.paper_evaluation.evaluate_t2` -- S0's and S3's own real,
broker-confirmed equity curves (`Ledger.snapshots_for`), each reduced to annualized
return, annualized vol, Sharpe (`market.risk_free.rate_at`), max drawdown, VaR/CVaR
(`analytics/risk.py`), and a block-bootstrap confidence interval on the *difference* in
annualized return and annualized vol between the two curves.

**Decided by**: whether that CI excludes zero for vol (S3 should show lower realized vol
than S0 if the targeting is doing anything at all) and whether Sharpe is higher for S3
without materially worse drawdowns.

**Minimum history**: 3 months of overlapping snapshots for both s0 and s3.

**Status**: not yet evaluated -- no trading history (neither sleeve has `trading_enabled`
or a registered account yet).

## T3 -- does the model-based vol forecast beat trailing realized vol?

**Claim**: S3's Monte Carlo forecast (fit fresh at each rebalance, `strategy/forecasts.py
::portfolio_vol_forecast`) produces a better-sized portfolio than simply sizing off
trailing realized volatility (`vol_twin_shadow`, `strategy/forecasts.py::
trailing_portfolio_vol`) -- i.e. that the forecasting model is earning its complexity.

**Comparison**: `analytics.paper_evaluation.evaluate_t3` -- S3's real equity curve against
`vol_twin_shadow`'s *synthetic* mark-to-market replay (`replay_shadow_equity_curve`,
built from its recorded decisions and realized prices, since it never actually trades),
reduced the same way as T2.

**Decided by**: the same vol/Sharpe/drawdown comparison as T2. A genuinely null result
here (no difference) is itself informative: it would mean the model-fitting machinery
this whole project is built around isn't earning its keep over the much simpler trailing
estimate, for sizing purposes specifically.

**Minimum history**: 3 months of decisions recorded for `vol_twin_shadow` (it has no
snapshots to wait on, since it never trades) overlapping 3 months of real s3 snapshots.

**Status**: not yet evaluated -- no trading history.

## T5 -- after-the-fact execution cost analysis

**Claim**: the assumptions baked into `execution/orders.py` (a collared marketable limit,
`paper.execution.collar_bps` = 20bps by default) produce realistic, bounded execution
costs relative to the price actually prevailing when each order was submitted -- i.e.
paper trading isn't quietly assuming away costs a live-equivalent account would actually
pay.

**Comparison**: `analytics.execution_cost.execution_cost_report` -- every filled order's
own fill price against an independent "arrival price" (the NBBO midpoint at
`order.submitted_at`, pulled from Alpaca's market-data API via `execution/market_data.py`
-- a source `execute` itself never consults when pricing the order, so this is a real,
independent check, not circular), expressed as signed slippage in bps per fill.

**Decided by**: whether realized slippage stays within a small multiple of the assumed
collar, and whether it's systematically one-sided (a sign of a pricing assumption that's
wrong in a specific, exploitable direction, not just noisy).

**Minimum history**: enough filled orders to say something -- unlike T2/T3 this isn't
time-gated so much as fill-count-gated; treat fewer than ~20 fills as anecdotal.

**Status**: not yet evaluated -- no fills exist yet.

## Explicitly out of scope

- **T1** (relative-strength signal tilt, S1): the underlying signal already failed its
  one-time pre-holdout bar and, separately, its real holdout evaluation
  (`research_log.md`) -- "paper trading can't rescue a signal that failed it" is this
  project's own standing rule. No T1 entry exists to evaluate.
- **T4** (sector-timing shadow overlay, `sector_shadow`): shadow-only by construction
  (`strategy/decide.py::decide_sector_shadow`, no `paper.sleeves` entry exists that could
  make it tradeable) and explicitly gets no verdict before the 12-month review
  (`paper.review_months[-1]`) -- see `tasks/todo.md`'s Phase 2 entry for the original
  reasoning.

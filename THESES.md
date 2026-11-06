# Paper-trading theses

Pre-registered before any sleeve has traded for real -- the entire point of writing this
down now, not after there is a result to be tempted by. (As of this writing, `s3` is
enabled and trading; `s0` and S4 are not. T6 below was pre-registered before S4 exists in
code at all, ahead of that same rule.)

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

## T6 -- does the S4 intraday agent add anything over a placebo and SOXX?

Pre-registered ahead of S4's own first order, per `BRIEF_INTRADAY.md`'s own instruction --
S4 does not exist in code yet (`tasks/todo.md`'s S4 section, Phase 0 only). Written now,
in the same pre-registration spirit as T2/T3/T5, so the first real result isn't the first
time this project decided what would count as a positive one.

**Claim**: a scheduled, LLM-driven research-and-trade loop (Researcher/Trader/Reviewer
roles reading news and market data, proposing trades validated against risk limits held
in code, `BRIEF_INTRADAY.md`) produces better risk-adjusted returns, net of modeled
costs, than the same risk controls applied to a placebo agent, and than holding SOXX.

**Placebo agent**: identical sizing, execution, and risk controls as the real S4 loop, but
entries drawn at random from the morning watchlist instead of the Trader's judgment. Runs
on its own shadow ledger every day S4 runs, never submitting real orders.

**Comparison**: the deciding measure below is `intraday/t6.py::evaluate_t6`, run as
`semimc intraday t6 --as-of <verdict date>`, which writes `reports/<date>/t6.md` with the
verdict (built 2026-11-06, tested on made-up exits only; it refuses to run before
2027-01-15). Reported alongside it, not yet in one place: S4's real equity curve, the
placebo's shadow equity curve, and SOXX buy-and-hold on net P&L per trade, expectancy, hit
rate, average win vs. average loss, exposure-adjusted return, beta to SOXX, turnover,
slippage vs. arrival price, time in market, confidence calibration, and process scores
(per `BRIEF_INTRADAY.md`'s "Evaluation" section; the readiness, end-of-day and weekly
reports carry most of these). The verdict rests on one measure, below.

**Decided by** (owner decision 2026-11-04, from a power analysis on the first exits; see
the caveat):

- **Measure.** Every exit since Stage 2 began (2026-10-15) that `intraday/hedge.py` can
  price: P&L net of modeled costs plus the measured SOXX hedge's P&L, in basis points of
  the exit's entry notional. The hedge is measured, not traded, so its cost isn't charged.
  The statistic is the agent's mean minus the placebo's. Each mean's standard error is
  clustered by trading day, since exits on one day aren't independent; the difference's
  is the root of their sum of squares.
- **Power.** Over the first exits each trade's sector-removed result varied by about 72
  bps (agent) and 97 bps (placebo). At about 2 agent and 6 placebo exits a session,
  2027-01-15 should hold about 125 and 340 exits, enough to detect a gap of about 25 bps
  per trade (80% power at 2 standard errors), against a 16 bps modeled round trip. By
  2027-04-15, about 250 and 700 exits: about 17 bps. Clustering by day widens both.
- **2027-01-15, 3 months after Stage 2 began.** Agent ahead by 2 or more standard errors:
  the loop adds something, and it runs on to the 6-month verdict. Behind by 2 or more: S4
  stops trading (`intraday.trading_enabled: false`). In between: it runs on to 6 months.
- **2027-04-15, 6 months.** Anything short of 2 standard errors ahead: S4 stops trading. A
  pass decides nothing further; more size or real money would be a separate owner decision.
- **Caveat.** Written after 30 agent exits (-26.8 bps per exit before costs, standard error
  13.1) and 48 placebo exits (-27.8, 14.0) had been seen. Their spread set the power
  figures, but their means were in view too. The rule is fixed now so later results can't
  move it.

**Minimum history**: monthly review once trading; formal verdicts at 3 and 6 months
(`BRIEF_INTRADAY.md`'s own review cadence, distinct from `paper.review_months`):
2027-01-15 and 2027-04-15, above.

**Status**: not yet evaluated. Real paper orders since Stage 2 (2026-10-15); Stage 3, the
full budget, since 2026-11-04. The measure is built (`intraday/t6.py`, 2026-11-06) and
has not been run on the real ledgers; its first run is the 2027-01-15 verdict. It also
counts the exits it can't measure: one agent exit is already among them, QCOM's on
2026-11-05, whose proposal was lost with a failed push (-$44.34 before costs). The
readiness report's sector-removed table still shows each sleeve's mean before costs with
an unclustered error. The rest of this paragraph is the 2026-09-21 record.
As of 2026-09-21, the placebo agent itself is built and
running on schedule (`intraday/placebo.py`, `semimc intraday placebo open-batch|cycle|
carry`, S4 Phase 7) on its own shadow ledger (`ledger/intraday_shadow.sqlite3`) -- but
S4's real account is still `trading_enabled: false` (`tasks/todo.md`'s "Remaining
prerequisites"), so neither curve has real trades yet. No claim above changes until both
do; `evaluate_t6` still doesn't exist.

## Explicitly out of scope

- **T1** (relative-strength signal tilt, S1): the underlying signal already failed its
  one-time pre-holdout bar and, separately, its real holdout evaluation
  (`research_log.md`) -- "paper trading can't rescue a signal that failed it" is this
  project's own standing rule. No T1 entry exists to evaluate.
- **T4** (sector-timing shadow overlay, `sector_shadow`): shadow-only by construction
  (`strategy/decide.py::decide_sector_shadow`, no `paper.sleeves` entry exists that could
  make it tradeable) and explicitly gets no verdict before the 12-month review
  (`paper.review_months[-1]`) -- see `tasks/todo.md`'s Phase 2 entry for the original
  reasoning. Its first three decisions (2026-09-19, 10-01, 11-02) had no FRED series on the
  runner and fell back to plain S0; the job fetches it since 2026-11-02. That changes no
  weights yet: the multiplier is capped at 1.0, so only a negative forecast moves it. On
  today's revised FRED data that happened at 6 of the last 95 month starts, all in 2019, by
  at most 1.6% of exposure. While the forecast stays positive T4 is S0, and the review may
  have nothing to compare.

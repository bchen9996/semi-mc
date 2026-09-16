# Research log

Every specification tried for the addendum's Phase 4B signals, including the ones that
found nothing — required by the addendum ("record every specification tried ... including
failures"). Entries are append-only; nothing here is edited after the fact, only appended
to. `semimc signals --holdout` appends automatically when it runs.

---

## 2026-09-15 — build session: scope decisions

Three questions were raised in `tasks/plan_addendum.md` before any Phase 4B code was
written, and answered by the user via three choices rather than by default:

1. **Industry-cycle data source.** TSMC monthly revenue and SIA global semiconductor sales
   both need scraping (an ask-first boundary) and neither has a free API. Chose FRED's
   `IPG3344S` (semiconductor and related electronic component manufacturing, industrial
   production index) — no scraping, no key, but a revised-only series with an
   *approximated* release date (`period + 45d`). Revision bias is a real, unmeasured
   limitation on this signal specifically, not on the others.
2. **Holdout horizon.** Kept the addendum's 24-month holdout, but the headline verdict is
   reported only at 21 days; 24 months leaves only ~8 non-overlapping 63-day origins,
   which is too few to carry a conclusion on its own. The 63-day holdout cell is still
   computed and printed, flagged `low_power`.
3. **Customer lead-lag universe.** Added 9 customer tickers (AAPL, MSFT, GOOGL, AMZN, META,
   TSLA, DELL, HPQ, CSCO) to `signals.customer_tickers`, used only as a signal input, never
   simulated. `data/external/supply_chain_links.csv` records 21 hand-curated links, each
   dated `as_of = 2026-09-15` — the day this file was written — so the signal is correctly
   inert before that date in any backtest, not silently wrong.

## 2026-09-15 — pre-holdout signal evaluation, 21-day horizon

- **Universe:** NVDA, AMD, AVGO, QCOM, MRVL, TSM, INTC, MU, ASML, AMAT, LRCX, KLAC, TXN, ADI
  (ARM excluded: insufficient history). Benchmarks SPY, SOXX.
- **Sample:** monthly origins, `backtest.start` (2012-01-01) through 2026-09-15, cut to the
  24-month holdout boundary (2024-09-15) so nothing after it was touched. 127 dates
  survived the point-in-time filter for momentum/reversal/relative_strength; 102 for
  earnings_drift (needs an active report within the last 60 trading days).
- **Method:** 5-fold walk-forward cross-validation on unique dates, 1-period gap between
  train and test folds (`forecasting.cv.date_grouped_gapped_splits`), Driscoll-Kraay pooled
  regression per fold (`maxlags = round(1.0 * 21 / 21) = 1`).
- **Command:** `semimc signals --tickers NVDA,AMD,AVGO,QCOM,MRVL,TSM,INTC,MU,ASML,AMAT,LRCX,KLAC,TXN,ADI --horizon 21`
- **Run:** `runs/20260915-235555-signals`

| signal | n_obs | cv_oos_r2 | clark_west_t | clark_west_p | rank_ic_mean | rank_ic_t | rank_ic_n_dates |
|---|---|---|---|---|---|---|---|
| relative_strength | 2128 | -0.0013 | 1.627 | 0.052 | 0.0419 | 1.756 | 127 |
| reversal | 2128 | -0.0039 | -0.584 | 0.720 | 0.0067 | 0.228 | 127 |
| momentum | 2128 | -0.0088 | 0.627 | 0.266 | 0.0021 | 0.077 | 127 |
| earnings_drift | 1419 | -0.0090 | -1.235 | 0.892 | 0.0198 | 0.659 | 102 |
| combined (Driscoll-Kraay, all 4) | 1419 | -0.0261 | 0.528 | 0.299 | 0.0349 | 1.042 | 102 |

Sector tilt (industry cycle → sector factor, IPG3344S): coef = -0.0112, t = -0.31,
R² = 0.0003, n = 152. Run `20260915-235555-signals` (fetched with `semimc fetch --industry`
immediately before).

**Read:** every signal's OOS R² is negative — the zero-tilt null beats all of them on raw
prediction error at 21 days. `relative_strength` is the closest to nominal significance
(Clark-West p = 0.052) but not there. The combined regression is worse than any single
signal, consistent with overfitting four signals on ~100-160 dates. The sector tilt found
nothing, as expected with one series.

## 2026-09-15 — pre-holdout signal evaluation, 63-day horizon

- Same universe, sample, and method as above, `maxlags = round(1.0 * 63 / 21) = 3`.
- **Command:** `semimc signals --tickers NVDA,AMD,AVGO,QCOM,MRVL,TSM,INTC,MU,ASML,AMAT,LRCX,KLAC,TXN,ADI --horizon 63`
- **Run:** `runs/20260915-235614-signals`

| signal | n_obs | cv_oos_r2 | clark_west_t | clark_west_p | rank_ic_mean | rank_ic_t | rank_ic_n_dates |
|---|---|---|---|---|---|---|---|
| relative_strength | 2128 | -0.0035 | 2.364 | 0.009 | 0.0713 | 2.034 | 127 |
| earnings_drift | 1419 | -0.0086 | 1.024 | 0.153 | 0.0917 | 2.341 | 102 |
| reversal | 2128 | -0.0079 | 0.636 | 0.263 | 0.0538 | 1.835 | 127 |
| combined (Driscoll-Kraay, all 4) | 1419 | -0.0386 | 1.997 | 0.023 | 0.0881 | 2.017 | 102 |
| momentum | 2128 | -0.0206 | 0.693 | 0.244 | 0.0020 | 0.052 | 127 |

Sector tilt: coef = 0.0086, t = 0.13, R² = 0.0001, n = 152.

**Read:** `relative_strength` and the combined model are nominally significant on
Clark-West here (p = 0.009 and 0.023), and `relative_strength`/`earnings_drift` have
rank-IC t-stats above 2. All four are still short of the addendum's own "~3" skepticism bar
for a search this size, and every OOS R² is still negative — a model can be statistically
distinguishable from a zero forecast and still lose to it on raw squared error, which is
what happened here throughout. Not treated as a discovery.

**Decision:** on this evidence, `--tilt` stays off by default in every command. The
mechanism (signal store → point-in-time panel → Driscoll-Kraay/Fama-MacBeth → shrinkage →
coefficient bootstrap → `DriftTilt` → `simulate`) is fully built and tested; the case for
using it live is not there yet. Revisit after the holdout is eventually spent, or if the
combined-via-simple-average alternative the addendum suggests (not yet implemented; see
`tasks/todo.md`) changes the picture.

## 2026-09-16 — the ladder's remaining rungs: simple average, ridge, lasso, quantile regression

Before touching the holdout, three items from `tasks/todo.md` were closed out: the simple
average of single-signal forecasts (the addendum: "start with the simple average ... often
more robust than one regression containing every signal"), ridge/lasso, and quantile
regression. This order is deliberate, not incidental — the addendum's own "Never" boundary
says "look at holdout results before the final evaluation," and its "Models, simple to
complex" section places the simple average and ridge/lasso *before* the holdout section
entirely. The 2026-09-15 entries above already showed the kitchen-sink `combined` regression
underperforming every single signal at both horizons — spending the one-time holdout on a
specification already known pre-holdout to be the weakest candidate would have wasted it.

**What changed in the code:** `forecasting/combine.py` adds `CandidateSpec` (a uniform
fit/predict contract every rung of the ladder implements, so cross-validation and the
eventual one-time holdout fit share exactly the same two calls), `fit_simple_average`
(one univariate Driscoll-Kraay regression per signal, combined by row-wise mean over
whichever components have a usable value — a row missing one signal still gets scored, not
dropped), `fit_ridge_or_lasso` (`sklearn`, alpha chosen by the same pre-holdout CV folds
used to score it — disclosed, not hidden: this optimistically biases the reported ridge/
lasso R² slightly, since the same folds pick and score the alpha; a true nested CV would
starve the folds below the >=30-row floor the fits need), and `fit_quantile_regressions`
(`statsmodels` `QuantReg` at 0.05/0.25/0.5/0.75/0.95, scored by cross-validated pinball
loss). `semimc signals` now selects the candidate with the best pre-holdout CV OOS R² as
the frozen specification for `--holdout`, replacing the previous hardcoded `combined`.

**Command:** `semimc signals --horizon 21` then `semimc signals --horizon 63` (same
universe/sample/method as 2026-09-15; `ridge_lasso_alphas = [0.03, 0.1, 0.3, 1, 3, 10, 30]`).
**Runs:** `runs/20260916-005947-signals` (21d), `runs/20260916-010024-signals` (63d).

**21-day horizon** (best pre-holdout candidate: `relative_strength`, cv_oos_r2 = -0.0013,
simple_average a close second at -0.0014):

| signal | n_obs | cv_oos_r2 | clark_west_t | clark_west_p | rank_ic_mean | rank_ic_t |
|---|---|---|---|---|---|---|
| relative_strength | 2280 | -0.0013 | 1.823 | 0.034 | 0.0512 | 2.258 |
| simple_average | 2280 | -0.0014 | 1.135 | 0.128 | 0.0406 | 1.822 |
| reversal | 2280 | -0.0044 | -0.608 | 0.729 | 0.0074 | 0.259 |
| lasso(alpha=0.03) | 1419 | -0.0055 | -1.159 | 0.877 | NaN | NaN |
| momentum | 2280 | -0.0075 | 0.910 | 0.181 | 0.0198 | 0.790 |
| earnings_drift | 1419 | -0.0090 | -1.235 | 0.892 | 0.0198 | 0.659 |
| ridge(alpha=30) | 1419 | -0.0206 | 0.288 | 0.387 | 0.0382 | 1.117 |
| combined | 1419 | -0.0248 | 0.525 | 0.300 | 0.0393 | 1.152 |

**63-day horizon** (best pre-holdout candidate: `simple_average`, cv_oos_r2 = **+0.0004**,
the only positive OOS R² seen anywhere in this project's Phase 4B evaluation to date):

| signal | n_obs | cv_oos_r2 | clark_west_t | clark_west_p | rank_ic_mean | rank_ic_t |
|---|---|---|---|---|---|---|
| simple_average | 2280 | +0.0004 | 2.297 | 0.011 | 0.0999 | **3.268** |
| relative_strength | 2280 | -0.0019 | 2.783 | 0.003 | 0.0909 | 2.763 |
| reversal | 2280 | -0.0080 | 0.753 | 0.226 | 0.0537 | 1.936 |
| earnings_drift | 1419 | -0.0086 | 1.024 | 0.153 | 0.0917 | 2.341 |
| lasso(alpha=0.1) | 1419 | -0.0132 | -1.094 | 0.863 | NaN | NaN |
| momentum | 2280 | -0.0196 | 0.989 | 0.161 | 0.0280 | 0.800 |
| ridge(alpha=30) | 1419 | -0.0283 | 1.708 | 0.044 | 0.0803 | 1.834 |
| combined | 1419 | -0.0362 | 2.038 | 0.021 | 0.0828 | 1.895 |

Quantile regression (all signals, cross-validated pinball loss): symmetric across levels at
both horizons (21d: 0.0093/0.0222/0.0271/0.0222/0.0087 at q=.05/.25/.5/.75/.95; 63d:
0.0161/0.0395/0.0487/0.0401/0.0158) — no evidence of the tails moving differently from the
centre; not surprising given the mean-forecast candidates above are themselves weak.

**Read:** the addendum's own prediction held up. `simple_average` beats the kitchen-sink
`combined` regression by a wide margin at both horizons (e.g. 63d: -0.0362 vs +0.0004), and
at 63 days it is the first candidate in this project's entire Phase 4B search to post a
positive OOS R², a Clark-West p below 0.05, *and* a rank-IC t-statistic (3.268) above the
addendum's own "~3" skepticism bar simultaneously. `relative_strength` alone is close
behind and also clears Clark-West significance at both horizons. Ridge and lasso do not
help here — lasso zeroed every coefficient at every alpha the grid tried strong enough to
beat the noise (hence the `NaN` rank IC: a constant prediction has no rank to correlate),
and ridge sits between the single signals and `combined`, as shrinkage toward zero should.
This is one genuinely promising pre-holdout result after two prior evaluations that found
nothing — treated with proportionate caution, not as a discovery, until the holdout below
either confirms or contradicts it.

**Decision:** `simple_average` is the specification each `--holdout` run below will be
spent on — selected here, from pre-holdout cross-validation alone, before either holdout
window has been read. `--tilt` stays off by default; this result alone does not clear the
bar for shipping a live tilt, and won't until the holdout has been seen.

## 2026-09-16 — holdout, spent

The holdout below is the one-time evaluation the addendum's own rule requires: run once,
report whatever it shows. Two horizons are run and logged, per the 2026-09-15 scope
decision — 21 days is the headline; 63 days leaves only ~8 non-overlapping origins in 24
months and is reported but flagged low-power, not treated as a tiebreaker against 21d.


## 2026-09-16 -- holdout evaluation (21d)

- Tickers: NVDA, AMD, AVGO, QCOM, MRVL, ARM, TSM, INTC, MU, ASML, AMAT, LRCX, KLAC, TXN, ADI
- Specification (selected by pre-holdout CV, before any holdout row was read): relative_strength
- Signals: relative_strength
- Pre-holdout training rows: 2280
- Holdout window: 2024-09-15 onward (345 rows)
- OOS R^2: -0.0057
- Clark-West t: -0.69 (p=0.756)
- Run: 20260916-010126-signals

## 2026-09-16 -- holdout evaluation (63d)

- Tickers: NVDA, AMD, AVGO, QCOM, MRVL, ARM, TSM, INTC, MU, ASML, AMAT, LRCX, KLAC, TXN, ADI
- Specification (selected by pre-holdout CV, before any holdout row was read): simple_average
- Signals: momentum, reversal, relative_strength, earnings_drift
- Pre-holdout training rows: 2280
- Holdout window: 2024-09-15 onward (315 rows)
- OOS R^2: -0.0058
- Clark-West t: -0.94 (p=0.825)
- Run: 20260916-010145-signals

**Read:** null at both horizons, whatever each held. Note each horizon independently froze
its own pre-holdout winner (21d: `relative_strength`; 63d: `simple_average` -- the
"Decision" line above named `simple_average` in general terms before both selections were
actually made per-horizon; this is what the pre-registration rule produced, not a
deviation from it). The 63-day `simple_average` result that looked like the first real
finding in this project's Phase 4B search (cv_oos_r2 = +0.0004, Clark-West p = 0.011,
rank-IC t = 3.27, all pre-holdout) did not replicate: OOS R² -0.0058, Clark-West t = -0.94.
This is precisely the outcome the holdout exists to catch -- a specification search across
five single signals, a kitchen sink, a simple average, two shrinkage methods and five
quantile levels, at two horizons, across three evaluation dates, will produce something
that clears a "~3" t-stat by chance; the addendum names this risk explicitly, and it is
what happened here. 21 days, the pre-registered headline, is unambiguously null.

**Decision:** `--tilt` stays off by default. Both holdout cells are now spent and must not
be re-run against this universe/window under any config change -- a different result on a
rerun would be evidence of leakage or of re-fishing, not a better estimate. Nothing in this
search justifies shipping a live drift tilt; the mechanism remains fully built and tested
for whenever new, genuinely out-of-sample signals or a longer live track record change the
picture.

**Holdout status: SPENT (2026-09-16, both cells).** Do not run `semimc signals --holdout`
against this universe and holdout window again. A new holdout would require a new frozen
window (e.g. as more data accrues past 2026-09-15) and a fresh pre-registration, not a
retry of this one.

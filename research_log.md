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

**Holdout status: NOT SPENT.** `semimc signals --holdout` has not been run against the real
universe. Do so only when ready to report whatever it shows, per the addendum's own rule.

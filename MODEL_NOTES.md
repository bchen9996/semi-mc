# Model notes

What each model assumes, the mathematics it implements, and where it is known to break.
Written to be read start to finish rather than referred to — if a model's failure mode is
not stated here, that is a gap in the notes, not evidence the model has none.

Throughout: `r_t` is a daily **log** return, `sigma` is annualized volatility, and there
are 252 trading days in a year.

---

## 0. The rule that binds every model: drift

No model is allowed to decide its own expected return. This is the single most important
convention in the project, so it is worth being blunt about why.

NVDA compounded at roughly 70% a year over the five years to 2025. Any model fitted to
that window and allowed to keep its sample mean would project a share price in the
thousands, and every risk number downstream — VaR, drawdowns, probability of loss — would
be computed against a fantasy. The historical mean of a single stock over five years is a
famously terrible estimator of its expected return: its standard error is roughly
`sigma / sqrt(T)`, which for a 55%-volatility name over five years is about 25 percentage
points a year. It is noise with a trend drawn through it.

So the drift comes from a **policy**, set in config, and it is imposed identically on
every model:

| policy | annual arithmetic drift `mu_A` |
|---|---|
| `capm` | `rf + beta * ERP` |
| `risk_free` | `rf` |
| `shrunk_historical` (default) | `w * historical + (1 - w) * capm`, `w = 0.25` |
| `manual` | whatever you say |

The default is `shrunk_historical` because it won the head-to-head backtest in §7 — with
the large caveat, recorded there, that it was chosen on the same period the scorecard
reports. `capm` is the conservative alternative.

`mu_A` is the *arithmetic* expected growth rate, defined so that `E[S_T]/S_0 = exp(mu_A T)`.
Simulators need the **log** drift, which is lower by the Ito correction:

```
daily log drift = (mu_A - sigma^2 / 2) / 252
```

That `sigma^2 / 2` is not a detail. For a 55%-volatility name it is 15 percentage points a
year — larger than the entire CAPM risk premium. Omitting it would make the median path
drift upward by about 15% a year for free.

**How it is enforced.** Each model demeans its training or filtered returns and draws
shocks with a theoretical mean of zero; `BaseReturnModel.simulate` then adds the policy
drift. The realized mean of a finite draw is deliberately *not* subtracted afterwards:
doing so would make output depend on how many paths happened to be simulated in one chunk,
and would hide genuine Monte Carlo error in the mean. The drift is exact in expectation,
not in each particular sample. `tests/test_models.py::test_no_model_inherits_the_sample_mean`
gives a model a 96%-a-year training history and asserts the simulation does not inherit it.

**What this means for the backtest.** Calibration tests are far more sensitive to the
*width* of a distribution than to its centre, so a wrong drift is mostly invisible in
coverage and CRPS at short horizons — but not at long ones, where it dominates. §7 compares
the three policies directly and finds the drift mattered more than the choice of model.
That comparison ranks the policies *on one historical sample*; it does not establish that
any of them is the right expected return. The drift remains an assumption, stated.

---

## 1. GBM — the baseline

**Equation.** `r_t ~ N(mu_log, sigma^2 / 252)`, i.i.d. across days, with a constant
correlation matrix across tickers.

**Parameters.** `sigma` from a one-year trailing realized window (or EWMA with
lambda = 0.94, config `fit.vol_estimator`); correlation from the full fit window;
Cholesky factor of the resulting covariance, with eigenvalue clipping if the sample
covariance comes back slightly indefinite.

**Why it is the baseline.** It is the simplest thing that is not obviously wrong, it has a
closed form to test against, and it is what most people mean when they say "Monte Carlo
simulation of a stock". Every other model has to beat it on the scorecard to justify its
existence.

**Failure modes.**

- *No volatility clustering.* A simulation started the day after a 12% crash is just as
  calm as one started in a quiet August. This is the big one for semiconductors, and it is
  why VaR exceptions from GBM arrive in clusters — which the Christoffersen test detects.
- *Tails far too thin.* A normal distribution puts essentially zero probability on a
  -20% day. NVDA has had several. Measured excess kurtosis on this universe runs 4 to 13;
  a normal has 0.
- *Constant correlation.* Semis decouple in calm markets and move as one in a selloff.
  GBM uses one number for both regimes, so portfolio tail risk is understated exactly when
  it matters.
- *Volatility estimated from a trailing window is stale by construction.* A one-year window
  still contains last October's crash in September.

---

## 2. Stationary block bootstrap

**Method.** Politis and Romano (1994). Resample *blocks* of consecutive historical days
whose lengths are geometric with mean `L`, wrapping at the end of the sample. Blocks
preserve short-range dependence that an i.i.d. bootstrap destroys; the random (rather than
fixed) block length is what keeps the resampled series stationary.

`L` comes from `arch.bootstrap.optimal_block_length` — Politis and White (2004) with the
Patton, Politis and White (2009) correction. When several tickers are simulated together,
the per-ticker estimates are averaged into one, because rows are drawn jointly and using
the maximum would let the single most persistent name dictate the block length for
everyone.

**Cross-section.** Rows are drawn **by date**: the whole cross-section of one historical
day is taken at once, so that day's correlation and tail co-movement survive resampling.

**Drift.** The sample is demeaned before resampling, so the policy drift is the only drift.

**Failure modes.**

- *It cannot invent a day worse than the worst day it has seen.* The support of the
  simulated distribution is bounded by the training window. A five-year window with no
  crash produces a model that believes crashes are impossible. Asserted in
  `test_bootstrap_cannot_exceed_the_worst_historical_day`.
- *It is unconditional.* Blocks are drawn from anywhere in the window regardless of what
  today looks like, so a simulation started mid-crisis will mostly draw calm days.
  Filtered historical simulation (§6) is the fix.
- *It assumes the window is representative of the future.* Five years of an AI capex boom
  bootstraps into five more.
- *Block artifacts.* Wrapping joins the end of the sample to its beginning, creating a
  seam that is meaningless. With `L` around 5 and thousands of paths the effect is small,
  but it is real.

---

## 3. GJR-GARCH(1,1) with Student-t innovations

**Equation.** On demeaned returns `e_t`:

```
h_t = omega + (alpha + gamma * 1[e_{t-1} < 0]) * e_{t-1}^2 + beta * h_{t-1}
e_t = sqrt(h_t) * z_t,    z_t ~ standardized t(nu)
```

Two features earn their place. **Clustering**: a violent day raises tomorrow's expected
variance, so a simulation started in October 2018 stays volatile for weeks rather than
reverting to a five-year average overnight. **Asymmetry**: `gamma` lets a fall raise future
volatility more than a rise of the same size, which is what equity data shows and what a
symmetric GARCH misses. Student-t innovations then supply fat tails on top of the fat tails
that clustering alone produces.

**Simulation starts from today's conditional variance** — the whole point of the model —
and mean-reverts towards `omega / (1 - alpha - gamma/2 - beta)` at the estimated rate.

**Implementation details that matter.**

- Returns are multiplied by 100 before fitting. With daily variances around 1e-4 the
  log-likelihood surface is flat enough that the optimizer stalls; at order 1 it converges.
  Every variance parameter is divided back by 100² afterwards.
- A raw `t(nu)` has variance `nu/(nu-2)`, so innovations are divided by its square root to
  give unit variance — otherwise the GARCH recursion's scale would be wrong by that factor.
- Across several tickers, innovations use a **Gaussian copula**: correlate normals with the
  standardized-residual correlation, push through `Phi`, then through the `t` quantile
  function. Applying a Cholesky factor directly to `t` draws would destroy the marginal
  distribution — a sum of t variables is not t.
- The variance used for the Ito correction is the *average* of the analytic multi-step
  variance forecast over the horizon, not the one-step value, because that is what the
  realized average variance will be in expectation.

**Failure modes.**

- *Optimizer failures.* On short or unusual samples the fit does not converge. The
  backtester records the cell in `failures.csv` rather than substituting a different model
  silently.
- *Near-integrated fits.* Persistence estimates often land at 0.98–0.995. The long-run
  variance `omega/(1-persistence)` then becomes enormous and unstable, and at
  persistence ≥ 1 it does not exist at all; the code falls back to the one-step forecast
  and logs a warning.
- *Parameter uncertainty is ignored.* A persistence of 0.97 with a standard error of 0.02
  is simulated as exactly 0.97, so intervals are narrower than a fully Bayesian treatment
  would give.
- *It has no idea earnings exist.* A scheduled 15% gap is, to this model, a once-a-decade
  tail event. That is what §4 is for.
- *One regime only.* GARCH mean-reverts to a single long-run variance. The semiconductor
  cycle arguably has two.

---

## 4. Earnings-jump overlay

**What it does.** Wraps any base model. On each scheduled report inside the horizon, it
adds a draw from that ticker's own empirical distribution of earnings-day **abnormal**
returns, `r - beta * benchmark`.

**Why abnormal rather than raw.** A ticker that reported on a day the whole market fell 3%
did not have a -3% earnings reaction. Subtracting `beta * benchmark` separates the two.
Beta for this purpose is estimated on *non-earnings* days, so the jumps do not inflate it.

**Reaction-day mapping.** A before-open (`bmo`) report moves that same session; an
after-close (`amc`) report moves the next one. `unknown` is treated as `amc`, the dominant
convention for this universe, and the assumption is logged.

**No double counting.** Each reaction day's return is replaced in the base model's training
data with `beta * benchmark` for that day, so the base model never sees the jumps that are
about to be added back. **This has a cost**: it removes that day's ordinary idiosyncratic
noise along with the jump. At roughly four days a year out of 252, the base model's
variance is understated by about 1.6% of its idiosyncratic component — small, but real, and
it biases the combined model very slightly narrow.

**No free drift.** The jump distribution is demeaned before use. A ticker that
historically popped on earnings had a *drift* contribution, and §0 forbids inheriting one.
Jumps supply variance and skew; the policy still supplies the expected return.

**A stated assumption about dates.** The *date* of a future report is treated as known at
the forecast origin; the *reaction* is not. This is close to reality — companies confirm
the next date weeks ahead and the quarterly cadence is highly predictable — but it is an
assumption, and it is the one place in the package where information from after the origin
(the calendar row) is deliberately used. Estimation of the jump distribution still uses
only reports that had already happened, via `calendar.truncated(as_of)`.

**Failure modes.**

- *It needs a calendar and will not invent one.* With no populated calendar the model
  raises rather than guessing dates. With a calendar that has no *future* dates it logs a
  warning and injects nothing.
- *Small samples.* Eight to forty historical reports is a thin basis for an empirical
  distribution, especially in its tails. The distribution is resampled with replacement, so
  it cannot produce a reaction larger than the largest one observed.
- *Regime shifts in earnings behaviour.* NVDA's reactions since 2023 are not drawn from the
  same distribution as its reactions in 2015.
- *Timing data is the weakest link.* yfinance rarely supplies `bmo`/`amc`, and getting it
  wrong puts the jump on the wrong day.

---

## 5. Factor model

**Equation.** `r_i = b_m * f_market + b_s * f_sector + e_i`, where `f_market` is SPY's log
return and `f_sector` is SOXX **residualized on SPY**. Residualizing makes the two factors
close to orthogonal, so `b_s` reads as sector exposure over and above market exposure
rather than a mixture of the two.

Each piece gets its own GJR-GARCH-t dynamics. Idiosyncratic residuals are forced
independent across tickers — that independence *is* the factor model's assumption, and §6
is the alternative that drops it.

**Why correlations rise in a selloff, for free.** Idiosyncratic variance is roughly fixed
while factor volatility is not, so when the market gets violent the shared component's
share of total variance rises and cross-sectional correlation rises with it. This falls out
of the structure rather than being set by hand.

**Failure modes.**

- *Two factors is not enough.* Memory, equipment and analog names have distinct cycles that
  neither SPY nor SOXX captures, so their residuals are not really independent.
- *Static loadings.* Betas are constant over the fit window. NVDA's beta in 2016 and in
  2024 are not the same number.
- *Circularity.* Every universe member is in SOXX, and NVDA is a large part of it. The
  sector factor is partly the thing being explained. SMH would be worse — it is more
  top-heavy still — which is why SOXX is the default.

---

## 6. Filtered historical simulation

**Method.** Fit a GJR-GARCH to each ticker, divide returns by fitted conditional volatility
to get standardized residuals `z_t`, resample whole **rows** of the residual matrix (the
entire cross-section of one historical day), then run each ticker's GARCH recursion forward
driven by those residuals, starting from today's conditional volatility.

**Why it is worth having.** It keeps the empirical shape of the *joint* distribution — fat
tails, asymmetric crashes, tail dependence — without assuming any parametric copula, while
still being conditional on today's volatility, which a plain historical bootstrap is not.
For a portfolio of names that crash together, that combination is the closest thing here to
an honest tail model.

**Failure modes.**

- *Bounded standardized support.* No simulated day can have a more extreme *standardized*
  residual than the worst one in the window. The volatility scaling means the *returns* can
  still exceed anything in history, which is the point.
- *It inherits every GARCH failure mode from §3*, including fit failures.
- *Residuals are not perfectly i.i.d.* If the GARCH filter is misspecified, the leftover
  structure is resampled as if it were noise.
- *Date alignment costs data.* Only dates where every ticker has a residual can be used, so
  adding a short-history name like ARM truncates the usable window for everyone.

---

## 7. What the scoreboard measures

- **Coverage** — does a nominal 90% interval contain the outcome 90% of the time?
- **PIT** — the outcome's percentile within its own forecast distribution, which should be
  uniform across origins. A central hump means too wide, a U-shape means too narrow, a tilt
  means the drift is biased.
- **CRPS** — a proper scoring rule over the whole distribution, so a model cannot improve
  it by misstating its uncertainty. Reported as a skill score against the baseline, because
  raw CRPS scales with volatility and is incomparable across tickers and horizons. The
  *fair* (unbiased) sample estimator is used so a model cannot win by drawing more paths.
- **Kupiec and Christoffersen** — whether VaR breaches happen at the right *rate*, and
  whether they arrive independently rather than in clusters. A model can pass the first and
  fail the second, and that failure is diagnostic of missing volatility clustering.
- **Diebold-Mariano** — whether a CRPS difference against the baseline is larger than noise.

Every significance test runs on **non-overlapping** origins only. Monthly origins with a
252-day horizon overlap by eleven twelfths; treating those as independent turns a nominal
5% test into something closer to a 30% one. Coverage and PIT use all origins because they
are descriptive, and every cell reports both `n` and `n_independent` so the difference is
visible.

**Low power is the normal state at long horizons.** Cells with fewer than 20 independent
observations are flagged `low_power`. At a 252-day horizon over this history there are
around 15 per ticker, and 15 observations cannot distinguish 90% coverage from 75%
coverage. Those rows are reported, not interpreted.

**Non-overlapping origins fix dependence through time, not across tickers.**
Semiconductor names co-move (pairwise return correlations roughly 0.5-0.7), so a Diebold-
Mariano test or PIT chi-squared that pools `(ticker, origin)` rows as independent
observations is still oversized after the non-overlapping filter -- the effective sample
size is far closer to the origin count than to the ticker-origin count. `analytics/scoring.py`
now runs a **block bootstrap over origins**
(`block_bootstrap_origins`, `diebold_mariano_bootstrap`, `pit_bias_bootstrap_test`) to
correct for this: whole origins, not individual rows, are resampled with replacement, so a
sector-wide move that hit every ticker on one date stays intact -- appearing together, or
not at all -- in each replicate, and the spread of the resulting statistic reflects the
real degree of cross-sectional dependence instead of assuming there is none. Owner decision
2026-09-16: build this rather than continue only disclosing the limitation. Two things are
*not* attempted: a dependence-corrected version of the multi-bin chi-squared statistic
itself (a Rao-Scott-style correction needs the full multinomial covariance structure,
which a few dozen origins cannot estimate reliably), and a correction to Kupiec/
Christoffersen (VaR exceptions are already tested per ticker, not pooled, so the pooling
problem does not apply there). Instead, the PIT correction tests the two summary
statistics the written report already interprets -- mean PIT (centre bias) and tail mass
(width) -- against a block-bootstrap standard error, which is the more modest and more
robust question. `backtest.n_dependence_bootstraps` (default 2,000) controls the replicate
count; both corrections are seeded from the run's own seed, so they reproduce exactly on
rerun. `written_summary` reports the corrected numbers as the read to prefer and drops the
"no correction applied" caveat once they are present.

### What the first real run actually found

177 monthly origins, 2012-01 to 2026-09, 14 tickers (ARM lacks the history), 2,000 paths.

**The widths are fine; the centre is not.** Realized coverage of the nominal 90% interval
was 88–91% at 21 and 63 days for all three models, and PIT tail mass sat at 17–23% against
the 20% a calibrated forecast implies. But the PIT is *tilted*, not U-shaped: mean PIT was
0.56 at 21 days rising to 0.67 at 252 days, meaning outcomes kept landing in the upper part
of the forecast. At a one-year horizon the median realized return was **+25%** against a
median forecast of **+1.8%**.

That is a verdict on the **drift policy**, not on any model — all three are handed the
same drift and differ only in distribution shape, which is exactly why all three show the
same tilt. Semiconductors over this window returned far more than `rf + beta * 5%`.

### The drift policies, backtested head to head

Run with GBM alone, since every model shares the drift and using one model isolates its
effect. Same 177 origins, same tickers, same seed.

| policy | mean PIT 21d | 63d | 126d | 252d | cov 90% (21d) | CRPS 252d | DM vs capm |
|---|---|---|---|---|---|---|---|
| `risk_free` | 0.572 | 0.636 | 0.666 | 0.715 | 0.906 | 0.3769 | +9.2 to +12.3 (worse) |
| `capm` | 0.556 | 0.609 | 0.629 | 0.668 | 0.910 | 0.3475 | — |
| `shrunk_historical` | 0.546 | 0.590 | 0.603 | 0.634 | 0.912 | 0.3333 | −4.5 to −7.3 (better) |

The ordering is monotone and identical on every metric — mean PIT, 50% and 90% coverage,
PIT tail mass and CRPS — at every horizon. `shrunk_historical` is now the default.

**Three caveats, in order of importance.**

1. **This is selection on the test set.** The policy was chosen by looking at the same
   period the scorecard reports, which Section 8 of the brief explicitly says must be
   flagged. Semiconductors rose sharply over 2012–2026, so a policy that leans 25% on the
   realized mean was always going to win here. In a sector downturn it will lose by the
   same mechanism, and `capm` is the more conservative default. The honest summary is that
   the backtest cannot distinguish "better calibrated" from "better matched to a bull
   market" on a single sample.
2. **The tilt is reduced, not removed.** `shrunk_historical` still runs a mean PIT of 0.63
   at a year. No available policy makes the centre unbiased.
3. **The p-values are optimistic.** Diebold-Mariano pools 14 co-moving tickers, so the
   effective sample is far smaller than `n` suggests.

Under either policy the same structural point holds: this is the expected cost of refusing
to extrapolate the historical mean (§0). The policy protects against projecting NVDA's
70%-a-year run-up forward, and pays for it with a centre that was too low in a period when
the sector did extraordinarily well. Neither choice is free.

**Only the earnings overlay beat the baseline.** GARCH-t's CRPS skill against GBM was +0.3%
to +0.5% at 21 to 126 days and slightly negative at 252 — never statistically
distinguishable (Diebold-Mariano p = 0.13 to 0.77). The block bootstrap was *worse* at
every horizon (−0.6% to −0.9%, DM p = 0.003 to 0.037), consistent with its bounded support:
it cannot generate a day worse than the worst day in its window.

The **earnings-jump overlay**, run separately over 2018–2026 (105 origins — the calendar
only reaches back to 2014, and the model needs several years of reports before its first
origin), is the one model that earns its place:

| horizon | CRPS skill vs GBM | DM vs GBM | DM vs garch_t |
|---|---|---|---|
| 21d | +0.92% | −2.93 (p = 0.003) | −4.19 (p < 0.0001) |
| 63d | +0.65% | −0.84 (p = 0.40) | −0.61 (p = 0.54) |
| 126d | +0.73% | +1.05 (p = 0.30) | −0.73 (p = 0.46) |

It beats both the baseline *and* the GARCH it wraps at 21 days, and is indistinguishable
beyond that. The pattern is exactly what the mechanism predicts: a 21-day window contains
precisely one scheduled report, so a 10–20% gap is a large share of total variance; stretch
to 126 days and diffusion swamps it. It is also the one place where adding structure that
matches a real feature of the asset — semis gap on earnings — paid off, as opposed to
adding statistical sophistication in the abstract.

**Everything is overconfident at one year.** 50% coverage fell to ~40% and PIT tail mass
rose to 31–34% at the 252-day horizon for every model. Square-root-of-time scaling of daily
volatility understates the variance of long-horizon returns, and no model here corrects
for it. With only ~14 independent observations per ticker, this is flagged `low_power` —
suggestive, not established.

The practical reading: for this universe and period, statistical sophistication alone
(GARCH, bootstrapping) bought almost nothing over GBM on distributional accuracy. What did
help was modelling a real, known feature of the asset — scheduled earnings gaps — and even
that only at the horizon where it dominates the variance. The drift assumption mattered
more than any of it. That is a more useful result than a scorecard showing the newest model
winning.

---

## 8. Things that are wrong with all of this

- **Survivorship bias.** The universe is today's semiconductor leaders. Every historical
  statistic is measured on a sample selected for having survived, which understates
  volatility and overstates drift by an unknown amount.
- **The drift is an assumption.** See §0. Nothing in the backtest validates it, since the
  default was chosen on the same window it is scored on. Since 2026-09-27 the forward
  forecast journal (`journal.py`) records garch_t under all three non-manual policies each
  month-end, before the outcome exists -- the first clean test of the choice, once enough
  independent origins accumulate (years, not months, at the longer horizons).
- **Parameter uncertainty is not propagated** in any model.
- **Regime change is unmodelled.** Every model assumes the next period resembles a rolling
  five-year window.
- **Monte Carlo error is separate from model error.** The convergence diagnostic quantifies
  the first; only the backtest addresses the second.
- **Models were not tuned on the backtest periods, but they were designed knowing 2020 and
  2022 happened**, which is a milder form of the same problem and is worth remembering when
  reading a favourable scorecard.

---

## 9. Merton jump-diffusion

Daily log return, before drift: `sigma_d * Z + (sum of N jumps, Y ~ N(mu_J, sigma_J^2)) -
lambda_d * mu_J`, with `N ~ Poisson(lambda_d)`. The last term is the **compensator** — it
keeps the jump component's own contribution to the mean at zero, exactly the way every
other model's shocks are zero-mean by construction.

**Relationship to GARCH and the earnings overlay.** GJR-GARCH-t widens its tails by raising
*conditional volatility after* a shock; a jump process can produce a large move straight out
of a calm state, closer to how an unscheduled headline (export controls, a customer
cutting guidance) actually arrives. Scheduled earnings gaps stay with the earnings overlay:
reaction days are excluded from the jump MLE so the two models can be combined without
double counting the same gap twice.

**The compensator falls out of the base class for free.** The base class simulates
`mu_A - V/2 + shocks` where `V` is whatever `annualized_variance()` returns. For
`E[S_T/S_0] = exp(mu_A * T)` this process needs `V = sigma_d^2 + 2*lambda*(k - mu_J)`,
`k = exp(mu_J + sigma_J^2/2) - 1`, which makes the net log drift `mu_A - sigma_d^2/2 -
lambda*k` — the textbook Merton drift — with zero special-casing in `base.py`. The
consequence worth remembering: this `V` is *not* the log-return variance
`sigma_d^2 + lambda*(mu_J^2 + sigma_J^2)`; the two differ by a term of order
`lambda * mu_J^2`, usually small but not zero.

**Weak identification.** On daily data a larger jump intensity offset by a smaller
diffusion volatility gives nearly the same likelihood — the addendum names this explicitly.
`refit_stability()` in `models/merton.py` refits on rolling sub-windows and reports each
parameter's coefficient of variation across refits; treat a CV near or above 1 as "this
parameter is not identified," whatever the point estimate says.

**What it does not model.** Jumps are independent across tickers — an export-control
headline that hits the whole sector at once is not represented as a shared shock here. The
factor model is the one built for shared shocks; a portfolio simulated with Merton alone
will understate the correlation of tail events across names.

---

## 10. HAR volatility

`log RVbar(t+1..t+h) = a + b_d log RV_d + b_w log RV_w + b_m log RV_m`, fit as a **separate
direct regression per horizon** rather than iterating a one-day model forward (which
compounds its own forecast error). The variance proxy is Garman-Klass by default —
`0.5 ln(H/L)^2 - (2 ln2 - 1) ln(C/O)^2` — plus the overnight squared return
`ln(O_t / C_{t-1})^2`, because a range estimator alone misses gaps that happen while the
market is shut, which is exactly when semiconductor names move on earnings and headlines.

**Getting the per-step path back from direct forecasts.** A direct forecast at horizon `h`
pins the *cumulative* variance `C(h) = h * Vbar(h)`. `per_step_variance_path()` recovers the
variance for the steps between two fitted horizons as the forward difference
`(C(h_k) - C(h_{k-1})) / (h_k - h_{k-1})`, so the simulator reproduces every direct
forecast exactly rather than interpolating between averages.

**A bug the drift test caught.** The first implementation demeaned standardized residuals
*before* dividing by a time-varying scale, which does not preserve zero mean when
volatility scaling correlates with return sign (a heteroskedasticity effect). The fix —
demean and rescale to unit variance again *after* alignment across tickers, exactly the
pattern `models/fhs.py` already used — is the same "shocks must be zero-mean by
construction" discipline as everywhere else in this codebase, just caught on the fitting
side instead of the simulation side. `test_addendum_models_impose_the_configured_drift`
would fail again if this regressed.

**Earnings variance replacement.** A report day's realized-variance spike is replaced with
the trailing 22-day median of *non-earnings* days before HAR ever sees it — otherwise HAR
learns to forecast a permanently elevated diffusion volatility for the whole horizon, and
stacking it with the earnings overlay would count the same report twice.

---

## 11. Regime switching

Two-state `MarkovRegression` (`statsmodels.tsa.regime_switching`), variance-switching by
default, ordered so regime 0 is always the calmer one (the fit has no such constraint on
its own, and which fitted index is "calm" is otherwise arbitrary between near-identical
refits).

**Rescaling by 100, for the same reason as GARCH.** At daily log-return scale (~1e-2) the
optimizer stalled with non-finite filtered probabilities even while *reporting*
convergence — the same flat-likelihood problem `garch_t.py` already documents. Returns are
scaled up before fitting and every parameter converted back afterward.

**Filtered, never smoothed.** The simulation starts from `filtered_marginal_probabilities`
at the origin — conditioned only on data up to that day — never the smoothed probability,
which uses observations after it and would leak the future into the one place leakage is
easiest to miss: the starting point. `test_regime_filtered_probability_is_causal` checks
this by filtering a longer sample at a shorter sample's fitted parameters and confirming
the probability at the shared boundary date does not move.

**Drift stays exact.** Regime means enter only as deviations, re-centred so their
*stationary-distribution-weighted* average is exactly zero — not their simple average,
because the calm regime is visited far more often. `switching_mean` defaults to `false`
(variance-only), which makes the drift contract trivially exact; turning it on is a real
statement about the world and is tested separately.

**Daily-to-weekly fallback.** When either regime's expected duration falls under 15 trading
days, the fitter refits on weekly returns instead — a regime that short is a volatility
cluster, not an industry cycle — and maps the result back by dividing variance by 5,
holding the fitted regime fixed across each 5-day block during simulation. Which route was
taken is recorded in `describe()`.

---

## 12. Blending

Whole simulated **paths** are drawn from each fitted component in proportion to its weight,
so a path drawn from Merton still has Merton's jump distribution end to end — never a
pointwise average of two densities, which no single model could actually generate.

**The drift contract holds exactly, not approximately.** Every component targets the same
annual arithmetic drift `mu_A` (the shared policy), so each component's own bias-corrected
daily drift is `mu_A - V_i/2`. Because `mu_A` is linear and shared across components, the
weight-averaged drift equals `mu_A - (sum_i w_i * V_i)/2` — exactly what the base class
computes on its own if the blend reports `sum_i w_i * V_i` as *its* `annualized_variance`.
So `BlendModel` needs no override of `simulate()`, no special case anywhere in the shared
machinery: it draws each path's *shape* from one component's own zero-mean shock generator,
and the base class adds the (correctly blended) drift once, uniformly.
`test_blend_imposes_the_configured_drift` checks the two ways of computing it agree to
floating-point precision, not just approximately.

**Weight optimization scores a real mixture, not a shortcut.** Minimizing a linear
combination of each component's own CRPS always puts every weight on the single best
component — it cannot see the benefit of mixing distributions at all. `pooled_mixture_crps`
instead draws a pooled sample from each candidate weight vector the way a live blend
actually would, and `optimize_crps_weights` runs SLSQP over the simplex against that. This
is also why "equal weights are often hard to beat," per the addendum: the diversification
benefit of mixing is often close to its maximum already at 50/50.

---

## 13. Phase 4B: signals and the regression-based drift tilt

### The point-in-time rule, stated once

A row of any signal may reach a fit at origin `t` only if that row's `release_date <= t`.
For a *training* row there is a second rule: its forward target realizes at
`target_end_date`, and the row is usable only if `target_end_date <= t` too — a 63-day
forward return computed today is not knowable for 63 more trading days. Both are enforced
in one place each (`signals/external.py:as_of`, `forecasting/panel.py:training_rows`) with
tests that fail on violation (`tests/test_signals_forecasting.py`).

### Splitting the forecast: exposure first, signal second

The panel regression predicts each stock's return **net of** its trailing market and
sector-factor beta — not the raw forward return. Betas are estimated on the window
*ending at* the origin, never on the forward window the target comes from (that would leak
the realized covariance into what is supposed to be a residual). This gives the regression
a more stationary target and makes a coefficient interpretable as "return beyond what this
stock's own market/sector exposure already implies" — which is the quantity that is safe
to add to the drift without double-counting CAPM.

### Driscoll-Kraay, and why not something simpler

`cov_type="hac-groupsum"` handles cross-sectional correlation (15 semiconductor names share
a sector factor) *and* serial correlation (forward returns sampled monthly overlap) at
once. A plain cluster-by-ticker or cluster-by-date standard error only catches one half.
Fama-MacBeth (per-date cross-sectional OLS, averaged with a Newey-West standard error) runs
alongside it as the addendum's requested robustness check — it is more robust to a few
extreme dates dominating a pooled fit, at the cost of needing enough names per date to be
meaningful (`min_cross_section`, default 5 — a 2-ticker test fixture cannot exercise this
in the same way the real 14-name universe can, and the tests for it use `min_cross_section
= 2` accordingly).

**No fixed effects, deliberately.** A ticker or sub-industry fixed effect would reintroduce
each stock's own historical average return into the regression — precisely the leak
Section 8 of the original brief bans for every model's drift. The addendum repeats the ban
for this regression by name; it is not implemented, not disabled by a flag.

### Coefficient uncertainty as path dispersion, not a discarded number

`n_coefficient_bootstraps` (default 500) coefficient sets come from resampling **dates**,
not rows, in contiguous blocks (`block_bootstrap_date_indices`) — a row-level bootstrap
would erase exactly the serial correlation Driscoll-Kraay exists to account for. Each
simulated path draws one coefficient set and predicts from it; `DriftTilt.build` centres
the whole cloud on the point estimate, so the spread widens the simulated distribution
without moving its mean off what the point estimate (after shrinkage and the cap) already
set.

### Shrinkage, the cap, and expiry

`drift = policy drift + lambda * tilt`, `lambda` fixed before any holdout evaluation
(`signals.shrinkage_lambda`, default 0.5), the annualized result clipped to
`signals.tilt_cap_annual` (default ±10%) regardless of what the regression says. The tilt
is a **daily, per-path deviation** added on top of the ordinary drift term in
`BaseReturnModel.simulate` — see `_path_drift_tilt` in `models/base.py` — active only for
the first `horizon_days` steps of a simulated path, then reverting to the policy drift.
Because the bootstrap draws are demeaned to the point estimate at construction, this
deviation sums to exactly zero across a chunk-independent draw, which is what keeps the
long-standing "chunk size must not change results" invariant intact for the tilt too.

### The industry-cycle signal is FRED, not TSMC or SIA, and that has a cost

TSMC's monthly revenue and SIA's global semiconductor sales both need scraping — an
ask-first boundary — and neither offers a free, key-free API. FRED's `IPG3344S`
(semiconductor and related electronic component manufacturing, industrial production
index) needs neither, using the same public CSV endpoint `data/riskfree.py` already reads
for the risk-free curve. The cost: FRED serves the **current, revised** vintage with no
published history of earlier vintages, so (a) `release_date` here is an *approximation*
(`period + industry_publication_lag_days`, default 45 days), not a fact, and (b) every
value carries some revision bias — a first estimate for semiconductor industrial
production typically moves in later revisions, so a signal built on the revised number
will look more predictive in-sample than it would have looked in real time. Both are
stated in `signals/external.py`'s docstring and repeated here rather than only in code.

### Customer lead-lag and its survivorship problem

`data/external/supply_chain_links.csv` is hand-curated, not inferred, per the addendum.
Each link carries the date it was recorded (`as_of`) and is usable at an origin only at or
after that date — the same point-in-time discipline applied to the *existence of a
relationship* rather than to a data value, because "AAPL is NVDA's largest customer today"
says nothing about who was buying NVDA chips in 2016. Every link committed in this
project's `supply_chain_links.csv` has `as_of = 2026-09-15` (when the file was written),
so the signal is presently usable only from that date forward in any backtest — it is
correctly inert further back, by design, not by omission. A supplier whose customers were
never fetched into the cache gets `NaN` for this signal, never a substituted zero.

### The holdout, and why its horizon matters

Twenty-four months of monthly origins is 24 observations before non-overlap is enforced;
after it, roughly 24 at 21 days but only about 8 at 63 days — against five candidate
signals, several combination methods, and a shrinkage parameter. The default therefore
keeps the 24-month holdout the addendum specifies, but the *headline* verdict is reported
only at `signals.holdout_headline_horizon` (21 days); the 63-day holdout cell is still
computed and printed, explicitly flagged as low-power, and is not allowed to carry a
conclusion on its own. `semimc signals --holdout` writes the result once and appends it to
`research_log.md`; running it twice against the same data is exactly the thing the
addendum's evaluation rule forbids, and nothing in this codebase automates a second attempt.

---

## 14. What the addendum's signal evaluation actually found

Runs of 2026-09-15 and 2026-09-16 on the real 14-name universe (ARM excluded —
insufficient history), 2012 onward, monthly origins, 5-fold walk-forward cross-validation
with a one-period gap. Every candidate signal, the combination methods, and finally the
24-month holdout are all spent. See `research_log.md` for the exact runs and numbers; this
section is the summary.

**Every mean-forecast candidate's pre-holdout out-of-sample R² was negative or
approximately zero**, including the combined Driscoll-Kraay fit — predicting nothing (the
zero-tilt null the target is already net of CAPM and sector exposure against) beat or tied
every candidate on raw prediction error. `relative_strength` came closest at both horizons
on Clark-West and rank IC.

**The combined multi-signal (kitchen-sink) regression was worse than any single signal**,
at both horizons — a textbook small-sample overfitting signature. The addendum anticipated
this and suggested starting with a simple average of single-signal forecasts rather than
one regression containing every signal; `forecasting/combine.py` implements exactly that
ladder (simple average, then ridge, then lasso, then quantile regression — see §15), and it
held up: `simple_average` beat `combined` by a wide margin at both horizons, and at 63 days
it was, briefly, the first candidate anywhere in this search to post a positive pre-holdout
OOS R² (+0.0004), a Clark-West p under 0.05 (0.011), and a rank-IC t-statistic above the
addendum's own "~3" skepticism bar (3.27) — all at once.

**It did not replicate.** The one-time, 24-month holdout — spent 2026-09-16, with each
horizon's specification frozen from pre-holdout cross-validation alone, before any holdout
row was read — came back null at both cells: 21 days (headline, spec `relative_strength`)
OOS R² −0.0057, Clark-West t −0.69; 63 days (low-power, spec `simple_average`) OOS R²
−0.0058, t −0.94. This is the exact failure mode a holdout exists to catch: a search wide
enough (five signals, a kitchen sink, a simple average, two shrinkage methods, five
quantile levels, two horizons) will eventually clear a "~3" bar somewhere by chance, and
this project's own honest evaluation rule caught it rather than shipping on it.

**The sector tilt found nothing** (t = −0.31 at 21 days, t = 0.13 at 63 days, R² ≈ 0 at
both) — expected, since there is exactly one sector series and the addendum said as much
going in.

**Bottom line:** `simulate --tilt` is fully wired and will apply a regression-based drift
tilt on request, but nothing in this search — pre-holdout or holdout — justifies turning it
on by default. `signals.shrinkage_lambda` defaults to 0.5 and the cap to ±10% annualized
specifically so that even a spurious tilt cannot move a forecast very far; a user who runs
`--tilt` today is, on this evidence, adding noise dressed as signal. **The holdout is now
spent and must not be re-run against this universe and window** — a different result on a
rerun would be evidence of leakage or re-fishing, not a better estimate; a genuinely new
holdout requires a new frozen window and fresh pre-registration.

---

## 15. The combination ladder, and shared sector regimes for the portfolio simulation

### `forecasting/combine.py`: simple average, ridge/lasso, quantile regression

Every candidate on the addendum's "simple to complex" ladder — one regression per signal,
the kitchen sink, the simple average, ridge, lasso — is reduced to a `CandidateSpec`: a
`fit(train) -> model` and `predict(model, frame) -> array`. `evaluate_candidate` is written
once against that pair, so every rung of the ladder is cross-validated through identical
machinery, and the specification `semimc signals --holdout` freezes for the one-time
holdout is chosen the same way every candidate was scored pre-holdout — by best CV OOS R²,
never by which one "looks right."

The simple average (`fit_simple_average`) fits one univariate Driscoll-Kraay regression per
signal and predicts each row as the mean of whichever component models have a usable value
for that row — a row missing one signal still gets scored, not dropped, which is the actual
mechanism behind "often more robust than a regression containing every signal": the kitchen
sink has to also estimate how the signals correlate with each other, on the same few
hundred rows it estimates everything else from.

Ridge and lasso (`fit_ridge_or_lasso`) pick their alpha by the same pre-holdout,
date-grouped, gapped CV folds used to score the result — disclosed as an optimistic bias,
not hidden: a true nested CV would starve the inner folds below the ≥30-row floor the fits
themselves require, given only a few hundred pre-holdout rows split into a handful of
folds. Lasso zeroed every coefficient at every alpha in the default grid strong enough to
beat sampling noise on this data, which is itself informative — the addendum's signals do
not survive an L1 penalty here.

Quantile regression (`fit_quantile_regressions`, `statsmodels` `QuantReg` at
5/25/50/75/95%, scored by cross-validated pinball loss) checks whether a signal moves the
tails without moving the mean. On this universe it did not: pinball loss was symmetric
across levels at both horizons, consistent with the mean-forecast candidates themselves
being weak.

### `models/factor_regime.py`: shared sector regime switching

The addendum's Phase 5 "shared regimes" ask: put regime switching on the sector factor, so
every stock turns turbulent together in a portfolio simulation, instead of each switching
regimes on its own schedule. Applying `RegimeSwitchingModel` directly to a multi-ticker
panel does not do this — its `_simulate_zero_mean` draws each ticker's regime chain from
its own independent `rng` call, so which names are turbulent on a given simulated day is
uncorrelated across tickers except through the (regime-conditional) Gaussian correlation. A
genuine sector-wide selloff, every name entering the turbulent regime on the same simulated
day, would only ever happen by chance.

`FactorRegimeModel` gets this for free by reusing `FactorModel`'s own architecture rather
than changing `RegimeSwitchingModel` at all: every ticker's simulated shock already
includes the *same* simulated sector-factor draw, because that sharing (`factor_shocks @
loadings.T`) is what a factor decomposition already is. Fitting the sector factor's
volatility with `RegimeSwitchingModel` instead of GARCH — one path, shared across every
ticker via the loadings — makes a sector-wide regime switch appear in every ticker's return
on the same simulated day, with no change to how tickers are combined. The market factor
and idiosyncratic residuals keep GJR-GARCH-t, exactly as in `FactorModel`.

One disclosed simplification: `FactorModel` fits the market and sector factors jointly (one
2-column GARCH, correlated innovations between them); `FactorRegimeModel` fits them as two
independent single-series models, since a GARCH fit and a Markov-switching fit cannot share
one joint likelihood. Whatever residual correlation GARCH would have found between the two
factors is dropped — expected to be small, since the sector factor is already SOXX
residualized on SPY by construction, but real and worth naming rather than hiding. A
sector-shared regime chain is what the addendum specifically asked for; a `regime`-switched
*market* factor, or an idiosyncratic-level shared regime, was not, and neither is built.

---

## 16. Options-implied volatility (`simulate --implied-vol`)

The original brief's one remaining stretch item: "Calibrating volatility to
options-implied volatility." Owner decision 2026-09-16: build it as a **live-only**
override of GBM's volatility estimator, not a new stochastic-volatility model and not
part of the backtester — free historical option chains do not exist, the exact reason the
addendum itself gave for declining a Heston model. There is no walk-forward counterpart
to check this against, and that is stated here rather than left to be inferred from its
absence in `semimc backtest`.

**Mechanism.** `data/options.py` fetches the current option chain (`yfinance`) for one
ticker, and recomputes every implied vol itself via a from-scratch Black-Scholes
inversion (`scipy.optimize.brentq`) rather than trusting the exchange-computed
`impliedVolatility` field Yahoo returns — a live check on NVDA during development found
that field reporting 1300%/870% "implied vols" on deep-in-the-money contracts with
`openInterest = 1` and no volume: a stale or non-existent market re-priced through
Black-Scholes with a garbage quote. Only out-of-the-money, liquid quotes (positive bid, ask
above bid, nonzero volume or open interest) within `options.moneyness_band` (default
±10%) of spot are used, volume-weighted per expiry. A target horizon that falls between
two listed expiries is handled by interpolating *linearly in total variance*
(`sigma^2 * T`, additive under a diffusion's independent increments — not linear in
volatility itself), holding flat at the nearer endpoint outside the listed range rather
than extrapolating a slope that was never quoted.

**Wiring.** `GBMModel.implied_annual_vol: dict[str, float]` is a per-ticker override set
*before* `.fit()` by the CLI layer, not inside `_fit_shape` — fitting stays a network-free,
deterministic function of `log_returns` for every model, including this one; only the CLI
command that requests `--implied-vol` ever touches the network. `--implied-vol` requires
`--model gbm` and a single `--ticker` (not `--portfolio`): the feature calibrates one
name's own option chain, which has no portfolio-level meaning.

**What is not attempted.** A full implied-vol smile or a Heston-style stochastic-vol
process — this stays a single scalar annual volatility, exactly the brief's literal ask.
Dividend yield defaults to 0 (`options.dividend_yield`); TXN and ADI pay real dividends, a
known, small, disclosed bias in their Black-Scholes inversion rather than a hidden one.
Same-day (0-DTE) expiries are excluded outright, not inverted.

## 17. The HTML dashboard

The brief's last stretch item: "A Streamlit dashboard." Owner decision 2026-09-16: build
the fuller of two scopes considered — not a viewer sitting on top of runs already made
(the smaller option), but a front end that drives and runs `fetch`/`validate`/`simulate`/
`backtest`/`signals`/`scenario` itself, since `backtest` in particular takes minutes and a
dashboard that could only browse past results would leave the actual waiting still bound
to a terminal. Superseding decision 2026-09-17: rebuild the same scope as a custom
HTML/CSS/JS frontend over a stdlib `http.server` backend, replacing Streamlit outright,
for a purpose-built UI and to drop the only optional dependency the project had — the
owner's stated reason was wanting a better-looking interface, and eventually to read a
run's output into a paper-trading workflow (Alpaca; not yet built, see CLAUDE.md's
"Ask first" list — new dependency, API keys, and a still-open design question about
translating a distribution into a trade decision given the Phase 4B tilt's null holdout).

**Mechanism.** `dashboard/jobs.py` is unchanged: it launches `python -m semimc.cli
<command> ...` as a subprocess — the exact argument list a terminal user would type — and
streams its stdout/stderr back line by line as it runs. This is a deliberate choice over
importing `cli.py`'s command functions directly and calling them in-process: it keeps
`cli.py` as the single implementation every interface (terminal, dashboard) goes through,
the same reasoning as every other choke point in this project (`training_window`,
`build_full_panel`, the provider interface), and it means a long-running command like
`backtest` cannot leave the dashboard process itself blocked in a way that only a page
reload can recover from. `dashboard/server.py` is the new seam above it: a small JSON API
(`POST /api/jobs` launches a command in a background thread and returns a job id; `GET
/api/jobs/<id>?since=N` polls it) plus static file serving for `dashboard/web/` (`index.html`,
`app.js`, `style.css`). `dashboard/web/app.js` is where the Streamlit pages' argument-building
logic moved to — one function per command, each reading its own form fields and POSTing the
resulting args, mirroring `page_simulate`/`page_backtest`/etc. one for one. The Runs &
Reports panel lists existing run folders and renders any of them by requesting
`GET /api/runs/<name>/report`, which (re)builds `report.html` via the same `report` CLI
command on first request and then just serves the file, embedded in an `<iframe srcdoc=...>`
— the dashboard never re-renders a run's tables or charts itself, it always asks
`reporting/html.py` for them, so a report looks identical whether it was opened from a
terminal or a browser. `jobs.py` has no web-framework import and is covered by the normal
test suite (`tests/test_dashboard_jobs.py`); `server.py` is stdlib-only and is covered
too (`tests/test_dashboard_server.py`), so there is no more optional dependency to skip
tests over.

**What is not exposed.** `signals --holdout` has no button here — `server.py` rejects it
outright (`start_job` raises before a subprocess is ever launched). It is a one-time
evaluation, already spent against the real universe (research_log.md, 2026-09-16), and
CLAUDE.md forbids re-running it; a dashboard makes exactly the kind of casual, repeatable
action that a one-time resource must never be easy to trigger. It stays reachable only by
typing the CLI command directly, deliberately.

**What is not attempted.** No job queue beyond an in-memory dict keyed by job id, no
persistence of a run's log across a server restart, and no auth or TLS — the server binds
to `127.0.0.1` only and is meant for one local user, the same trust boundary as the CLI
itself.
Navigating away mid-run does not send the child process any signal to stop, but it also
stops anything from draining its stdout pipe; a command that produced enough output to
fill the OS pipe buffer after that point would block on its own next write rather than run
to completion unattended. In practice nothing here writes anywhere near that much to
stdout, so this is a real but minor caveat, not a load-bearing one. This is a personal,
single-user tool; building multi-job orchestration for it would be solving a problem
nobody has.

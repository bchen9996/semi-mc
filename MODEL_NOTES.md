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
- **The drift is an assumption.** See §0. Nothing here validates it.
- **Parameter uncertainty is not propagated** in any model.
- **Regime change is unmodelled.** Every model assumes the next period resembles a rolling
  five-year window.
- **Monte Carlo error is separate from model error.** The convergence diagnostic quantifies
  the first; only the backtest addresses the second.
- **Models were not tuned on the backtest periods, but they were designed knowing 2020 and
  2022 happened**, which is a milder form of the same problem and is worth remembering when
  reading a favourable scorecard.

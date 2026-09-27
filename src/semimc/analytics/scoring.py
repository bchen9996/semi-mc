"""Scoring rules and calibration tests for the walk-forward backtester.

The brief's premise is that a forecast is judged by calibration, not by how convincing its
chart looks. These are the instruments:

* **Coverage** -- does a nominal 90% interval contain the outcome 90% of the time?
* **PIT** -- where in the simulated distribution did the outcome land? Across many
  origins those ranks should be uniform. A hump in the middle means the forecast is too
  wide; a U-shape means it is too narrow; a tilt means the drift is biased.
* **CRPS** -- a proper scoring rule over the whole distribution, in return units. Lower
  is better, and "proper" means a model cannot improve it by lying about its uncertainty.
* **VaR exception tests** -- Kupiec for the *rate* of breaches, Christoffersen for their
  *clustering*. A model can have exactly the right number of exceptions and still be
  broken if they all arrive in one week.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
from scipy import stats


def crps_from_samples(samples: np.ndarray, observed: float) -> float:
    """Continuous Ranked Probability Score estimated from a sample.

    Uses the energy form ``CRPS = E|X - y| - 0.5 * E|X - X'|`` with the *fair* (unbiased)
    estimator, which divides the second term by ``m(m-1)`` rather than ``m^2``. The biased
    version rewards a model simply for drawing fewer paths, which would corrupt any
    comparison between models run at different path counts.

    The double sum is evaluated in ``O(m log m)`` by sorting:
    ``sum_ij |x_i - x_j| = 2 * sum_i (2i - m - 1) * x_(i)`` for 1-based sorted ``x``.

    Parameters
    ----------
    samples
        Draws from the predictive distribution, shape ``(m,)``.
    observed
        The realized outcome, in the same units.

    Returns
    -------
    float
        CRPS in the units of the forecast variable. Lower is better; 0 is a perfect
        point forecast.
    """
    values = np.sort(np.asarray(samples, dtype=float))
    m = values.size
    if m == 0:
        return float("nan")
    if m == 1:
        return float(abs(values[0] - observed))

    absolute_error = float(np.mean(np.abs(values - observed)))
    ranks = np.arange(1, m + 1)
    pairwise = 2.0 * float(np.sum((2 * ranks - m - 1) * values))
    return absolute_error - pairwise / (2.0 * m * (m - 1))


def crps_from_quantiles(quantiles: np.ndarray, levels: np.ndarray, observed: float) -> float:
    """CRPS approximated from a grid of predictive quantiles, for forecasts stored as
    quantiles rather than samples (the forecast journal, ``journal.py``).

    Uses ``CRPS = 2 * integral_0^1 QS_tau(q_tau, y) dtau`` with the pinball loss
    ``QS_tau(q, y) = (1{y < q} - tau) * (q - y)``, the integral replaced by the mean over
    an evenly spaced grid. The journal's 1%..99% grid leaves the outer tails unresolved,
    so this differs from the exact CRPS by a small amount in either direction (about 1%
    for a normal forecast; ``tests/test_journal.py`` checks it against the closed form).
    Every forecast on the same grid carries the same approximation, so comparisons
    between models stay fair, but these numbers are not interchangeable with
    :func:`crps_from_samples` values.

    Parameters
    ----------
    quantiles
        Predictive quantiles at ``levels``, shape ``(k,)``.
    levels
        Evenly spaced probability levels in ``(0, 1)``, shape ``(k,)``.
    observed
        The realized outcome, in the same units as ``quantiles``.
    """
    q = np.asarray(quantiles, dtype=float)
    tau = np.asarray(levels, dtype=float)
    pinball = ((observed < q).astype(float) - tau) * (q - observed)
    return float(2.0 * np.mean(pinball))


def pit_from_quantiles(quantiles: np.ndarray, levels: np.ndarray, observed: float) -> float:
    """Probability integral transform from a quantile grid, by linear interpolation of
    the predictive CDF between stored quantiles. An outcome beyond the outermost
    quantile is placed at the midpoint of that tail's unresolved mass (e.g. 0.005 below
    a 1% quantile) -- the grid cannot say where inside the tail it fell."""
    q = np.maximum.accumulate(np.asarray(quantiles, dtype=float))
    tau = np.asarray(levels, dtype=float)
    if observed < q[0]:
        return float(tau[0] / 2.0)
    if observed > q[-1]:
        return float((1.0 + tau[-1]) / 2.0)
    return float(np.interp(observed, q, tau))


def crps_normal(mu: float, sigma: float, observed: float) -> float:
    """Closed-form CRPS of a normal distribution.

    ``sigma * [ w * (2 * Phi(w) - 1) + 2 * phi(w) - 1 / sqrt(pi) ]`` with
    ``w = (y - mu) / sigma``. Used to test the sample estimator above.
    """
    if sigma <= 0:
        return float(abs(observed - mu))
    w = (observed - mu) / sigma
    return float(
        sigma
        * (w * (2.0 * stats.norm.cdf(w) - 1.0) + 2.0 * stats.norm.pdf(w) - 1.0 / np.sqrt(np.pi))
    )


def pit_value(
    samples: np.ndarray, observed: float, rng: np.random.Generator | None = None
) -> float:
    """Probability Integral Transform: the outcome's rank within the sample, in [0, 1].

    Ties are broken by drawing uniformly between the ranks either side. Without that
    randomization a discrete sample produces a PIT that is uniform only approximately, and
    the histogram picks up spurious structure at the edges.

    Returns
    -------
    float
        The outcome's percentile. Uniform across origins if the forecast is calibrated.
    """
    values = np.asarray(samples, dtype=float)
    m = values.size
    if m == 0:
        return float("nan")
    below = float(np.sum(values < observed))
    equal = float(np.sum(values == observed))
    if equal == 0:
        return below / m
    draw = rng.random() if rng is not None else 0.5
    return (below + draw * equal) / m


def coverage(samples: np.ndarray, observed: float, level: float) -> tuple[bool, float, float]:
    """Whether the outcome fell inside the central interval at ``level``.

    Returns
    -------
    hit, lower, upper
    """
    tail = (1.0 - level) / 2.0
    lower, upper = np.quantile(np.asarray(samples, dtype=float), [tail, 1.0 - tail])
    return bool(lower <= observed <= upper), float(lower), float(upper)


def pit_histogram(pit_values: np.ndarray, bins: int = 10) -> pd.DataFrame:
    """Binned PIT counts with the frequency a calibrated model would produce.

    Also reports a chi-squared test of uniformity. With overlapping forecast windows the
    p-value is optimistic -- the observations are not independent -- so it is a diagnostic,
    not a verdict, and the backtester computes it on non-overlapping origins.
    """
    values = np.asarray(pit_values, dtype=float)
    values = values[np.isfinite(values)]
    edges = np.linspace(0.0, 1.0, bins + 1)
    counts, _ = np.histogram(values, bins=edges)
    expected = len(values) / bins if len(values) else np.nan

    frame = pd.DataFrame(
        {
            "bin_low": edges[:-1],
            "bin_high": edges[1:],
            "count": counts,
            "expected": expected,
            "frequency": counts / len(values) if len(values) else np.nan,
        }
    )
    if len(values) >= bins * 5:
        statistic = float(np.sum((counts - expected) ** 2 / expected))
        frame.attrs["chi2"] = statistic
        frame.attrs["chi2_pvalue"] = float(stats.chi2.sf(statistic, df=bins - 1))
    else:
        frame.attrs["chi2"] = np.nan
        frame.attrs["chi2_pvalue"] = np.nan
    frame.attrs["n"] = int(len(values))
    return frame


def kupiec_pof(n_observations: int, n_exceptions: int, expected_rate: float) -> dict[str, float]:
    """Kupiec proportion-of-failures test.

    Null hypothesis: exceptions occur at exactly the nominal rate. The likelihood ratio

    ``LR = -2 * ln[ (1-p)^(n-x) p^x / ((1-pi)^(n-x) pi^x) ]``,  ``pi = x / n``

    is chi-squared with one degree of freedom. A small p-value means the *number* of VaR
    breaches is wrong -- too many (the model understates risk) or too few (it overstates
    it, which is also a miscalibration, just a conservative one).
    """
    n, x, p = int(n_observations), int(n_exceptions), float(expected_rate)
    out = {
        "n": float(n),
        "exceptions": float(x),
        "observed_rate": float(x / n) if n else np.nan,
        "expected_rate": p,
        "lr_pof": np.nan,
        "p_value": np.nan,
    }
    if n == 0 or not 0 < p < 1:
        return out

    pi_hat = x / n
    if x == 0:
        log_likelihood_alt = 0.0
    elif x == n:
        log_likelihood_alt = n * np.log(pi_hat)
    else:
        log_likelihood_alt = (n - x) * np.log(1 - pi_hat) + x * np.log(pi_hat)
    log_likelihood_null = (n - x) * np.log(1 - p) + x * np.log(p)

    statistic = -2.0 * (log_likelihood_null - log_likelihood_alt)
    out["lr_pof"] = float(statistic)
    out["p_value"] = float(stats.chi2.sf(statistic, df=1))
    return out


def christoffersen_independence(exceptions: np.ndarray) -> dict[str, float]:
    """Christoffersen test that VaR exceptions are not clustered.

    Counts transitions ``n_ij`` from state ``i`` to state ``j`` in the 0/1 exception
    sequence and tests ``pi_01 = pi_11`` -- that a breach today says nothing about a breach
    tomorrow. Clustered breaches are the signature of a model that ignores volatility
    clustering, which is exactly what GBM does.

    Parameters
    ----------
    exceptions
        Sequence of 0/1 flags **in chronological order**, one per non-overlapping origin.
    """
    series = np.asarray(exceptions, dtype=int).ravel()
    out = {"lr_ind": np.nan, "p_value": np.nan, "n00": 0.0, "n01": 0.0, "n10": 0.0, "n11": 0.0}
    if series.size < 2:
        return out

    previous, current = series[:-1], series[1:]
    n00 = float(np.sum((previous == 0) & (current == 0)))
    n01 = float(np.sum((previous == 0) & (current == 1)))
    n10 = float(np.sum((previous == 1) & (current == 0)))
    n11 = float(np.sum((previous == 1) & (current == 1)))
    out.update({"n00": n00, "n01": n01, "n10": n10, "n11": n11})

    total = n00 + n01 + n10 + n11
    if total == 0 or (n01 + n11) == 0:
        return out

    pi = (n01 + n11) / total
    pi0 = n01 / (n00 + n01) if (n00 + n01) > 0 else 0.0
    pi1 = n11 / (n10 + n11) if (n10 + n11) > 0 else 0.0

    def _log(value: float) -> float:
        return np.log(value) if value > 0 else 0.0

    log_null = (n00 + n10) * _log(1 - pi) + (n01 + n11) * _log(pi)
    log_alt = n00 * _log(1 - pi0) + n01 * _log(pi0) + n10 * _log(1 - pi1) + n11 * _log(pi1)
    statistic = -2.0 * (log_null - log_alt)
    out["lr_ind"] = float(statistic)
    out["p_value"] = float(stats.chi2.sf(statistic, df=1))
    return out


def christoffersen_conditional_coverage(
    exceptions: np.ndarray, expected_rate: float
) -> dict[str, float]:
    """Joint test of the exception rate and independence (``LR_cc = LR_pof + LR_ind``).

    Distributed chi-squared with two degrees of freedom under the null that the model is
    correctly calibrated *and* its breaches are independent.
    """
    series = np.asarray(exceptions, dtype=int).ravel()
    pof = kupiec_pof(series.size, int(series.sum()), expected_rate)
    ind = christoffersen_independence(series)
    statistic = pof["lr_pof"] + ind["lr_ind"]
    if not np.isfinite(statistic):
        return {"lr_cc": np.nan, "p_value": np.nan}
    return {"lr_cc": float(statistic), "p_value": float(stats.chi2.sf(statistic, df=2))}


def crps_skill_score(model_crps: float, baseline_crps: float) -> float:
    """``1 - CRPS_model / CRPS_baseline``.

    Positive means the model beats the baseline; 0 means it ties; negative means it is
    worse. Reported rather than raw CRPS because CRPS scales with volatility, so raw
    values are incomparable across tickers and horizons.
    """
    if not np.isfinite(baseline_crps) or baseline_crps <= 0:
        return float("nan")
    return float(1.0 - model_crps / baseline_crps)


def diebold_mariano(
    losses_model: np.ndarray,
    losses_baseline: np.ndarray,
) -> dict[str, float]:
    """Diebold-Mariano test of equal predictive accuracy on paired losses.

    Applied to per-origin CRPS differences. Uses a plain standard error, so it is only
    valid on **non-overlapping** origins -- overlapping windows make the differences
    autocorrelated and the test badly oversized. The backtester enforces that.
    """
    model = np.asarray(losses_model, dtype=float)
    baseline = np.asarray(losses_baseline, dtype=float)
    mask = np.isfinite(model) & np.isfinite(baseline)
    difference = model[mask] - baseline[mask]
    n = difference.size
    if n < 8:
        return {"dm_statistic": np.nan, "p_value": np.nan, "n": float(n)}

    mean = float(difference.mean())
    standard_error = float(difference.std(ddof=1) / np.sqrt(n))
    if standard_error == 0:
        return {"dm_statistic": np.nan, "p_value": np.nan, "n": float(n)}
    statistic = mean / standard_error
    return {
        "dm_statistic": float(statistic),
        "p_value": float(2.0 * stats.t.sf(abs(statistic), df=n - 1)),
        "n": float(n),
    }


def block_bootstrap_origins(
    rows: pd.DataFrame,
    statistic_fn: Callable[[pd.DataFrame], float],
    n_bootstrap: int,
    rng: np.random.Generator,
    origin_column: str = "origin",
) -> np.ndarray:
    """Resample whole origins with replacement, keeping every row that shares an origin
    together in each replicate.

    This is the fix the project's own caveats name but do not apply: every significance
    test above treats each ``(ticker, origin)`` row as an independent observation, but
    semiconductor tickers co-move (pairwise return correlations roughly 0.5-0.7), so
    resampling individual rows would treat that co-movement as independent evidence.
    Resampling *origins* instead means a sector-wide move that hit every ticker on one
    date stays intact -- appearing together, or not at all -- in each replicate, so the
    spread of ``statistic_fn`` across replicates reflects the real degree of
    cross-sectional dependence rather than assuming there is none.
    """
    # Row *positions* per origin, not sub-frames: concatenating position arrays (cheap
    # numpy) and taking one `.iloc` slice per replicate is far faster than concatenating
    # `n_origins` small DataFrames on every one of `n_bootstrap` replicates, which matters
    # at the 2,000-replicate default over a real multi-ticker backtest.
    positions = pd.Series(np.arange(len(rows)), index=rows.index)
    groups = {
        origin: group.to_numpy()
        for origin, group in positions.groupby(rows[origin_column].to_numpy())
    }
    origins = np.array(list(groups.keys()), dtype=object)
    n = len(origins)
    if n == 0:
        return np.array([])
    replicates = np.empty(int(n_bootstrap), dtype=float)
    for b in range(int(n_bootstrap)):
        draw = rng.choice(origins, size=n, replace=True)
        row_positions = np.concatenate([groups[o] for o in draw])
        resampled = rows.iloc[row_positions]
        replicates[b] = statistic_fn(resampled)
    return replicates


def diebold_mariano_bootstrap(
    paired: pd.DataFrame,
    model_column: str,
    baseline_column: str,
    n_bootstrap: int,
    rng: np.random.Generator,
    origin_column: str = "origin",
    min_origins: int = 8,
) -> dict[str, float]:
    """Block-bootstrap-over-origins version of :func:`diebold_mariano`.

    The plain test's standard error assumes every ``(ticker, origin)`` row is an
    independent draw; pooled semiconductor tickers co-move, so that standard error is too
    small and the plain p-value too optimistic -- disclosed as a caveat throughout this
    project's written summaries rather than corrected, until this function. The point
    estimate (mean CRPS difference) is unchanged; what changes is the standard error,
    estimated by resampling whole origins via :func:`block_bootstrap_origins` instead of
    assuming row-level independence.

    Requires at least ``min_origins`` distinct origins -- the resampling unit here is the
    origin, not the row, so origin count (not row count) is what determines whether the
    bootstrap standard error means anything.
    """
    mask = np.isfinite(paired[model_column]) & np.isfinite(paired[baseline_column])
    usable = paired.loc[mask]
    n_origins = int(usable[origin_column].nunique()) if not usable.empty else 0
    out = {
        "dm_statistic": float("nan"),
        "p_value": float("nan"),
        "mean_difference": float("nan"),
        "bootstrap_se": float("nan"),
        "n": int(len(usable)),
        "n_origins": n_origins,
    }
    if usable.empty or n_origins < min_origins:
        return out

    def _mean_difference(frame: pd.DataFrame) -> float:
        return float((frame[model_column] - frame[baseline_column]).mean())

    observed = _mean_difference(usable)
    replicates = block_bootstrap_origins(usable, _mean_difference, n_bootstrap, rng, origin_column)
    bootstrap_se = float(np.std(replicates, ddof=1))
    out["mean_difference"] = observed
    out["bootstrap_se"] = bootstrap_se
    if bootstrap_se == 0 or not np.isfinite(bootstrap_se):
        return out
    statistic = observed / bootstrap_se
    out["dm_statistic"] = float(statistic)
    out["p_value"] = float(2.0 * stats.norm.sf(abs(statistic)))
    return out


def pit_bias_bootstrap_test(
    rows: pd.DataFrame,
    n_bootstrap: int,
    rng: np.random.Generator,
    bins: int = 10,
    origin_column: str = "origin",
    pit_column: str = "pit",
    min_origins: int = 8,
) -> dict[str, float]:
    """Block-bootstrap-over-origins significance test for PIT miscalibration.

    The classic multi-bin chi-squared goodness-of-fit test (:func:`pit_histogram`) has the
    same row-independence problem as the plain Diebold-Mariano test. Correcting the
    chi-squared statistic itself for clustering (a Rao-Scott-style correction) needs the
    full multinomial covariance structure, which is easy to get subtly wrong with only a
    few dozen origins to estimate it from -- a worse failure than the disclosed limitation
    it would replace. Instead this tests the two summary statistics the written report
    already interprets (:func:`diagnose_pit`): mean PIT (centre bias, null 0.5) and tail
    mass (width, null ``2/bins``), each against a block-bootstrap standard error. That is a
    more modest and more robust question than "is the full shape uniform": not whether the
    histogram is exactly uniform, but whether the specific bias or width already being
    described in prose is distinguishable from noise once co-moving tickers stop being
    counted as independent evidence.
    """
    usable = rows.loc[np.isfinite(rows[pit_column].to_numpy(dtype=float))]
    n_origins = int(usable[origin_column].nunique()) if not usable.empty else 0
    edges = np.linspace(0.0, 1.0, bins + 1)
    uniform_tail = 2.0 / bins

    out = {
        "mean_pit": float("nan"),
        "mean_pit_bootstrap_se": float("nan"),
        "mean_pit_z": float("nan"),
        "mean_pit_p_value": float("nan"),
        "tail_mass": float("nan"),
        "tail_mass_bootstrap_se": float("nan"),
        "tail_mass_z": float("nan"),
        "tail_mass_p_value": float("nan"),
        "n": int(usable.shape[0]),
        "n_origins": n_origins,
    }
    if usable.empty or n_origins < min_origins:
        return out

    def _mean_pit(frame: pd.DataFrame) -> float:
        return float(frame[pit_column].mean())

    def _tail_mass(frame: pd.DataFrame) -> float:
        counts, _ = np.histogram(frame[pit_column].to_numpy(dtype=float), bins=edges)
        total = int(counts.sum())
        return float((counts[0] + counts[-1]) / total) if total else float("nan")

    observed_mean = _mean_pit(usable)
    observed_tail = _tail_mass(usable)
    out["mean_pit"] = observed_mean
    out["tail_mass"] = observed_tail

    mean_replicates = block_bootstrap_origins(usable, _mean_pit, n_bootstrap, rng, origin_column)
    mean_se = float(np.std(mean_replicates, ddof=1))
    if mean_se > 0 and np.isfinite(mean_se):
        z = (observed_mean - 0.5) / mean_se
        out["mean_pit_bootstrap_se"] = mean_se
        out["mean_pit_z"] = float(z)
        out["mean_pit_p_value"] = float(2.0 * stats.norm.sf(abs(z)))

    tail_replicates = block_bootstrap_origins(usable, _tail_mass, n_bootstrap, rng, origin_column)
    tail_se = float(np.std(tail_replicates, ddof=1))
    if tail_se > 0 and np.isfinite(tail_se):
        z = (observed_tail - uniform_tail) / tail_se
        out["tail_mass_bootstrap_se"] = tail_se
        out["tail_mass_z"] = float(z)
        out["tail_mass_p_value"] = float(2.0 * stats.norm.sf(abs(z)))
    return out

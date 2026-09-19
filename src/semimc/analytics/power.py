"""How much more data a signal-search result would need, and how much a correlated
cross-section is actually worth.

Every Phase 4B result this project has produced so far is statistically weak by
construction: 14 co-moving tickers and a handful of monthly origins give little power to
tell a real, small effect apart from noise. These two functions turn "how much more" into
a number instead of a feeling, so a `signals` run can report it automatically rather than
leaving the reader to guess whether waiting, resampling faster, or growing the universe is
worth trying.
"""

from __future__ import annotations


def required_sample_size(observed_t: float, observed_n: int, target_t: float) -> float:
    """Extrapolated total ``n`` needed to reach ``target_t``, from one already-observed
    ``(observed_t, observed_n)`` pair.

    Assumes ``t`` scales with ``sqrt(n)`` at a fixed effect size -- the asymptotic scaling
    a Newey-West/HAC-corrected mean statistic follows. This is a planning heuristic, not a
    guarantee: it assumes the autocorrelation and cross-sectional-correlation structure
    behind the observed standard error stays constant as ``n`` grows. A shorter-horizon or
    higher-frequency redesign changes that structure on purpose -- this number says how
    much data *would* be needed if the effect size carries over unchanged, not that it
    will. Returns ``inf`` when ``observed_t``/``observed_n`` give no basis to extrapolate
    from (a non-positive observed effect implies no amount of additional data helps).
    """
    if observed_t <= 0 or observed_n <= 0:
        return float("inf")
    return observed_n * (target_t / observed_t) ** 2


def effective_independent_units(n: float, avg_pairwise_correlation: float) -> float:
    """Design-effect formula: ``N / (1 + (N-1)*rho)``.

    ``rho=0`` -> ``N`` fully independent units. ``rho=1`` -> 1 (every unit is the same
    information, however many there are). Used here to show why growing a correlated
    universe (e.g. adding more semiconductor tickers, which move together) buys far less
    statistical power than the raw ticker count suggests -- the number that matters is how
    many *effectively independent* return series exist, not how many tickers.
    """
    if not (-1.0 <= avg_pairwise_correlation <= 1.0):
        raise ValueError("avg_pairwise_correlation must be in [-1, 1]")
    denominator = 1 + (n - 1) * avg_pairwise_correlation
    if denominator <= 0:
        return float(n)
    return n / denominator

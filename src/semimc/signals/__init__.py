"""Addendum Phase 4B: point-in-time signals for the regression-based drift tilt.

``build.py`` computes the five candidate signals from data the rest of the package already
loads point-in-time correctly (returns, benchmarks, the earnings calendar) plus one series
that genuinely needs the release-date machinery in ``external.py`` (the industry cycle).
Every builder here takes an explicit ``as_of`` and is a pure function of data at or before
it -- there is no separate assertion step because, unlike model fitting, nothing here reads
from a mutable cache that could accidentally include a later date.
"""

from __future__ import annotations

from .build import (
    earnings_drift_signal,
    momentum_signal,
    relative_strength_signal,
    reversal_signal,
    signal_panel,
)
from .external import ExternalSeriesError, as_of, load_external_series
from .supply_chain import customer_lead_lag_signal, load_supply_chain_links

__all__ = [
    "ExternalSeriesError",
    "as_of",
    "customer_lead_lag_signal",
    "earnings_drift_signal",
    "load_external_series",
    "load_supply_chain_links",
    "momentum_signal",
    "relative_strength_signal",
    "reversal_signal",
    "signal_panel",
]

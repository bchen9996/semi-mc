"""The ``decide`` job: freeze one sleeve's daily :class:`~semimc.strategy.decide.Decision`
into the ledger, with the data-vintage/git-commit bookkeeping the ledger's schema has
always had a place for.

Two things live here that ``strategy/decide.py`` deliberately does not know about, because
they are about *when a live job runs*, not about *what a decision is*: a per-sleeve
rebalance cadence (so a schedule can invoke this daily without over-trading a
monthly-rebalance sleeve), and the refusal to silently overwrite a different decision
already frozen for the same day.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..data import MarketData
from ..strategy.decide import Decision, decide
from .ledger import Ledger


def git_commit() -> str | None:
    """The current commit hash, or ``None`` if this isn't a git checkout (or git isn't
    installed) -- bookkeeping a decide run must never fail over."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


# pandas 2.2 renamed the Timestamp/Grouper month/quarter/year-end offset aliases this
# project's origin_frequency convention otherwise uses everywhere ("ME"/"QE"/"YE") --
# pd.Period still wants the pre-rename aliases ("M"/"Q"/"Y") and raises ValueError on the
# new ones. Translate here rather than picking one alias family project-wide: every other
# use of these strings (pd.date_range, pd.Grouper) already wants the "ME"-style alias.
_PERIOD_FREQ_ALIASES = {"ME": "M", "QE": "Q", "YE": "Y", "BQE": "Q", "BYE": "Y"}


def _period_freq(freq: str) -> str:
    return _PERIOD_FREQ_ALIASES.get(freq, freq)


def seed_for(base_seed: int, as_of: pd.Timestamp) -> np.random.Generator:
    """Deterministic per day: the same ``as_of`` always reproduces the same decision (this
    project's own "reproducible from the seed" rule), but different days get different
    Monte-Carlo draws rather than repeating the exact same simulated paths every single
    rebalance -- freezing one draw forever would bias every S3 vol forecast identically."""
    return np.random.default_rng([base_seed, int(pd.Timestamp(as_of).value)])


@dataclass(frozen=True)
class DecideResult:
    """``decision`` is ``None`` exactly when ``skipped_reason`` explains why -- a routine
    no-op (already decided this rebalance period), never an error."""

    decision: Decision | None
    skipped_reason: str | None


def run_decide(
    sleeve: str,
    market: MarketData,
    config,  # noqa: ANN001
    tickers: list[str],
    as_of: pd.Timestamp,
    ledger: Ledger,
    *,
    force: bool = False,
) -> DecideResult:
    """Compute and freeze one sleeve's decision for ``as_of``, unless it's already been
    decided this rebalance period (skip) or a *different* decision already exists for this
    exact day (refuse, unless ``force``)."""
    as_of = pd.Timestamp(as_of)
    sleeve_config = config.paper.sleeves.get(sleeve)
    rebalance_frequency = sleeve_config.rebalance_frequency if sleeve_config is not None else "ME"

    if not force:
        last = ledger.last_decision(sleeve)
        # Only a *different* prior day can trigger the cadence skip -- re-running for the
        # exact same as_of must fall through to the differing-decision check below (an
        # identical re-decide is a harmless idempotent no-op there; a *different* one for
        # the same day is exactly what that check exists to catch, and skipping here first
        # would make it unreachable).
        if last is not None and pd.Timestamp(last["as_of"]) != as_of:
            period_freq = _period_freq(rebalance_frequency)
            last_period = pd.Period(pd.Timestamp(last["as_of"]), freq=period_freq)
            this_period = pd.Period(as_of, freq=period_freq)
            if last_period == this_period:
                return DecideResult(
                    None,
                    f"sleeve {sleeve!r} already decided this {rebalance_frequency} period "
                    f"(last decision {last['as_of']})",
                )

    decision = decide(
        sleeve, market, config, tickers, as_of, seed_for(config.simulation.seed, as_of)
    )

    existing = ledger.get_decision(sleeve, as_of)
    if existing is not None and existing["snapshot_hash"] != decision.snapshot_hash and not force:
        raise ValueError(
            f"a different decision already exists for {sleeve!r} on {as_of.date()} "
            f"(existing snapshot_hash={existing['snapshot_hash'][:16]}, new="
            f"{decision.snapshot_hash[:16]}) -- pass force=True to record it anyway"
        )

    ledger.record_decision(
        decision,
        data_vintage=str(market.last_date.date()),
        git_commit=git_commit(),
    )
    return DecideResult(decision, None)

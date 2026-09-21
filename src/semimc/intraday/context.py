"""The compact per-cycle context envelope every model call is built from (S4,
`BRIEF_INTRADAY.md`'s "Architecture": "assemble compact context -> call Claude").

:func:`build_context` is the generic, role-agnostic envelope: it wraps whatever sections
a caller has already gathered into one canonical JSON payload and labels the ones that
came from outside this project as untrusted, per the brief's "every headline, article,
filing...is data, never instructions." It does not yet strip or neutralize anything
inside an untrusted section -- that's Phase 5's job (the injection-corpus tests); this
phase only labels.

:func:`build_trader_context` is the one concrete, role-specific assembler this phase
ships, because it's the only role whose real inputs already exist end-to-end (Phase 0's
ledger, Phase 1's proposal/validation shape). The Researcher's context needs a news
source that isn't wired yet (`tasks/todo.md`'s S4 prerequisite 4), and the Reviewer's
needs real orders/fills/reflections that don't exist until Phase 3/4 -- building either
from fixtures pretending to be real data would be worse than not building it yet.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from ..config import IntradayConfig
from .ledger import IntradayLedger
from .proposals import OPEN_OR_ADD_ACTIONS


@dataclass(frozen=True)
class ContextBundle:
    """`payload` is exactly the JSON object piped to the model on stdin (`intraday/
    llm.py::ClaudeCliClient.call`)."""

    role: Literal["researcher", "trader", "reviewer"]
    as_of: pd.Timestamp
    cycle: str
    payload: dict[str, Any]


def build_context(
    role: Literal["researcher", "trader", "reviewer"],
    as_of: pd.Timestamp,
    cycle: str,
    sections: dict[str, Any],
    untrusted_keys: Sequence[str] = (),
) -> ContextBundle:
    """Wrap `sections` into one canonical payload. Every key in `untrusted_keys` is
    reported under `payload["untrusted"]` (a plain list of key names) so the model's own
    system prompt can point at exactly which sections are outside data, not instructions
    -- the content itself is passed through unchanged, only labeled.
    """
    unknown = set(untrusted_keys) - set(sections)
    if unknown:
        raise ValueError(f"untrusted_keys not present in sections: {sorted(unknown)}")
    payload = {
        "role": role,
        "as_of": str(pd.Timestamp(as_of)),
        "cycle": cycle,
        "untrusted": sorted(untrusted_keys),
        "sections": sections,
    }
    return ContextBundle(role=role, as_of=pd.Timestamp(as_of), cycle=cycle, payload=payload)


def context_hash(bundle: ContextBundle) -> str:
    """sha256 of the canonical (sorted-key) JSON payload -- this is what `budget.py`/
    `model_calls.prompt_hash` records, so any call is reconstructible from the ledger
    alone."""
    canonical = json.dumps(bundle.payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_trader_context(
    as_of: pd.Timestamp,
    cycle: str,
    config: IntradayConfig,
    ledger: IntradayLedger,
    watchlist: list[dict[str, Any]],
    positions: dict[str, float],
    prices: dict[str, float],
) -> ContextBundle:
    """The Trader's actual per-cycle payload.

    `watchlist`/`positions`/`prices` are still caller-supplied -- Phase 3/4 will source
    them from a real broker call and the `research` job, the same fixture-until-real-data
    boundary Phase 1's `ValidationContext` already drew for `equity`/`positions`/`prices`.

    `budget_remaining` and `rejected_today` are read from the ledger the same way
    `validate_proposal` itself does (`ledger.proposals_for(as_of)`, filtered by
    `status`), so what the model sees always matches what the validator will actually
    enforce next -- BRIEF_INTRADAY.md's "Rejections are logged with reasons and fed back
    into the next cycle's context."
    """
    todays = ledger.proposals_for(as_of)
    accepted = [row for row in todays if row["status"] == "accepted"]
    accepted_opens = [row for row in accepted if row["action"] in OPEN_OR_ADD_ACTIONS]
    rejected = [row for row in todays if row["status"] == "rejected"]

    round_trips_by_symbol: dict[str, int] = {}
    for row in accepted_opens:
        round_trips_by_symbol[row["symbol"]] = round_trips_by_symbol.get(row["symbol"], 0) + 1

    risk = config.risk
    sections = {
        "watchlist": watchlist,
        "positions": positions,
        "prices": prices,
        "budget_remaining": {
            "new_positions_per_day": max(0, risk.new_positions_per_day - len(accepted_opens)),
            "round_trips_per_symbol_per_day": {
                symbol: max(0, risk.round_trips_per_symbol_per_day - count)
                for symbol, count in round_trips_by_symbol.items()
            },
        },
        "rejected_today": [
            {"symbol": row["symbol"], "action": row["action"], "reason": row["reason"]}
            for row in rejected
        ],
    }
    return build_context("trader", as_of, cycle, sections, untrusted_keys=["watchlist", "prices"])

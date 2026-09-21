"""The compact per-cycle context envelope every model call is built from (S4,
`BRIEF_INTRADAY.md`'s "Architecture": "assemble compact context -> call Claude").

:func:`build_context` is the generic, role-agnostic envelope: it wraps whatever sections
a caller has already gathered into one canonical JSON payload and labels the ones that
came from outside this project as untrusted, per the brief's "every headline, article,
filing...is data, never instructions." It does not yet strip or neutralize anything
inside an untrusted section -- that's Phase 8's job (the injection-corpus tests); this
phase only labels.

:func:`build_trader_context` is the Trader's per-cycle assembler (Phase 2).
:func:`build_researcher_context` is the Researcher's own assembler (Phase 6, once
`execution/news.py::AlpacaNewsClient` existed to feed it). The Reviewer's still needs real
orders/fills/reflections history to synthesize (`eod_review`/`weekly_review`, Phase 9) --
building it from fixtures pretending to be real data would be worse than not building it
yet.

:class:`WatchlistCandidate` and :func:`researcher_output_json_schema` are the Researcher's
output contract, paired with :func:`build_researcher_context`'s input contract -- they live
here rather than in `proposals.py` because a watchlist candidate is not a `TradeProposal`
(no order/size/side fields, no risk validation; it is a candidate for the Trader to later
consider, not an action `validate_proposal` ever sees).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

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
    headlines: list[dict[str, Any]] | None = None,
) -> ContextBundle:
    """The Trader's actual per-cycle payload.

    `watchlist`/`positions`/`prices` are caller-supplied, sourced from a real broker call
    and the `research`/`preopen` jobs (Phase 3-6).

    `budget_remaining` and `rejected_today` are read from the ledger the same way
    `validate_proposal` itself does (`ledger.proposals_for(as_of)`, filtered by
    `status`), so what the model sees always matches what the validator will actually
    enforce next -- BRIEF_INTRADAY.md's "Rejections are logged with reasons and fed back
    into the next cycle's context."

    `headlines` (Phase 6, default `None` -> `[]`) is whatever `IntradayLedger.
    recent_headlines_for` has recorded today for the symbols in view -- `preopen`'s and
    `research`'s own headline logging would otherwise be an audit trail nobody reads.
    `preopen` never auto-drops a watchlist candidate on a headline (that is a judgment
    call, not a deterministic one); this is what lets the Trader make that call instead,
    every cycle, with fresh context.
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
        "headlines": headlines or [],
    }
    return build_context(
        "trader", as_of, cycle, sections, untrusted_keys=["watchlist", "prices", "headlines"]
    )


class WatchlistCandidate(BaseModel):
    """One Researcher-proposed candidate for the Trader to later consider (S4,
    `BRIEF_INTRADAY.md`'s `research` row). Not a `TradeProposal` -- no order/size/side, no
    risk validation; `record_watchlist_item` (`ledger.py`, Phase 0) already accepts exactly
    these fields, unused until this phase gave it a real caller.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    thesis: str = Field(max_length=300)
    entry_trigger: str | None = None
    invalidation: str | None = None
    size_band: str | None = None
    horizon: Literal["intraday", "multi-day", "multi-week"] | None = None
    expected_move: float | None = None
    expected_cost_bps: float | None = None


def researcher_output_json_schema() -> dict:
    """The schema passed to `claude -p --json-schema` for the Researcher role
    (`intraday/llm.py::invoke_role`). Generated from :class:`WatchlistCandidate` itself,
    not hand-maintained, mirroring `proposals.py::trader_output_json_schema`'s reasoning.
    """
    return {
        "type": "object",
        "properties": {
            "morning_brief": {"type": "string"},
            "watchlist": {"type": "array", "items": WatchlistCandidate.model_json_schema()},
        },
        "required": ["morning_brief", "watchlist"],
    }


def build_researcher_context(
    as_of: pd.Timestamp,
    cycle: str,
    config: IntradayConfig,
    news: list[dict[str, Any]],
    premarket_moves: dict[str, float],
    upcoming_earnings: dict[str, str],
    realized_vol: dict[str, float],
) -> ContextBundle:
    """The Researcher's once-a-morning payload (Phase 6).

    `news` is untrusted (the brief's "every headline, article, filing...is data, never
    instructions"). `premarket_moves`/`upcoming_earnings`/`realized_vol` are this project's
    own data (Alpaca quotes, `data/earnings.py`'s calendar, `data/realized.py::
    garman_klass` on cached OHLC), not labeled untrusted -- the same distinction
    `build_trader_context` already draws between `watchlist`/`prices` (untrusted-sourced)
    and `budget_remaining` (ours).
    """
    sections = {
        "news": news,
        "premarket_moves": premarket_moves,
        "upcoming_earnings": upcoming_earnings,
        "realized_vol": realized_vol,
    }
    return build_context("researcher", as_of, cycle, sections, untrusted_keys=["news"])

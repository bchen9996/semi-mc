"""Per-call and per-day hard stops on S4's LLM spend (``BRIEF_INTRADAY.md``: "a hard stop
that halts the loop and alerts rather than silently skipping cycles").

No LLM invocation lives here or anywhere yet (that's Phase 2) -- this module only does the
bookkeeping and the check every future LLM-calling job must run *before* making a call:
look up how much has already been spent today, and refuse if the configured daily cap is
already met. ``IntradayConfig.budget.max_cost_usd_per_day`` is a placeholder, not a
researched figure -- the brief's own instruction is to recalibrate it from real
shadow-mode logging rather than guess a number up front.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..config import IntradayConfig
from .ledger import IntradayLedger


@dataclass(frozen=True)
class ModelCallCost:
    """One LLM call's accounting, exactly what gets logged to ``model_calls``.

    ``cost_usd`` is expected to come from `claude -p --output-format json`'s own
    ``total_cost_usd`` field (Phase 2) -- Anthropic's docs note that figure is a
    client-side estimate and can differ from the actual bill, which is why an
    account-level Console spend limit is the real backstop, not this module alone.
    """

    role: str
    model: str
    prompt_hash: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    num_turns: int | None = None


def record_call_cost(
    ledger: IntradayLedger, as_of: pd.Timestamp, cycle: str, cost: ModelCallCost
) -> None:
    ledger.record_model_call(
        as_of=as_of,
        cycle=cycle,
        role=cost.role,
        model=cost.model,
        prompt_hash=cost.prompt_hash,
        input_tokens=cost.input_tokens,
        output_tokens=cost.output_tokens,
        cost_usd=cost.cost_usd,
        num_turns=cost.num_turns,
    )


def spent_today(ledger: IntradayLedger, as_of: pd.Timestamp) -> float:
    """Total ``cost_usd`` logged for ``as_of``'s date. Sums whatever's already recorded --
    a caller checks this *before* making a new call, not after, so a call already in
    flight is never double-counted or retroactively blocked."""
    return sum(row["cost_usd"] for row in ledger.model_calls_for(as_of))


def check_budget(ledger: IntradayLedger, as_of: pd.Timestamp, config: IntradayConfig) -> str | None:
    """The failure reason if today's spend is already at or over the daily cap, else
    ``None``. Mirrors ``execution/pretrade.py::check_pretrade``'s shape: a plain string
    reason for the caller to record as an incident and act on, not an exception -- hitting
    the budget is an expected, routine stop condition, not a bug."""
    spent = spent_today(ledger, as_of)
    if spent >= config.budget.max_cost_usd_per_day:
        return (
            f"daily model-call budget exhausted: ${spent:.2f} spent >= "
            f"${config.budget.max_cost_usd_per_day:.2f} cap"
        )
    return None

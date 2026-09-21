"""S4: the LLM-driven intraday agent sleeve (``BRIEF_INTRADAY.md``).

Phase 0-2 -- ``ledger.py`` holds S4's own SQLite ledger (deliberately separate from
``live/ledger.py``'s file: see its module docstring for why), ``budget.py`` enforces the
per-call/per-day spend caps every LLM-calling call goes through, ``proposals.py`` defines
the trade proposal schema and its risk/budget validator, ``context.py`` assembles the
compact per-cycle context envelope, and ``llm.py`` is the headless ``claude -p --bare``
invocation wrapper. No order and no job exists yet -- see ``BRIEF_INTRADAY.md``'s roadmap.
"""

from __future__ import annotations

from .budget import ModelCallCost, check_budget, record_call_cost, spent_today
from .context import ContextBundle, build_context, build_trader_context, context_hash
from .ledger import IntradayLedger
from .llm import (
    ClaudeCliClient,
    ClaudeCliError,
    DailyBudgetExhausted,
    ModelResponse,
    invoke_role,
)
from .proposals import (
    TradeProposal,
    ValidationContext,
    trader_output_json_schema,
    validate_proposal,
)

__all__ = [
    "ClaudeCliClient",
    "ClaudeCliError",
    "ContextBundle",
    "DailyBudgetExhausted",
    "IntradayLedger",
    "ModelCallCost",
    "ModelResponse",
    "TradeProposal",
    "ValidationContext",
    "build_context",
    "build_trader_context",
    "check_budget",
    "context_hash",
    "invoke_role",
    "record_call_cost",
    "spent_today",
    "trader_output_json_schema",
    "validate_proposal",
]

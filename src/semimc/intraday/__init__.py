"""S4: the LLM-driven intraday agent sleeve (``BRIEF_INTRADAY.md``).

Phase 0-4 -- ``ledger.py`` holds S4's own SQLite ledger (deliberately separate from
``live/ledger.py``'s file: see its module docstring for why), ``budget.py`` enforces the
per-call/per-day spend caps every LLM-calling call goes through, ``proposals.py`` defines
the trade proposal schema and its risk/budget validator, ``context.py`` assembles the
compact per-cycle context envelope, ``llm.py`` is the headless ``claude -p --bare``
invocation wrapper, ``pretrade.py`` is S4's own sleeve-level gate, and ``orders.py`` turns
an accepted proposal into a real order -- opening/adding with a real stop resting at the
broker, or closing/trimming while keeping that protection intact. No job exists yet --
see ``BRIEF_INTRADAY.md``'s roadmap.
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
from .orders import (
    build_close_order,
    build_entry_order,
    build_replacement_stop_order,
    build_trim_order,
    find_and_cancel_resting_stop,
    intraday_client_order_id,
    submit_order_with_fault_isolation,
)
from .pretrade import IntradayPretradeContext, check_intraday_pretrade
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
    "IntradayPretradeContext",
    "ModelCallCost",
    "ModelResponse",
    "TradeProposal",
    "ValidationContext",
    "build_close_order",
    "build_context",
    "build_entry_order",
    "build_replacement_stop_order",
    "build_trader_context",
    "build_trim_order",
    "check_budget",
    "check_intraday_pretrade",
    "context_hash",
    "find_and_cancel_resting_stop",
    "intraday_client_order_id",
    "invoke_role",
    "record_call_cost",
    "spent_today",
    "submit_order_with_fault_isolation",
    "trader_output_json_schema",
    "validate_proposal",
]

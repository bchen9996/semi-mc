"""S4: the LLM-driven intraday agent sleeve (``BRIEF_INTRADAY.md``).

Phase 0-6 -- ``ledger.py`` holds S4's own SQLite ledger (deliberately separate from
``live/ledger.py``'s file: see its module docstring for why), ``budget.py`` enforces the
per-call/per-day spend caps every LLM-calling call goes through, ``proposals.py`` defines
the trade proposal schema and its risk/budget validator, ``context.py`` assembles the
compact per-cycle context envelope (both the Trader's and, since Phase 6, the
Researcher's), ``llm.py`` is the headless ``claude -p --bare`` invocation wrapper,
``pretrade.py`` is S4's own sleeve-level gate plus the per-candidate spread/staleness
gate, ``orders.py`` turns an accepted proposal into a real order, ``monitor.py`` enforces
invalidation/time-stops deterministically, and ``jobs.py`` is the scheduled loop
(``preflight``/``research``/``preopen``/``open_batch``/``cycle``/``carry``) that sequences
all of it. ``eod_review``/``weekly_review`` are Phase 9's jobs -- see ``tasks/todo.md``'s
roadmap.
"""

from __future__ import annotations

from .budget import ModelCallCost, check_budget, record_call_cost, spent_today
from .context import (
    ContextBundle,
    WatchlistCandidate,
    build_context,
    build_researcher_context,
    build_trader_context,
    context_hash,
    researcher_output_json_schema,
)
from .jobs import (
    CarryResult,
    CycleResult,
    PreflightResult,
    PreopenResult,
    ResearchResult,
    midpoint_price,
    quote_map,
    run_carry,
    run_cycle,
    run_preflight,
    run_preopen,
    run_research,
)
from .ledger import IntradayLedger
from .llm import (
    ClaudeCliClient,
    ClaudeCliError,
    DailyBudgetExhausted,
    ModelResponse,
    invoke_role,
)
from .monitor import MonitorResult, check_invalidation_and_time_stops
from .orders import (
    build_close_order,
    build_entry_order,
    build_replacement_stop_order,
    build_trim_order,
    find_and_cancel_resting_stop,
    intraday_client_order_id,
    submit_order_with_fault_isolation,
)
from .pretrade import IntradayPretradeContext, check_intraday_pretrade, check_spread_gate
from .proposals import (
    TradeProposal,
    ValidationContext,
    trader_output_json_schema,
    validate_proposal,
)

__all__ = [
    "CarryResult",
    "ClaudeCliClient",
    "ClaudeCliError",
    "ContextBundle",
    "CycleResult",
    "DailyBudgetExhausted",
    "IntradayLedger",
    "IntradayPretradeContext",
    "ModelCallCost",
    "ModelResponse",
    "MonitorResult",
    "PreflightResult",
    "PreopenResult",
    "ResearchResult",
    "TradeProposal",
    "ValidationContext",
    "WatchlistCandidate",
    "build_close_order",
    "build_context",
    "build_entry_order",
    "build_replacement_stop_order",
    "build_researcher_context",
    "build_trader_context",
    "build_trim_order",
    "check_budget",
    "check_intraday_pretrade",
    "check_invalidation_and_time_stops",
    "check_spread_gate",
    "context_hash",
    "find_and_cancel_resting_stop",
    "intraday_client_order_id",
    "invoke_role",
    "midpoint_price",
    "quote_map",
    "record_call_cost",
    "researcher_output_json_schema",
    "run_carry",
    "run_cycle",
    "run_preflight",
    "run_preopen",
    "run_research",
    "spent_today",
    "submit_order_with_fault_isolation",
    "trader_output_json_schema",
    "validate_proposal",
]

"""S4: the LLM-driven intraday agent sleeve (``BRIEF_INTRADAY.md``).

Phase 0-1 only -- ``ledger.py`` holds S4's own SQLite ledger (deliberately separate from
``live/ledger.py``'s file: see its module docstring for why), ``budget.py`` enforces the
per-call/per-day spend caps every future LLM-calling job must check first, and
``proposals.py`` defines the trade proposal schema and its risk/budget validator. No job
or LLM call exists yet -- see ``BRIEF_INTRADAY.md``'s roadmap.
"""

from __future__ import annotations

from .budget import ModelCallCost, check_budget, record_call_cost, spent_today
from .ledger import IntradayLedger
from .proposals import TradeProposal, ValidationContext, validate_proposal

__all__ = [
    "IntradayLedger",
    "ModelCallCost",
    "TradeProposal",
    "ValidationContext",
    "check_budget",
    "record_call_cost",
    "spent_today",
    "validate_proposal",
]

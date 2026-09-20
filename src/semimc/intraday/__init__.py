"""S4: the LLM-driven intraday agent sleeve (``BRIEF_INTRADAY.md``).

Phase 0 only -- ``ledger.py`` holds S4's own SQLite ledger (deliberately separate from
``live/ledger.py``'s file: see its module docstring for why), and ``budget.py`` enforces
the per-call/per-day spend caps every future LLM-calling job must check first. No job,
proposal schema, or LLM call exists yet -- see ``BRIEF_INTRADAY.md``'s roadmap.
"""

from __future__ import annotations

from .budget import ModelCallCost, check_budget, record_call_cost, spent_today
from .ledger import IntradayLedger

__all__ = [
    "IntradayLedger",
    "ModelCallCost",
    "check_budget",
    "record_call_cost",
    "spent_today",
]

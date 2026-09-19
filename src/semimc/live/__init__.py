"""Live paper-trading jobs (``semis_paper_trading_brief.md``).

``ledger.py`` is the persistent record every job here writes to and reads back from.
``decide.py`` freezes one sleeve's daily decision with data-vintage/git-commit bookkeeping;
``execute.py`` turns an already-frozen decision into broker orders; ``mark.py`` polls fill
status, prunes stale unfilled orders, and reconciles positions; ``report.py`` reads the
ledger back into a human-readable summary. `cli.py`'s ``paper`` sub-app is a thin wrapper
around all four -- the actual logic lives here, same "one choke point" reasoning as every
other package in this project.
"""

from __future__ import annotations

from .decide import DecideResult, run_decide
from .execute import ExecutionResult, run_execute
from .ledger import Ledger
from .mark import MarkResult, run_mark
from .report import PaperReport, build_report, render_markdown

__all__ = [
    "DecideResult",
    "ExecutionResult",
    "Ledger",
    "MarkResult",
    "PaperReport",
    "build_report",
    "render_markdown",
    "run_decide",
    "run_execute",
    "run_mark",
]

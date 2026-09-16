"""Every module must import standalone, in any order.

A circular import between ``semimc.data`` and ``semimc.models`` was masked for a while by
import ordering: it only surfaced when the CLI entry point happened to import the data
layer first. Importing each module in a fresh interpreter is the only way to catch that,
because once any module is in ``sys.modules`` the cycle resolves silently.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

MODULES = [
    "semimc",
    "semimc.config",
    "semimc.context",
    "semimc.runs",
    "semimc.cli",
    "semimc.data",
    "semimc.data.cache",
    "semimc.data.earnings",
    "semimc.data.provider",
    "semimc.data.returns",
    "semimc.data.riskfree",
    "semimc.data.validation",
    "semimc.models",
    "semimc.models.base",
    "semimc.models.bootstrap",
    "semimc.models.drift",
    "semimc.models.earnings_jumps",
    "semimc.models.factor",
    "semimc.models.fhs",
    "semimc.models.garch_t",
    "semimc.models.gbm",
    "semimc.engine.runner",
    "semimc.engine",
    "semimc.analytics.convergence",
    "semimc.analytics.risk",
    "semimc.analytics.scoring",
    "semimc.backtest.scorecard",
    "semimc.backtest.walkforward",
    "semimc.scenarios.stress",
    "semimc.reporting.charts",
    "semimc.reporting.html",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_in_a_fresh_interpreter(module: str):
    """Import the module first, in its own process, with nothing else pre-loaded."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"`import {module}` failed in a fresh interpreter:\n{result.stderr}"
    )


def test_fitcontext_is_the_same_object_from_both_packages():
    """Re-exporting must not create two distinct classes."""
    from semimc.context import FitContext as FromContext
    from semimc.models.base import FitContext as FromModels

    assert FromContext is FromModels

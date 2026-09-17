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
    "semimc.data.options",
    "semimc.data.riskfree",
    "semimc.data.validation",
    "semimc.models",
    "semimc.models.base",
    "semimc.models.bootstrap",
    "semimc.models.drift",
    "semimc.models.earnings_jumps",
    "semimc.models.factor",
    "semimc.models.factor_regime",
    "semimc.models.fhs",
    "semimc.models.garch_t",
    "semimc.models.gbm",
    "semimc.models.merton",
    "semimc.models.har",
    "semimc.models.regime",
    "semimc.models.blend",
    "semimc.signals",
    "semimc.signals.build",
    "semimc.signals.external",
    "semimc.signals.supply_chain",
    "semimc.forecasting",
    "semimc.forecasting.panel",
    "semimc.forecasting.sector",
    "semimc.forecasting.cv",
    "semimc.forecasting.tilt",
    "semimc.forecasting.evaluate",
    "semimc.forecasting.combine",
    "semimc.forecasting.stability",
    "semimc.forecasting.pipeline",
    "semimc.data.realized",
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
    "semimc.dashboard.jobs",
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


def test_statsmodels_api_is_never_imported():
    """``import statsmodels.api`` pulls in the Kalman-filter state-space code, whose
    compiled extensions are blocked by Windows Application Control on the machine this was
    built on (see CLAUDE.md's Gotchas). Every submodule this project actually needs
    (regression.linear_model, regression.quantile_regression,
    tsa.regime_switching.markov_regression, stats.sandwich_covariance) imports fine on its
    own -- this test only forbids reaching for the umbrella import by mistake."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import semimc.cli, semimc.models, semimc.forecasting, semimc.signals; "
            "import sys; "
            "assert 'statsmodels.api' not in sys.modules, "
            "'statsmodels.api got imported somewhere in the semimc import graph'",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr

"""`dashboard/app.py` imports cleanly and builds its own config the same way the CLI does.

Skipped entirely when the optional ``dashboard`` extra isn't installed -- nothing else in
this project depends on Streamlit, and `uv run pytest` must stay green without it (see
``pyproject.toml``). Run `uv sync --extra dashboard` first to exercise this file.

Streamlit scripts are designed to be imported outside a real server (it logs "missing
ScriptRunContext" warnings and every widget call returns its default), which is what makes
a fresh-interpreter import -- the same isolation ``test_imports.py`` uses for every other
module -- a meaningful smoke test here: it catches a broken import or a syntax error the
same way, without needing a running server or a browser.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytest.importorskip("streamlit")


def test_app_imports_in_a_fresh_interpreter():
    result = subprocess.run(
        [sys.executable, "-c", "import semimc.dashboard.app"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"`import semimc.dashboard.app` failed in a fresh interpreter:\n{result.stderr}"
    )

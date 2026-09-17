"""Run ``semimc`` CLI commands as subprocesses and stream their output.

The dashboard never re-implements ``simulate``/``backtest``/``signals``/``scenario`` --
``cli.py`` stays the one place those commands' logic lives (CLAUDE.md's choke-point
convention). This module is the seam: it shells out to ``python -m semimc.cli <args>`` in
the *current* interpreter's environment (so it works from an editable install the same way
``uv run semimc ...`` does) and hands back output line by line as the process produces it.

Deliberately has no Streamlit import, so it can be unit-tested without the ``dashboard``
optional dependency installed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CLIJob:
    """A ``semimc`` subprocess, running or finished."""

    process: subprocess.Popen[str]
    args: list[str]

    def stream_lines(self) -> Iterator[str]:
        """Yield stdout/stderr lines as they arrive, then block until the process exits.

        Exhausting this iterator is what makes ``returncode`` meaningful -- ``Popen``
        leaves it ``None`` until something has waited on the process.
        """
        assert self.process.stdout is not None
        for line in self.process.stdout:
            yield line.rstrip("\n")
        self.process.wait()

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


def launch(args: list[str], project_root: Path) -> CLIJob:
    """Start ``python -m semimc.cli <args>``.

    stdout and stderr are merged (``typer.secho`` warnings live on stderr and belong in
    the same log the user is watching) and piped, which makes click disable ANSI colour
    automatically since the stream is not a TTY. ``PYTHONUNBUFFERED`` keeps the child's
    stdout flushed line by line -- without it, block-buffering on a pipe would hold every
    line until the process exits, and a three-minute backtest would look hung the whole
    time instead of streaming.

    ``args`` is passed as a list, never through a shell, so form input (tickers, config
    overrides) cannot be interpreted as shell syntax no matter what a user types into it.
    """
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "semimc.cli", *args],
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    return CLIJob(process=process, args=args)


def extract_run_folder(lines: list[str]) -> str | None:
    """Pull the run directory a command reported out of its captured output.

    Every command that writes a run directory ends with ``typer.echo(f"Written to
    {run.close()}")`` (``cli.py``) -- this is the one line the dashboard actually needs
    from the whole log, and reading it back is more robust than assuming the most
    recently created folder under ``runs/`` belongs to this command, which would be
    wrong if two runs were launched close together.
    """
    marker = "Written to "
    for line in reversed(lines):
        if marker in line:
            return line.split(marker, 1)[1].strip()
    return None

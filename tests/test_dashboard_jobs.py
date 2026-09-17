"""`dashboard/jobs.py`: the subprocess runner the Streamlit app is built on.

No Streamlit import here on purpose (see the module's own docstring), so these run in the
normal test environment without the optional ``dashboard`` dependency installed. Exercises
real subprocesses, wired to the fixture data the same way ``test_cli.py`` wires
``CliRunner`` invocations -- no test in this project touches the network, and a subprocess
is no exception just because it's out-of-process.
"""

from __future__ import annotations

from pathlib import Path

from semimc.config import PROJECT_ROOT
from semimc.dashboard.jobs import extract_run_folder, launch

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture_config_args(tmp_path: Path) -> list[str]:
    """``--set`` flags pointing a subprocess at the fixtures and a temp workspace.

    Forward slashes only: override values are parsed as YAML, and a Windows backslash
    would be read as an escape (see ``test_cli.py::base_overrides``, the same pattern).
    """
    fixtures = FIXTURES.as_posix()
    return [
        "--set",
        "data.provider=csv",
        "--set",
        f"data.csv_dir={fixtures}",
        "--set",
        f"data.cache_dir={(tmp_path / 'cache').as_posix()}",
        "--set",
        f"reporting.runs_dir={(tmp_path / 'runs').as_posix()}",
        "--set",
        "universe.groups={fixture: [NVDA, AMD]}",
        "--set",
        "fit.min_observations=300",
        "--set",
        "fit.window_years=4",
    ]


def test_launch_streams_stdout_from_a_real_command(tmp_path):
    job = launch(["models"], PROJECT_ROOT)
    lines = list(job.stream_lines())
    assert any("gbm" in line for line in lines)
    assert job.succeeded
    assert job.returncode == 0


def test_launch_runs_a_real_fixture_backed_command_end_to_end(tmp_path):
    fetch = launch(["fetch"] + fixture_config_args(tmp_path), PROJECT_ROOT)
    fetch_lines = list(fetch.stream_lines())
    assert fetch.succeeded, "\n".join(fetch_lines)

    job = launch(
        ["simulate", "--ticker", "NVDA", "--horizon", "21", "--paths", "200"]
        + fixture_config_args(tmp_path),
        PROJECT_ROOT,
    )
    lines = list(job.stream_lines())
    assert job.succeeded, "\n".join(lines)

    run_folder = extract_run_folder(lines)
    assert run_folder is not None
    assert Path(run_folder).is_dir()
    assert (Path(run_folder) / "manifest.json").exists()


def test_launch_reports_failure_for_a_bad_invocation(tmp_path):
    # simulate requires exactly one of --ticker/--portfolio; giving neither is a
    # BadParameter raised before any data is touched.
    job = launch(["simulate", "--horizon", "21"] + fixture_config_args(tmp_path), PROJECT_ROOT)
    lines = list(job.stream_lines())
    assert not job.succeeded
    assert job.returncode != 0
    assert any("ticker" in line.lower() or "portfolio" in line.lower() for line in lines)


def test_extract_run_folder_finds_the_written_to_line():
    lines = [
        "Fitting gbm on 1234 days ...",
        "",
        "Risk table -- NVDA, 126 trading days, 10000 paths, model gbm:",
        "Written to C:\\repo\\runs\\20260101-120000-simulate-NVDA",
    ]
    assert extract_run_folder(lines) == "C:\\repo\\runs\\20260101-120000-simulate-NVDA"


def test_extract_run_folder_returns_none_when_absent():
    assert extract_run_folder(["some", "unrelated", "output"]) is None


def test_extract_run_folder_uses_the_last_match():
    # Only the very last "Written to" line -- the run directory itself -- should count,
    # not any incidental earlier line that happens to contain the same words.
    lines = ["Written to nothing/real/yet", "more output", "Written to C:\\repo\\runs\\real-one"]
    assert extract_run_folder(lines) == "C:\\repo\\runs\\real-one"

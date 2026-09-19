"""`dashboard/server.py`: the stdlib HTTP backend the HTML dashboard runs on.

No third-party dependency here (see the module's own docstring), so -- unlike the old
Streamlit app -- these run in the normal test environment with nothing extra installed.
Exercises a real server bound to an ephemeral port on localhost, wired to the fixture data
the same way ``test_dashboard_jobs.py`` and ``test_cli.py`` are; this is loopback IPC to a
server this test process itself started, not the network access the project's "no network
in tests" rule is about.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from semimc.dashboard.server import Handler

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture_overrides(tmp_path: Path) -> list[str]:
    """Bare ``key=value`` overrides -- the fixture wiring ``test_dashboard_jobs.py`` uses."""
    fixtures = FIXTURES.as_posix()
    return [
        "data.provider=csv",
        f"data.csv_dir={fixtures}",
        f"data.cache_dir={(tmp_path / 'cache').as_posix()}",
        f"reporting.runs_dir={(tmp_path / 'runs').as_posix()}",
        "universe.groups={fixture: [NVDA, AMD]}",
        "fit.min_observations=300",
        "fit.window_years=4",
    ]


def fixture_config_args(tmp_path: Path) -> list[str]:
    """The above, as ``--set`` CLI flags -- for a job's ``args`` list."""
    args = []
    for override in _fixture_overrides(tmp_path):
        args += ["--set", override]
    return args


def fixture_query(tmp_path: Path) -> str:
    """The same overrides, as ``set=`` query params -- for a GET request."""
    from urllib.parse import urlencode

    return urlencode([("set", o) for o in _fixture_overrides(tmp_path)])


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url: str) -> tuple[int, dict | str]:
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
            body = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        status = exc.code
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, body


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _wait_for_job(base: str, job_id: str, timeout: float = 60.0) -> dict:
    since = 0
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, data = _get(f"{base}/api/jobs/{job_id}?since={since}")
        assert status == 200, data
        lines.extend(data["lines"])
        since = data["total"]
        if data["done"]:
            data["all_lines"] = lines
            return data
        time.sleep(0.1)
    raise TimeoutError(
        f"job {job_id} did not finish in {timeout}s; log so far:\n" + "\n".join(lines)
    )


# -- static files ---------------------------------------------------------------------


def test_serves_the_dashboard_index(server):
    status, body = _get(f"{server}/")
    assert status == 200
    assert "semimc" in body


def test_static_path_traversal_is_blocked(server):
    status, _ = _get(f"{server}/static/../server.py")
    assert status == 404


def test_unknown_path_is_404(server):
    status, _ = _get(f"{server}/nope")
    assert status == 404


# -- config -----------------------------------------------------------------------------


def test_api_config_reflects_the_fixture_universe(server, tmp_path):
    status, data = _get(f"{server}/api/config?{fixture_query(tmp_path)}")
    assert status == 200
    assert sorted(data["universe_tickers"]) == ["AMD", "NVDA"]
    assert "gbm" in data["models"]
    assert data["backtest"]["n_paths"] > 0


def test_api_config_reports_a_bad_override_as_400(server):
    status, data = _get(f"{server}/api/config?set=not.a.real.key=1")
    assert status == 400
    assert "error" in data


# -- jobs -------------------------------------------------------------------------------


def test_job_lifecycle_runs_a_real_fixture_backed_command(server, tmp_path):
    args = ["fetch"] + fixture_config_args(tmp_path)
    status, started = _post_json(f"{server}/api/jobs", {"args": args})
    assert status == 202
    data = _wait_for_job(server, started["id"])
    assert data["returncode"] == 0, "\n".join(data["all_lines"])
    assert data["done"] is True


def test_job_reports_failure_for_a_bad_invocation(server, tmp_path):
    args = ["simulate", "--horizon", "21"] + fixture_config_args(tmp_path)
    status, started = _post_json(f"{server}/api/jobs", {"args": args})
    assert status == 202
    data = _wait_for_job(server, started["id"])
    assert data["returncode"] != 0


def test_unknown_command_is_rejected_before_launch(server):
    status, data = _post_json(f"{server}/api/jobs", {"args": ["rm", "-rf", "/"]})
    assert status == 400
    assert "not a runnable dashboard command" in data["error"]


def test_holdout_flag_is_rejected_before_launch(server, tmp_path):
    args = ["signals", "--holdout"] + fixture_config_args(tmp_path)
    status, data = _post_json(f"{server}/api/jobs", {"args": args})
    assert status == 400
    assert "holdout" in data["error"].lower()


def test_unknown_job_id_is_404(server):
    status, data = _get(f"{server}/api/jobs/{'0' * 32}")
    assert status == 404


# -- runs and reports ---------------------------------------------------------------------


def test_runs_and_report_round_trip(server, tmp_path):
    fetch_status, fetch_started = _post_json(
        f"{server}/api/jobs", {"args": ["fetch"] + fixture_config_args(tmp_path)}
    )
    assert fetch_status == 202
    assert _wait_for_job(server, fetch_started["id"])["returncode"] == 0

    sim_status, sim_started = _post_json(
        f"{server}/api/jobs",
        {
            "args": [
                "simulate",
                "--ticker",
                "NVDA",
                "--horizon",
                "21",
                "--paths",
                "200",
            ]
            + fixture_config_args(tmp_path)
        },
    )
    assert sim_status == 202
    sim_data = _wait_for_job(server, sim_started["id"])
    assert sim_data["returncode"] == 0, "\n".join(sim_data["all_lines"])
    run_name = sim_data["run"]
    assert run_name is not None

    query = fixture_query(tmp_path)
    status, runs = _get(f"{server}/api/runs?{query}")
    assert status == 200
    assert any(r["name"] == run_name for r in runs["runs"])

    # no report yet -- build it via the report job, same as the browser would on first view
    status, _ = _get(f"{server}/api/runs/{run_name}/report?{query}")
    assert status == 404

    report_status, report_started = _post_json(
        f"{server}/api/jobs",
        {"args": ["report", "--run", run_name] + fixture_config_args(tmp_path)},
    )
    assert report_status == 202
    assert _wait_for_job(server, report_started["id"])["returncode"] == 0

    status, body = _get(f"{server}/api/runs/{run_name}/report?{query}")
    assert status == 200
    assert "<html" in body.lower()


def test_artifact_traversal_is_blocked(server, tmp_path):
    fetch_status, fetch_started = _post_json(
        f"{server}/api/jobs", {"args": ["fetch"] + fixture_config_args(tmp_path)}
    )
    assert fetch_status == 202
    assert _wait_for_job(server, fetch_started["id"])["returncode"] == 0

    sim_status, sim_started = _post_json(
        f"{server}/api/jobs",
        {
            "args": [
                "simulate",
                "--ticker",
                "NVDA",
                "--horizon",
                "21",
                "--paths",
                "200",
            ]
            + fixture_config_args(tmp_path)
        },
    )
    sim_data = _wait_for_job(server, sim_started["id"])
    run_name = sim_data["run"]

    query = fixture_query(tmp_path)
    status, _ = _get(f"{server}/api/runs/{run_name}/artifact/..%2F..%2Fserver.py?{query}")
    assert status == 404


def test_server_imports_in_a_fresh_interpreter():
    result = subprocess.run(
        [sys.executable, "-c", "import semimc.dashboard.server"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"import failed:\n{result.stderr}"

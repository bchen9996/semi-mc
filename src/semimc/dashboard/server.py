"""HTML dashboard: a browser front end that drives the CLI, not a replacement for it.

Run with:

    uv run semimc dashboard

This is the Streamlit app's replacement -- same seam, different rendering. The browser's
own JS (``web/app.js``) builds a ``semimc <command> ...`` argument list from form input,
the same list a terminal user would type, and POSTs it here; this module hands it to
:func:`semimc.dashboard.jobs.launch`, the same subprocess seam the Streamlit app used, and
streams the result back. That keeps exactly one implementation of
``simulate``/``backtest``/``signals``/``scenario`` (``cli.py``) and means the dashboard can
never drift out of sync with it.

Stdlib ``http.server`` only -- no third-party dependency, so there is no more
``--extra dashboard`` install step. ``ThreadingHTTPServer`` handles each request (including
each job-status poll) on its own thread; a running job's output accumulates in memory under
a lock, keyed by job id, so multiple browser tabs can watch the same job.

One deliberate omission, carried over from the Streamlit app: ``signals --holdout`` cannot
be launched from here. It is a one-time, already-spent evaluation (research_log.md,
2026-09-16) and CLAUDE.md forbids re-running it; a dashboard button is exactly the kind of
casual, repeatable action that resource must never be. Use the CLI directly, deliberately,
if that ever needs to change.
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from semimc.config import PROJECT_ROOT, load_config
from semimc.dashboard.jobs import extract_run_folder, launch
from semimc.models import MODEL_NAMES
from semimc.runs import find_run, load_manifest

WEB_DIR = Path(__file__).resolve().parent / "web"

# Every command reachable from the dashboard. `report` and `models` are here too --
# the browser calls `report` itself to (re)build a run's HTML report on demand.
ALLOWED_COMMANDS = {"fetch", "validate", "simulate", "backtest", "signals", "scenario", "report"}


# ------------------------------------------------------------------------------------------
# Job registry: one background thread per launched command, polled over HTTP
# ------------------------------------------------------------------------------------------


@dataclass
class _Job:
    lines: list[str] = field(default_factory=list)
    done: bool = False
    returncode: int | None = None
    run: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


_JOBS: dict[str, _Job] = {}
_JOBS_LOCK = threading.Lock()


def start_job(args: list[str]) -> str:
    """Validate and launch a ``semimc`` command in the background; return its job id."""
    if not args or args[0] not in ALLOWED_COMMANDS:
        raise ValueError(f"not a runnable dashboard command: {args[0] if args else '(empty)'}")
    if args[0] == "signals" and "--holdout" in args:
        raise ValueError(
            "signals --holdout is a one-time, already-spent evaluation -- run it from a "
            "terminal, deliberately, not from a dashboard button (see research_log.md)."
        )

    job_id = uuid.uuid4().hex
    job = _Job()
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    def _run() -> None:
        cli_job = launch(args, PROJECT_ROOT)
        for line in cli_job.stream_lines():
            with job.lock:
                job.lines.append(line)
        with job.lock:
            job.done = True
            job.returncode = cli_job.returncode
            job.run = extract_run_folder(job.lines)

    threading.Thread(target=_run, daemon=True).start()
    return job_id


def job_status(job_id: str, since: int) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise FileNotFoundError(f"unknown job id {job_id!r}")
    with job.lock:
        return {
            "lines": job.lines[since:],
            "total": len(job.lines),
            "done": job.done,
            "returncode": job.returncode,
            "run": Path(job.run).name if job.run else None,
        }


# ------------------------------------------------------------------------------------------
# HTTP handler
# ------------------------------------------------------------------------------------------

_JOB_ID = re.compile(r"^/api/jobs/([0-9a-f]{32})$")
_RUN_REPORT = re.compile(r"^/api/runs/([^/]+)/report$")
_RUN_ARTIFACT = re.compile(r"^/api/runs/([^/]+)/artifact/([^/]+)$")


class Handler(BaseHTTPRequestHandler):
    server_version = "semimc-dashboard/1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # the job log the browser streams is the log that matters here

    # -- response helpers ------------------------------------------------------------

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: object, status: int = 200) -> None:
        self._send_bytes(json.dumps(payload).encode("utf-8"), "application/json", status)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _send_html(self, text: str, status: int = 200) -> None:
        self._send_bytes(text.encode("utf-8"), "text/html; charset=utf-8", status)

    @staticmethod
    def _query(parsed) -> dict[str, list[str]]:
        return parse_qs(parsed.query)

    @staticmethod
    def _config_from_query(query: dict[str, list[str]]):
        config_path = (query.get("config", [""])[0] or "").strip() or None
        overrides = query.get("set", []) or None
        return load_config(config_path, overrides)

    # -- static files ------------------------------------------------------------------

    def _serve_static(self, rel_path: str) -> None:
        rel_path = rel_path or "index.html"
        root = WEB_DIR.resolve()
        candidate = (root / rel_path).resolve()
        if candidate != root and root not in candidate.parents:
            return self._send_error_json(404, "not found")
        if not candidate.is_file():
            return self._send_error_json(404, "not found")
        content_type, _ = mimetypes.guess_type(str(candidate))
        self._send_bytes(candidate.read_bytes(), content_type or "application/octet-stream")

    # -- API: config and runs -----------------------------------------------------------

    def _api_config(self, query: dict[str, list[str]]) -> None:
        cfg = self._config_from_query(query)
        self._send_json(
            {
                "universe_tickers": cfg.universe.tickers,
                "all_tickers": cfg.all_tickers,
                "benchmark_tickers": cfg.benchmarks.tickers,
                "groups": cfg.universe.groups,
                "drift_policy": cfg.drift.policy,
                "horizons": cfg.horizons,
                "scenarios": list(cfg.scenarios),
                "models": MODEL_NAMES,
                "simulation": {"n_paths": cfg.simulation.n_paths, "seed": cfg.simulation.seed},
                "backtest": {
                    "models": list(cfg.backtest.models),
                    "start": str(cfg.backtest.start),
                    "n_paths": cfg.backtest.n_paths,
                },
                "signals_horizons": cfg.signals.horizons,
            }
        )

    def _api_runs(self, query: dict[str, list[str]]) -> None:
        cfg = self._config_from_query(query)
        rows = []
        if cfg.runs_path.exists():
            for folder in sorted(cfg.runs_path.iterdir(), reverse=True):
                if not folder.is_dir():
                    continue
                try:
                    manifest = load_manifest(folder)
                except FileNotFoundError:
                    manifest = {}
                rows.append(
                    {
                        "name": folder.name,
                        "command": manifest.get("command", "-") if manifest else "(incomplete)",
                        "created_at": manifest.get("created_at", "-"),
                        "seed": manifest.get("seed"),
                        "has_report": (folder / "report.html").exists(),
                        "artifacts": manifest.get("artifacts", []),
                    }
                )
        self._send_json({"runs": rows})

    def _api_report(self, run_name: str, query: dict[str, list[str]]) -> None:
        cfg = self._config_from_query(query)
        folder = find_run(cfg.runs_path, run_name)
        report_path = folder / "report.html"
        if not report_path.exists():
            raise FileNotFoundError("this run has no report.html yet")
        self._send_html(report_path.read_text(encoding="utf-8"))

    def _api_artifact(self, run_name: str, filename: str, query: dict[str, list[str]]) -> None:
        cfg = self._config_from_query(query)
        folder = find_run(cfg.runs_path, run_name).resolve()
        candidate = (folder / filename).resolve()
        if folder not in candidate.parents or not candidate.is_file():
            raise FileNotFoundError("no such artifact")
        content_type, _ = mimetypes.guess_type(str(candidate))
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{candidate.name}"')
        body = candidate.read_bytes()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routing -------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = self._query(parsed)
        try:
            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/") :])
            if path == "/api/config":
                return self._api_config(query)
            if path == "/api/runs":
                return self._api_runs(query)
            match = _JOB_ID.match(path)
            if match:
                since = int((query.get("since", ["0"])[0]) or "0")
                return self._send_json(job_status(match.group(1), since))
            match = _RUN_REPORT.match(path)
            if match:
                return self._api_report(match.group(1), query)
            match = _RUN_ARTIFACT.match(path)
            if match:
                return self._api_artifact(match.group(1), match.group(2), query)
            self._send_error_json(404, "not found")
        except FileNotFoundError as exc:
            self._send_error_json(404, str(exc))
        except ValueError as exc:
            self._send_error_json(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(500, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/jobs":
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                args = payload.get("args")
                if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                    raise ValueError("args must be a list of strings")
                job_id = start_job(args)
                return self._send_json({"id": job_id}, status=202)
            self._send_error_json(404, "not found")
        except FileNotFoundError as exc:
            self._send_error_json(404, str(exc))
        except ValueError as exc:
            self._send_error_json(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(500, str(exc))


# ------------------------------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------------------------------


def run_server(port: int = 8765, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"semimc dashboard: {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()

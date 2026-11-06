"""`.github/scripts/push_ledger.sh` and the workflows that call it.

A rejected push used to reset the runner to the latest main and re-run the whole job
command. For `cycle` that repeated the Trader call (billed, but only the last attempt
reached the ledger), and on the real account it would have thrown away the ledger rows
of orders already at the broker. The script rebases the job's commit onto the latest
main instead. These tests drive it against throwaway local repositories (no network):
a push raced by a different file must land after a rebase, a push raced by the same
binary ledger file must stop with exit code 3 and leave nothing half-rebased, and a push
the server refuses a few times (GitHub's "Internal Server Error" on 2026-11-05) must
land once it accepts one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "push_ledger.sh"
WORKFLOWS = ROOT / ".github" / "workflows"


def _bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    # A bare `bash` can resolve to the WSL launcher; use Git for Windows' own, found from
    # git.exe, which may sit in `Git\cmd` or `Git\mingw64\bin`.
    git = shutil.which("git")
    if git is None:
        return None
    for parent in Path(git).resolve().parents:
        candidate = parent / "bin" / "bash.exe"
        if candidate.exists():
            return str(candidate)
    return None


BASH = _bash()


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    global_config = tmp_path / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(global_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        # Seven attempts as in production, without its 2.5 minutes of waiting.
        "PUSH_LEDGER_WAITS": "0 0 0 0 0 0",
    }


def _git(env: dict[str, str], cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _ledger_bytes(tag: str) -> bytes:
    # A NUL byte makes git treat the file as binary, like the real SQLite ledgers.
    return b"SQLite format 3\x00" + tag.encode() * 64


def _commit(env: dict[str, str], repo: Path, path: str, tag: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_ledger_bytes(tag))
    _git(env, repo, "add", path)
    _git(env, repo, "commit", "-q", "-m", f"{path}: {tag}")


@pytest.fixture
def repos(tmp_path: Path, git_env: dict[str, str]) -> tuple[Path, Path, Path]:
    """A bare origin holding both ledgers, plus two clones: the job and another workflow."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(git_env, seed, "init", "-q", "-b", "main")
    _commit(git_env, seed, "ledger/intraday.sqlite3", "seed")
    _commit(git_env, seed, "ledger/paper.sqlite3", "seed")
    origin = tmp_path / "origin.git"
    _git(git_env, tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    job = tmp_path / "job"
    other = tmp_path / "other"
    for clone in (job, other):
        _git(git_env, tmp_path, "clone", "-q", str(origin), str(clone))
    return origin, job, other


def _run_script(env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, SCRIPT.as_posix()], cwd=cwd, env=env, capture_output=True, text=True
    )


needs_bash = pytest.mark.skipif(BASH is None, reason="no bash to run the script with")


@needs_bash
def test_pushes_when_nothing_raced(repos, git_env):
    origin, job, _ = repos
    _commit(git_env, job, "ledger/intraday.sqlite3", "cycle")

    result = _run_script(git_env, job)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(git_env, origin, "log", "-1", "--format=%s", "main") == (
        "ledger/intraday.sqlite3: cycle"
    )


@needs_bash
def test_rebases_past_a_push_to_a_different_file(repos, git_env):
    origin, job, other = repos
    _commit(git_env, job, "ledger/intraday.sqlite3", "cycle")
    # A paper mark pushes between the job's checkout and its push.
    _commit(git_env, other, "ledger/paper.sqlite3", "mark")
    _git(git_env, other, "push", "-q")

    result = _run_script(git_env, job)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "rebasing onto the latest main" in result.stdout
    subjects = _git(git_env, origin, "log", "--format=%s", "main").splitlines()
    assert subjects[:2] == ["ledger/intraday.sqlite3: cycle", "ledger/paper.sqlite3: mark"]
    # Each side's ledger survives intact: nothing was re-run or overwritten.
    _git(git_env, other, "pull", "-q")
    assert (other / "ledger/intraday.sqlite3").read_bytes() == _ledger_bytes("cycle")
    assert (other / "ledger/paper.sqlite3").read_bytes() == _ledger_bytes("mark")


@needs_bash
def test_stops_with_exit_3_when_the_same_ledger_changed(repos, git_env):
    origin, job, other = repos
    _commit(git_env, job, "ledger/intraday.sqlite3", "cycle")
    _commit(git_env, other, "ledger/intraday.sqlite3", "monitor")
    _git(git_env, other, "push", "-q")
    upstream = _git(git_env, origin, "rev-parse", "main")

    result = _run_script(git_env, job)

    assert result.returncode == 3, result.stdout + result.stderr
    assert "another run changed the same ledger file" in result.stdout
    # Origin keeps the other run's ledger; the job is left on its own commit, not
    # mid-rebase, so the caller can fail cleanly (intraday) or re-run (paper).
    assert _git(git_env, origin, "rev-parse", "main") == upstream
    assert not (job / ".git" / "rebase-merge").exists()
    assert not (job / ".git" / "rebase-apply").exists()
    assert _git(git_env, job, "log", "-1", "--format=%s") == "ledger/intraday.sqlite3: cycle"


def _refuse_pushes(origin: Path, times: int) -> None:
    """Makes `origin` refuse its next `times` pushes, the way GitHub's servers did."""
    counter = origin / "refusals_left"
    counter.write_text(str(times), encoding="utf-8")
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        f'left=$(cat "{counter.as_posix()}")\n'
        'if [ "$left" -gt 0 ]; then\n'
        f'  echo $((left - 1)) > "{counter.as_posix()}"\n'
        '  echo "Internal Server Error" >&2\n'
        "  exit 1\n"
        "fi\n",
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o755)


@needs_bash
def test_retries_a_push_the_server_refuses_until_it_lands(repos, git_env):
    origin, job, _ = repos
    _commit(git_env, job, "ledger/intraday.sqlite3", "cycle")
    # 2026-11-05: a race, then two server errors, and the old script's third attempt was
    # its last.
    _refuse_pushes(origin, 3)

    result = _run_script(git_env, job)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("push rejected (attempt") == 3
    assert _git(git_env, origin, "log", "-1", "--format=%s", "main") == (
        "ledger/intraday.sqlite3: cycle"
    )


@needs_bash
def test_gives_up_with_exit_1_once_every_attempt_is_refused(repos, git_env):
    origin, job, _ = repos
    _commit(git_env, job, "ledger/intraday.sqlite3", "cycle")
    upstream = _git(git_env, origin, "rev-parse", "main")
    _refuse_pushes(origin, 100)

    result = _run_script(git_env, job)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "failed to push after 7 attempts" in result.stdout
    assert _git(git_env, origin, "rev-parse", "main") == upstream


def test_push_retries_wait_about_two_and_a_half_minutes_in_all():
    # Long enough to outlast a burst of server errors, short of the next 30-minute cycle.
    match = re.search(r"PUSH_LEDGER_WAITS:-([0-9 ]+)\}", SCRIPT.read_text(encoding="utf-8"))
    assert match is not None
    waits = [int(w) for w in match.group(1).split()]
    assert len(waits) == 6
    assert 120 <= sum(waits) <= 180


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_eod_review_commits_the_placebos_ledger_too():
    """It prices both sleeves' exits (`intraday/hedge.py`). Without this the placebo's blocks
    would be lost with the runner, and it would pay to price them again every day."""
    steps = _workflow("intraday_trading.yml")["jobs"]["eod-review"]["steps"]
    script = "\n".join(step.get("run", "") for step in steps)

    assert "git add ledger/intraday.sqlite3 ledger/intraday_shadow.sqlite3" in script


def _scripts(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_intraday_keeps_crons_only_for_jobs_a_late_copy_still_helps():
    # Owner decision 2026-10-08: GitHub delivers these crons hours late, and a late copy
    # waiting in the shared concurrency group cancels a pending on-time dispatch (the
    # likely loss of 2026-10-07's 12:00 CT cycle). Only carry, eod-review and
    # weekly-review keep one: their late copies land after the close and still have work
    # to do if the on-time run failed.
    workflow = _workflow("intraday_trading.yml")
    crons = {entry["cron"] for entry in workflow["on"]["schedule"]}
    assert crons == {"45 20 * * 1-5", "15 21 * * 1-5", "45 21 * * 5"}
    for name, job in workflow["jobs"].items():
        condition = job["if"]
        if "github.event.schedule" in condition:
            assert name in {"carry", "eod-review", "weekly-review"}, name
            assert any(f"'{cron}'" in condition for cron in crons), name


def test_ledger_jobs_push_through_the_script_and_never_rerun_a_trading_command():
    intraday = _workflow("intraday_trading.yml")["jobs"]
    for name, job in intraday.items():
        if name == "monitor":  # manual only, outside the group; see its job comment
            continue
        script = _scripts(job)
        assert ".github/scripts/push_ledger.sh" in script, name
        assert "for attempt" not in script, name
    paper = _workflow("paper_trading.yml")["jobs"]
    for name in ("decide-and-execute", "mark"):
        assert ".github/scripts/push_ledger.sh" in _scripts(paper[name]), name


def test_every_job_runs_on_the_pinned_runner_image():
    # `ubuntu-latest` began moving to Ubuntu 26 on 2026-10-19. A trading job's operating
    # system should change because someone decided to, not because a label moved.
    for path in WORKFLOWS.glob("*.yml"):
        for name, job in _workflow(path.name)["jobs"].items():
            assert job["runs-on"] == "ubuntu-24.04", (path.name, name)


def test_only_sector_shadow_fetches_its_gitignored_series_and_a_failure_only_warns():
    # A runner starts without `data/external/IPG3344S.csv`, and without this step the sleeve
    # falls back to S0 with nothing but a diagnostics field to say so.
    steps = _workflow("paper_trading.yml")["jobs"]["decide-and-execute"]["steps"]
    [fetch] = [step for step in steps if "--industry" in step.get("run", "")]
    assert fetch["if"] == "matrix.sleeve == 'sector_shadow'"
    assert fetch["continue-on-error"] is True
    decide = next(step for step in steps if "paper decide" in step.get("run", ""))
    assert steps.index(fetch) < steps.index(decide)

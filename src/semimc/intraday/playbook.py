"""`PLAYBOOK.md`'s versioned store and the human accept/reject/retire actions on it
(`BRIEF_INTRADAY.md`'s "Self-improvement rules"), Phase 10.

Not one of the brief's own named files (`jobs.py`/`context.py`/`proposals.py`/
`monitor.py`/`budget.py`/`options.py`/`placebo.py`/`review.py`) -- a disclosed departure
from Phase 9b's own "match the brief's file list" precedent (`review.py`'s module
docstring), because none of those names fit a versioned-document store with human
accept/reject/retire actions on it. `review.py` stays "mechanical scoring, attribution,
calibration"; this is a different concern.

`BRIEF_INTRADAY.md`'s schedule table names no agent for `weekly_review` (unlike
`research (Researcher agent)`/`eod_review (Reviewer agent)`), and the "Agent roles"
section lists exactly three model roles -- confirmed directly before designing anything
here. Accepting or rejecting a playbook proposal is also explicitly "Ask first" and
"accepted or rejected by me... not automatically" in the brief's own boundaries. So
nothing in this module is a scheduled job or a model call: every function here is a
plain, human-invoked action (`cli.py`'s `intraday playbook` commands), never called from
`jobs.py::run_weekly_review` itself.

The evidence bar ("at least 10 trading days or 20 relevant trades... unless it is a bug
or process fix") is enforced here in code, not left to the Reviewer's own prose --
`accept_proposal` takes the trading-day/trade counts as explicit arguments the *user*
supplies on the command line (`PlaybookEvidenceError` otherwise), rather than trying to
parse them back out of the proposal's own freeform text. That text is never structured
(Phase 9b's `reviewer_output_json_schema` deliberately kept `playbook_proposal` a plain
string, matching the brief's own "as a diff" framing), so a code-side parser would be
fragile in exactly the way this project has avoided elsewhere; a human reading the
proposal and asserting the numbers is the honest alternative.

`review_date = adopted_at + playbook.review_after_days` uses calendar days, not
trading-session-precise arithmetic -- the same "flat window, not calendar-derived"
simplification `IntradayResearchConfig`'s own docstring already discloses for its
lookback windows, chosen here for the identical reason: it needs no `Broker`/market-
calendar dependency in a command that otherwise only touches the ledger and the
filesystem.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pandas as pd

from ..config import Config
from .ledger import IntradayLedger


class PlaybookEvidenceError(Exception):
    """`accept_proposal`'s evidence gate wasn't met and `bug_fix` wasn't asserted."""


def _proposal_path(config: Config, date: str) -> Path:
    return config.path(config.intraday.proposals_dir) / f"{date}.md"


def _proposal_key(config: Config, path: Path) -> str:
    """The string `record_playbook_decision` stores/compares against -- relative to
    `proposals_dir`, not resolved to an absolute path, so it stays stable regardless of
    where the repo checkout lives."""
    return f"{config.intraday.proposals_dir}/{path.name}"


def render_playbook_markdown(entries: list[dict[str, Any]]) -> str:
    """`PLAYBOOK.md`'s own content -- every currently-`active` entry, oldest first, each
    showing its id (the version a trade proposal can be traced back to via
    `ledger.record_playbook_version`'s own snapshot history), adoption date, and review
    date. A retired or rejected entry never appears here -- `entries` is expected to be
    `ledger.active_playbook_entries()`'s own output."""
    lines = ["# PLAYBOOK.md", ""]
    if not entries:
        lines.append("No active rules yet.")
    for row in entries:
        adopted = pd.Timestamp(row["adopted_at"]).date()
        review = pd.Timestamp(row["review_date"]).date()
        lines.append(f"## Entry {row['id']} (adopted {adopted}, review by {review})")
        lines.append("")
        lines.append(row["text"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def pending_proposal_paths(config: Config, ledger: IntradayLedger) -> list[Path]:
    """Files under `proposals_dir` not yet referenced by any `playbook_entries` row --
    what the user still has to accept or reject. Empty (not an error) if `proposals_dir`
    doesn't exist yet -- routine before the Reviewer has ever proposed anything."""
    proposals_dir = config.path(config.intraday.proposals_dir)
    if not proposals_dir.exists():
        return []
    resolved = {row["source_proposal_path"] for row in ledger.all_playbook_entries()}
    return sorted(p for p in proposals_dir.glob("*.md") if _proposal_key(config, p) not in resolved)


def _rewrite_playbook(
    config: Config, ledger: IntradayLedger, change_kind: str, entry_id: int
) -> int:
    text = render_playbook_markdown(ledger.active_playbook_entries())
    version = ledger.record_playbook_version(text, change_kind, entry_id)
    config.path(config.intraday.playbook_path).write_text(text, encoding="utf-8")
    return version


def accept_proposal(
    config: Config,
    ledger: IntradayLedger,
    date: str,
    *,
    evidence_trading_days: int | None = None,
    evidence_trades: int | None = None,
    bug_fix: bool = False,
    now: dt.datetime,
) -> int:
    """Accepts `proposals/<date>.md` -- the user's own decision at the weekly review,
    never automatic. Returns the new `PLAYBOOK.md` version number.

    Raises `PlaybookEvidenceError` unless `bug_fix` is set or one of
    `evidence_trading_days`/`evidence_trades` meets `config.intraday.playbook`'s
    configured minimum -- BRIEF_INTRADAY.md's own "at least 10 trading days or 20
    relevant trades... unless it is a bug or process fix," enforced in code rather than
    trusted from the proposal's own prose (see module docstring).
    """
    path = _proposal_path(config, date)
    if not path.exists():
        raise FileNotFoundError(f"no playbook proposal at {path}")

    playbook_cfg = config.intraday.playbook
    meets_bar = (
        bug_fix
        or (
            evidence_trading_days is not None
            and evidence_trading_days >= playbook_cfg.evidence_min_trading_days
        )
        or (evidence_trades is not None and evidence_trades >= playbook_cfg.evidence_min_trades)
    )
    if not meets_bar:
        raise PlaybookEvidenceError(
            f"needs at least {playbook_cfg.evidence_min_trading_days} trading days or "
            f"{playbook_cfg.evidence_min_trades} relevant trades behind it (or --bug-fix)"
            " -- BRIEF_INTRADAY.md's playbook change threshold"
        )

    text = path.read_text(encoding="utf-8")
    adopted_at = pd.Timestamp(now).normalize()
    review_date = adopted_at + pd.Timedelta(days=playbook_cfg.review_after_days)
    entry_id = ledger.record_playbook_decision(
        "active",
        text,
        _proposal_key(config, path),
        adopted_at=adopted_at,
        review_date=review_date,
    )
    return _rewrite_playbook(config, ledger, "accepted", entry_id)


def reject_proposal(config: Config, ledger: IntradayLedger, date: str, reason: str) -> int:
    """Rejects `proposals/<date>.md` -- records the decision, touches neither
    `PLAYBOOK.md` nor its version history. Returns the new `playbook_entries` row id."""
    path = _proposal_path(config, date)
    if not path.exists():
        raise FileNotFoundError(f"no playbook proposal at {path}")
    text = path.read_text(encoding="utf-8")
    return ledger.record_playbook_decision(
        "rejected", text, _proposal_key(config, path), resolution_reason=reason
    )


def retire_entry(
    config: Config, ledger: IntradayLedger, entry_id: int, reason: str, now: dt.datetime
) -> int:
    """Retires a currently-active entry -- "rules that stop holding are retired"
    (BRIEF_INTRADAY.md). Returns the new `PLAYBOOK.md` version number."""
    entry = ledger.get_playbook_entry(entry_id)
    if entry is None or entry["status"] != "active":
        raise ValueError(f"playbook entry {entry_id} is not currently active")
    ledger.update_playbook_entry_status(entry_id, "retired", pd.Timestamp(now).normalize(), reason)
    return _rewrite_playbook(config, ledger, "retired", entry_id)

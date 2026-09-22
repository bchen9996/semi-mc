"""`intraday/playbook.py`: `PLAYBOOK.md`'s versioned store and the human accept/reject/
retire actions on it (S4, Phase 10). No LLM, no broker -- every function here is a plain,
human-invoked action operating on the ledger and the filesystem.
"""

from __future__ import annotations

import datetime as dt

import pytest

from semimc.intraday.ledger import IntradayLedger
from semimc.intraday.playbook import (
    PlaybookEvidenceError,
    accept_proposal,
    pending_proposal_paths,
    reject_proposal,
    render_playbook_markdown,
    retire_entry,
)

NOW = dt.datetime(2026, 9, 21, 15, 45)


def _ledger(tmp_path) -> IntradayLedger:
    return IntradayLedger(tmp_path / "intraday.sqlite3")


def _playbook_config(config, tmp_path, **playbook_overrides):
    playbook = config.intraday.playbook.model_copy(update=playbook_overrides)
    intraday = config.intraday.model_copy(
        update={
            "proposals_dir": str(tmp_path / "proposals"),
            "playbook_path": str(tmp_path / "PLAYBOOK.md"),
            "playbook": playbook,
        }
    )
    return config.model_copy(update={"intraday": intraday})


def _write_proposal(cfg, date: str, text: str) -> None:
    proposals_dir = cfg.path(cfg.intraday.proposals_dir)
    proposals_dir.mkdir(parents=True, exist_ok=True)
    (proposals_dir / f"{date}.md").write_text(text, encoding="utf-8")


# -- render_playbook_markdown --------------------------------------------------------------


def test_render_playbook_markdown_says_no_active_rules_yet_when_empty():
    assert "No active rules yet." in render_playbook_markdown([])


def test_render_playbook_markdown_shows_id_dates_and_text():
    entries = [
        {
            "id": 1,
            "adopted_at": "2026-09-21 00:00:00",
            "review_date": "2026-10-21 00:00:00",
            "text": "Never chase a gap over 3% at the open.",
        }
    ]
    text = render_playbook_markdown(entries)
    assert "Entry 1" in text
    assert "2026-09-21" in text
    assert "2026-10-21" in text
    assert "Never chase a gap over 3% at the open." in text


# -- pending_proposal_paths -----------------------------------------------------------------


def test_pending_proposal_paths_is_empty_when_proposals_dir_does_not_exist(config, tmp_path):
    cfg = _playbook_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    assert pending_proposal_paths(cfg, ledger) == []
    ledger.close()


def test_pending_proposal_paths_lists_unresolved_files(config, tmp_path):
    cfg = _playbook_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    _write_proposal(cfg, "2026-09-21", "proposal A")
    _write_proposal(cfg, "2026-09-22", "proposal B")

    paths = pending_proposal_paths(cfg, ledger)

    assert {p.name for p in paths} == {"2026-09-21.md", "2026-09-22.md"}
    ledger.close()


def test_pending_proposal_paths_excludes_already_decided_files(config, tmp_path):
    cfg = _playbook_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    _write_proposal(cfg, "2026-09-21", "proposal A")
    _write_proposal(cfg, "2026-09-22", "proposal B")
    reject_proposal(cfg, ledger, "2026-09-21", reason="not enough evidence")

    paths = pending_proposal_paths(cfg, ledger)

    assert {p.name for p in paths} == {"2026-09-22.md"}
    ledger.close()


# -- accept_proposal --------------------------------------------------------------------


def test_accept_proposal_raises_without_a_proposal_file(config, tmp_path):
    cfg = _playbook_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    with pytest.raises(FileNotFoundError):
        accept_proposal(cfg, ledger, "2026-09-21", bug_fix=True, now=NOW)
    ledger.close()


def test_accept_proposal_raises_without_meeting_the_evidence_bar(config, tmp_path):
    cfg = _playbook_config(config, tmp_path, evidence_min_trading_days=10, evidence_min_trades=20)
    ledger = _ledger(tmp_path)
    _write_proposal(cfg, "2026-09-21", "size cap tweak")

    with pytest.raises(PlaybookEvidenceError):
        accept_proposal(
            cfg, ledger, "2026-09-21", evidence_trading_days=5, evidence_trades=3, now=NOW
        )

    assert ledger.current_playbook_version() is None
    ledger.close()


def test_accept_proposal_bug_fix_bypasses_the_evidence_bar(config, tmp_path):
    cfg = _playbook_config(config, tmp_path, evidence_min_trading_days=10, evidence_min_trades=20)
    ledger = _ledger(tmp_path)
    _write_proposal(cfg, "2026-09-21", "fix a bug in the sizing formula")

    version = accept_proposal(cfg, ledger, "2026-09-21", bug_fix=True, now=NOW)

    assert version == 1
    ledger.close()


def test_accept_proposal_succeeds_when_evidence_meets_the_configured_minimum(config, tmp_path):
    cfg = _playbook_config(config, tmp_path, evidence_min_trading_days=10, evidence_min_trades=20)
    ledger = _ledger(tmp_path)
    _write_proposal(cfg, "2026-09-21", "size cap tweak")

    version = accept_proposal(cfg, ledger, "2026-09-21", evidence_trading_days=12, now=NOW)

    assert version == 1
    entries = ledger.active_playbook_entries()
    assert len(entries) == 1
    assert entries[0]["adopted_at"] == "2026-09-21 00:00:00"
    assert entries[0]["review_date"] == "2026-10-21 00:00:00"  # +30 default review_after_days
    ledger.close()


def test_accept_proposal_writes_playbook_md(config, tmp_path):
    cfg = _playbook_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    _write_proposal(cfg, "2026-09-21", "Never chase a gap over 3% at the open.")

    accept_proposal(cfg, ledger, "2026-09-21", bug_fix=True, now=NOW)

    playbook_text = cfg.path(cfg.intraday.playbook_path).read_text(encoding="utf-8")
    assert "Never chase a gap over 3% at the open." in playbook_text
    ledger.close()


def test_accept_proposal_twice_bumps_the_version_and_keeps_both_entries(config, tmp_path):
    cfg = _playbook_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    _write_proposal(cfg, "2026-09-21", "rule A")
    _write_proposal(cfg, "2026-09-28", "rule B")

    v1 = accept_proposal(cfg, ledger, "2026-09-21", bug_fix=True, now=NOW)
    v2 = accept_proposal(cfg, ledger, "2026-09-28", bug_fix=True, now=NOW)

    assert v2 > v1
    playbook_text = cfg.path(cfg.intraday.playbook_path).read_text(encoding="utf-8")
    assert "rule A" in playbook_text
    assert "rule B" in playbook_text
    ledger.close()


# -- reject_proposal ----------------------------------------------------------------------


def test_reject_proposal_raises_without_a_proposal_file(config, tmp_path):
    cfg = _playbook_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    with pytest.raises(FileNotFoundError):
        reject_proposal(cfg, ledger, "2026-09-21", reason="not enough evidence")
    ledger.close()


def test_reject_proposal_records_the_decision_without_touching_playbook_md(config, tmp_path):
    cfg = _playbook_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    _write_proposal(cfg, "2026-09-21", "size cap tweak")

    entry_id = reject_proposal(cfg, ledger, "2026-09-21", reason="not enough evidence")

    entry = ledger.get_playbook_entry(entry_id)
    assert entry["status"] == "rejected"
    assert entry["resolution_reason"] == "not enough evidence"
    assert ledger.current_playbook_version() is None
    assert not cfg.path(cfg.intraday.playbook_path).exists()
    ledger.close()


# -- retire_entry -------------------------------------------------------------------------


def test_retire_entry_raises_for_an_unknown_id(config, tmp_path):
    cfg = _playbook_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    with pytest.raises(ValueError, match="not currently active"):
        retire_entry(cfg, ledger, 999, reason="stopped holding", now=NOW)
    ledger.close()


def test_retire_entry_raises_for_an_already_rejected_id(config, tmp_path):
    cfg = _playbook_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    _write_proposal(cfg, "2026-09-21", "size cap tweak")
    entry_id = reject_proposal(cfg, ledger, "2026-09-21", reason="not enough evidence")

    with pytest.raises(ValueError, match="not currently active"):
        retire_entry(cfg, ledger, entry_id, reason="stopped holding", now=NOW)
    ledger.close()


def test_retire_entry_removes_it_from_the_rendered_playbook_and_bumps_the_version(config, tmp_path):
    cfg = _playbook_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    _write_proposal(cfg, "2026-09-21", "rule A")
    v1 = accept_proposal(cfg, ledger, "2026-09-21", bug_fix=True, now=NOW)
    entry_id = ledger.active_playbook_entries()[0]["id"]

    v2 = retire_entry(cfg, ledger, entry_id, reason="stopped holding", now=NOW)

    assert v2 > v1
    assert ledger.active_playbook_entries() == []
    playbook_text = cfg.path(cfg.intraday.playbook_path).read_text(encoding="utf-8")
    assert "rule A" not in playbook_text
    ledger.close()

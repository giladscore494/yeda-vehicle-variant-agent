"""Validation tests for canonical-first problem-queue state.

Guards against the "s1" pollution regression: an invalid token must
never appear in active needs_retry / problem_repair_state lists, must
not be counted toward total/pending/progress, and must only be visible
under ``batch_state.invalid_needs_retry_seed_ids`` (or be dropped
entirely).
"""
from __future__ import annotations

import json
from pathlib import Path

from agent.problem_queue import (
    build_problem_queue_payload,
    compute_problem_repair_state,
    regenerate_problem_queue,
    sanitize_problem_seed_lists,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PATH = REPO_ROOT / "data" / "canonical" / "resume_package_canonical.json"


def _valid_seed_ids() -> list[str]:
    return [
        "bmw__850i__2018__2026__il",
        "bmw__z4_sdrive20i__2019__2026__il",
        "honda__accord__1990__2024__il",
    ]


def _make_canonical(needs_retry, false_processed=None, invalid=None):
    return {
        "batch_state": {
            "needs_retry_seed_ids": list(needs_retry),
            "false_processed_seed_ids": list(false_processed or needs_retry),
            "invalid_needs_retry_seed_ids": list(invalid or []),
            "next_seed_id": "haval__h6__2022__2026__il",
            "last_completed_seed_id": "gmc__yukon__2000__2026__il",
            "original_false_processed_count": len(false_processed or needs_retry),
        }
    }


# ---------------------------------------------------------------------------
# Synthetic-canonical guards
# ---------------------------------------------------------------------------

def test_s1_never_in_active_needs_retry():
    seeds = _valid_seed_ids()
    polluted = ["s1"] + seeds
    canonical = _make_canonical(polluted, false_processed=seeds)
    sanitize_problem_seed_lists(canonical)
    bs = canonical["batch_state"]
    assert "s1" not in bs["needs_retry_seed_ids"]
    assert "s1" in bs["invalid_needs_retry_seed_ids"]
    assert bs["needs_retry_seed_ids"] == seeds


def test_s1_never_in_problem_repair_state_pending():
    seeds = _valid_seed_ids()
    polluted = ["s1"] + seeds
    canonical = _make_canonical(polluted, false_processed=seeds)
    sanitize_problem_seed_lists(canonical)
    prs = compute_problem_repair_state(canonical)
    assert "s1" not in prs["pending_seed_ids"]
    assert prs["total"] == len(seeds)
    assert prs["progress"]["pending"] == len(seeds)
    assert prs["current_seed_id"] == seeds[0]


def test_s1_not_counted_in_problem_queue_export():
    seeds = _valid_seed_ids()
    polluted = ["s1"] + seeds
    canonical = _make_canonical(polluted, false_processed=seeds)
    sanitize_problem_seed_lists(canonical)
    payload = build_problem_queue_payload(canonical)
    assert "s1" not in payload["pending_seed_ids"]
    assert payload["total"] == len(seeds)
    assert payload["pending"] == len(seeds)
    assert payload["first_pending"] == seeds[0]


def test_compute_strips_invalid_even_without_explicit_sanitize():
    """Even if a caller forgets to call sanitize, the derivation must
    refuse to count invalid tokens in the active progress numbers."""
    seeds = _valid_seed_ids()
    canonical = _make_canonical(["s1"] + seeds, false_processed=seeds)
    # Intentionally do NOT call sanitize_problem_seed_lists.
    prs = compute_problem_repair_state(canonical)
    assert "s1" not in prs["pending_seed_ids"]
    assert prs["progress"]["pending"] == len(seeds)
    assert prs["current_seed_id"] == seeds[0]


def test_needs_retry_outside_false_processed_is_quarantined():
    """Anything in needs_retry that isn't a member of false_processed is
    not a legitimate problem-queue seed and must be quarantined."""
    seeds = _valid_seed_ids()
    stray = "fiat__bravo__1995__2014__il"
    canonical = _make_canonical(seeds + [stray], false_processed=seeds)
    sanitize_problem_seed_lists(canonical)
    bs = canonical["batch_state"]
    assert stray not in bs["needs_retry_seed_ids"]
    assert stray in bs["invalid_needs_retry_seed_ids"]


# ---------------------------------------------------------------------------
# Real-canonical guard
# ---------------------------------------------------------------------------

def test_on_disk_canonical_is_clean():
    if not CANONICAL_PATH.exists():  # pragma: no cover - defensive
        return
    canonical = json.loads(CANONICAL_PATH.read_text(encoding="utf-8"))
    bs = canonical.get("batch_state") or {}
    prs = canonical.get("problem_repair_state") or {}
    assert "s1" not in (bs.get("needs_retry_seed_ids") or [])
    assert "s1" not in (prs.get("pending_seed_ids") or [])
    assert "s1" not in (prs.get("completed_seed_ids") or [])
    assert "s1" not in (prs.get("failed_retry_seed_ids") or [])
    # Counts must agree.
    needs = bs.get("needs_retry_seed_ids") or []
    fp = bs.get("false_processed_seed_ids") or []
    assert len(needs) == len(fp), (len(needs), len(fp))
    assert prs.get("total") == len(needs)
    assert prs.get("progress", {}).get("pending") == len(needs)
    if needs:
        assert prs.get("current_seed_id") == needs[0]


def test_on_disk_canonical_expected_counts_pre_bmw():
    """The uploaded canonical is the pre-BMW state: total = pending = 54,
    current = bmw__850i__2018__2026__il."""
    if not CANONICAL_PATH.exists():  # pragma: no cover - defensive
        return
    canonical = json.loads(CANONICAL_PATH.read_text(encoding="utf-8"))
    bs = canonical["batch_state"]
    prs = canonical["problem_repair_state"]
    assert len(bs["needs_retry_seed_ids"]) == 54
    assert len(bs["false_processed_seed_ids"]) == 54
    assert prs["total"] == 54
    assert prs["progress"]["pending"] == 54
    assert prs["progress"]["completed"] == 0
    assert prs["current_seed_id"] == "bmw__850i__2018__2026__il"
    assert prs["normal_continuation"]["next_seed_id"] == "haval__h6__2022__2026__il"


# ---------------------------------------------------------------------------
# Re-derivation idempotence
# ---------------------------------------------------------------------------

def test_regenerate_does_not_resurrect_invalid_tokens(tmp_path, monkeypatch):
    """Regenerating problem_queue.json from a polluted canonical must
    not reintroduce ``s1`` into the active pending list."""
    seeds = _valid_seed_ids()
    canonical = _make_canonical(["s1"] + seeds, false_processed=seeds)
    sanitize_problem_seed_lists(canonical)
    out = regenerate_problem_queue(canonical, delete_if_complete=False)
    assert "s1" not in out["pending_seed_ids"]
    assert out["pending"] == len(seeds)
    assert out["first_pending"] == seeds[0]

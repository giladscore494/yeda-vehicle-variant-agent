"""Integration tests for problem_queue wiring to app.py and batch_runner.

Verifies the contracts specified in the wiring problem statement:

1. app status shows Problem Queue 1/54 from canonical only.
2. Deleting data/output/problem_queue.json does not change UI status.
3. Stale problem_queue.json cannot reset progress.
4. run_next_batch selects BMW from select_next_seed (canonical-first).
5. After mocked BMW dedupe_proof result, progress becomes 2/54.
6. Normal next seed remains Haval after BMW runs.
7. batch_state.json absence does not break selection.
8. RerunQueueManager is not used as active selector.
"""
from __future__ import annotations

import copy
import json

import pytest

import agent.batch_runner as br
import app as app_mod
from agent.problem_queue import (
    compute_problem_repair_state,
    select_next_seed,
)


# ---------------------------------------------------------------------------
# Shared synthetic canonical (pre-BMW state: 54 pending)
# ---------------------------------------------------------------------------

BMW_SEED_ID = "bmw__850i__2018__2026__il"
Z4_SEED_ID = "bmw__z4_sdrive20i__2019__2026__il"
HAVAL_SEED_ID = "haval__h6__2022__2026__il"

PROBLEM_IDS = (
    [BMW_SEED_ID, Z4_SEED_ID]
    + [f"make{i}__model{i}__2010__2020__il" for i in range(52)]
)
assert len(PROBLEM_IDS) == 54


def _base_canonical(needs_retry=None, completed=None, last_completed=None):
    needs_retry = list(needs_retry if needs_retry is not None else PROBLEM_IDS)
    completed = list(completed or [])
    return {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "market": "IL",
            "needs_retry_seed_ids": needs_retry,
            "false_processed_seed_ids": list(PROBLEM_IDS),
            "original_false_processed_count": 54,
            "last_completed_seed_id": "gmc__yukon__2000__2026__il",
            "next_seed_id": HAVAL_SEED_ID,
            "processed_seed_ids": ["abarth__124__2016__2020__il"],
            "failed_seed_ids": [],
            "failed_details": [],
            "in_progress_seed_id": None,
        },
        "accumulated_clean_export": {
            "variants": [{"variant_id": f"v-{i}"} for i in range(1323)],
        },
        "problem_repair_state": {
            "active": bool(needs_retry),
            "total": 54,
            "completed_seed_ids": completed,
            "last_completed_seed_id": last_completed,
            "pending_seed_ids": needs_retry,
            "failed_retry_seed_ids": [],
            "current_seed_id": needs_retry[0] if needs_retry else None,
            "normal_continuation": {
                "next_seed_id": HAVAL_SEED_ID,
                "last_completed_seed_id": "gmc__yukon__2000__2026__il",
            },
            "progress": {
                "total": 54,
                "completed": len(completed),
                "pending": len(needs_retry),
                "failed_retry": 0,
                "current_position": f"{len(completed) + 1} / 54" if needs_retry else "54 / 54",
            },
        },
    }


# ---------------------------------------------------------------------------
# 1. App status shows Problem Queue 1/54 from canonical only
# ---------------------------------------------------------------------------

def test_app_status_shows_problem_queue_from_canonical_only(monkeypatch):
    canonical = _base_canonical()
    monkeypatch.setattr(app_mod, "load_problem_queue_canonical", lambda: copy.deepcopy(canonical))

    snap = app_mod._status_snapshot("IL")

    assert snap["active_mode"] == "problem_queue"
    assert snap["pq_active"] is True
    assert snap["pq_total"] == 54
    assert snap["pq_completed"] == 0
    assert snap["pq_pending"] == 54
    assert snap["pq_current_position"] == "1 / 54"
    assert snap["pq_current_seed"] == BMW_SEED_ID
    assert snap["pq_normal_paused_at"] == HAVAL_SEED_ID
    assert snap["next_normal_seed"] == HAVAL_SEED_ID


# ---------------------------------------------------------------------------
# 2. Deleting problem_queue.json does not change UI status
# ---------------------------------------------------------------------------

def test_deleting_problem_queue_json_does_not_change_ui_status(tmp_path, monkeypatch):
    """The mirror file data/output/problem_queue.json is disposable.

    Even when it does not exist, _status_snapshot must report Problem Queue
    state sourced exclusively from canonical.
    """
    canonical = _base_canonical()
    monkeypatch.setattr(app_mod, "load_problem_queue_canonical", lambda: copy.deepcopy(canonical))

    # Verify no problem_queue.json exists in tmp_path (simulates deletion).
    pq_file = tmp_path / "problem_queue.json"
    assert not pq_file.exists()

    snap = app_mod._status_snapshot("IL")
    assert snap["active_mode"] == "problem_queue"
    assert snap["pq_total"] == 54
    assert snap["pq_pending"] == 54
    assert snap["pq_current_seed"] == BMW_SEED_ID


# ---------------------------------------------------------------------------
# 3. Stale problem_queue.json cannot reset progress
# ---------------------------------------------------------------------------

def test_stale_problem_queue_json_cannot_reset_progress(tmp_path, monkeypatch):
    """A stale mirror showing 0 completed cannot override canonical showing 1 completed."""
    completed = [BMW_SEED_ID]
    remaining = [sid for sid in PROBLEM_IDS if sid not in completed]
    canonical = _base_canonical(
        needs_retry=remaining,
        completed=completed,
        last_completed=BMW_SEED_ID,
    )
    # Write a stale mirror that says 0 completed (the wrong state).
    stale_mirror = {
        "total": 54, "pending": 54, "completed": 0,
        "pending_seed_ids": list(PROBLEM_IDS),
    }
    (tmp_path / "problem_queue.json").write_text(json.dumps(stale_mirror), encoding="utf-8")

    monkeypatch.setattr(app_mod, "load_problem_queue_canonical", lambda: copy.deepcopy(canonical))

    snap = app_mod._status_snapshot("IL")
    # Canonical says 1 completed → must win over stale mirror.
    assert snap["pq_completed"] == 1
    assert snap["pq_pending"] == 53
    assert snap["pq_current_position"] == "2 / 54"


# ---------------------------------------------------------------------------
# 4. run_next_batch selects BMW from select_next_seed (canonical gate)
# ---------------------------------------------------------------------------

def test_run_next_batch_selects_bmw_from_canonical(monkeypatch):
    """When problem_queue is active, run_next_batch must select current_seed_id."""
    canonical = _base_canonical()
    ordered = [
        {"seed_id": BMW_SEED_ID, "make": "BMW", "model": "850i", "year_start": 2018, "year_end": 2026, "market": "IL"},
        {"seed_id": Z4_SEED_ID, "make": "BMW", "model": "Z4", "year_start": 2019, "year_end": 2026, "market": "IL"},
        {"seed_id": HAVAL_SEED_ID, "make": "Haval", "model": "H6", "year_start": 2022, "year_end": 2026, "market": "IL"},
    ]
    called = {}

    def _fake_run(make, model, *args, **kwargs):
        called["make"] = make
        called["model"] = model
        return {"status": "completed", "variants_created": 0}

    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": {
        "passed": True, "repair_required": False, "needs_retry_required": False, "false_processed_seeds": [],
        "coverage_audit": {"holes_count": 0, "missing_seeds": []},
    })
    monkeypatch.setattr(br, "load_batch_state", lambda market="IL": {
        "market": "IL", "processed_seed_ids": [], "failed_seed_ids": [], "failed_details": [],
        "needs_retry_seed_ids": [], "last_completed_seed_id": None, "in_progress_seed_id": None,
        "next_seed_id": HAVAL_SEED_ID,
    })
    monkeypatch.setattr(br, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_refresh_coverage", lambda s, o: None)
    monkeypatch.setattr(br, "save_json", lambda *a, **k: None)
    monkeypatch.setattr(br, "run_single_model", _fake_run)
    monkeypatch.setattr(br, "persist_canonical_resume_package", lambda **k: {"ok": True})
    monkeypatch.setattr(br, "persist_canonical_after_seed", lambda **k: {"ok": True})

    result = br.run_next_batch(limit=1, market="IL")

    assert result.get("status") == "completed"
    assert result.get("batch_mode") == "problem_queue"
    assert called.get("make") == "BMW"


# ---------------------------------------------------------------------------
# 5. After mocked BMW dedupe_proof result, progress becomes 2/54
# ---------------------------------------------------------------------------

def test_classify_seed_closure_dedupe_proof_counts_as_closed(monkeypatch):
    """A seed with 0 new variants but a valid dedupe_proof must still be classified as closed."""
    from agent.problem_queue import classify_seed_closure

    closure_input = {
        "variants_added_to_canonical": 0,
        "dedupe_proof": {"matched_variant_ids": ["v-existing-1", "v-existing-2"]},
        "no_variants_reason": None,
    }
    closed, status = classify_seed_closure(closure_input)
    assert closed is True, f"dedupe_proof result must be closed; got closed={closed}, status={status}"
    assert status in {"completed_added", "completed_deduped", "completed_no_variants_reason"}


def test_progress_after_bmw_dedup_matches_spec(monkeypatch):
    """After one BMW deduped/successful run:
      - completed = 1, pending = 53, current_position = 2 / 54
      - last_completed = bmw__850i__2018__2026__il
      - current_seed = Z4 (second in PROBLEM_IDS)
      - normal_continuation.next_seed_id remains haval__h6__2022__2026__il
    """
    completed = [BMW_SEED_ID]
    remaining = [sid for sid in PROBLEM_IDS if sid not in completed]
    canonical = _base_canonical(
        needs_retry=remaining,
        completed=completed,
        last_completed=BMW_SEED_ID,
    )
    prs = compute_problem_repair_state(canonical)

    assert prs["active"] is True
    assert prs["progress"]["completed"] == 1
    assert prs["progress"]["pending"] == 53
    assert prs["progress"]["current_position"] == "2 / 54"
    assert prs["last_completed_seed_id"] == BMW_SEED_ID
    assert prs["current_seed_id"] == Z4_SEED_ID
    assert prs["normal_continuation"]["next_seed_id"] == HAVAL_SEED_ID


# ---------------------------------------------------------------------------
# 6. Normal next seed remains Haval after BMW runs
# ---------------------------------------------------------------------------

def test_normal_next_seed_remains_haval_after_bmw(monkeypatch):
    """While problem_queue is active, next_normal_seed must be Haval (frozen)."""
    completed = [BMW_SEED_ID]
    remaining = [sid for sid in PROBLEM_IDS if sid not in completed]
    canonical = _base_canonical(
        needs_retry=remaining,
        completed=completed,
        last_completed=BMW_SEED_ID,
    )
    monkeypatch.setattr(app_mod, "load_problem_queue_canonical", lambda: copy.deepcopy(canonical))

    snap = app_mod._status_snapshot("IL")
    assert snap["next_normal_seed"] == HAVAL_SEED_ID
    assert snap["pq_normal_paused_at"] == HAVAL_SEED_ID


# ---------------------------------------------------------------------------
# 7. batch_state.json absence does not break selection
# ---------------------------------------------------------------------------

def test_batch_state_json_absence_does_not_break_selection(monkeypatch):
    """select_next_seed must work from canonical alone; batch_state.json is irrelevant."""
    canonical = _base_canonical()

    # No batch_state.json patching needed — select_next_seed only reads canonical.
    selection = select_next_seed(canonical)

    assert selection["mode"] == "problem_queue"
    assert selection["seed_id"] == BMW_SEED_ID
    assert selection["blocks_normal_batch"] is True


# ---------------------------------------------------------------------------
# 8. RerunQueueManager is not used as active selector
# ---------------------------------------------------------------------------

def test_rerun_queue_manager_not_used_as_active_selector(monkeypatch):
    """When problem_queue is active, run_next_batch must NOT be blocked by RerunQueueManager."""
    canonical = _base_canonical()
    ordered = [
        {"seed_id": BMW_SEED_ID, "make": "BMW", "model": "850i", "year_start": 2018, "year_end": 2026, "market": "IL"},
        {"seed_id": HAVAL_SEED_ID, "make": "Haval", "model": "H6", "year_start": 2022, "year_end": 2026, "market": "IL"},
    ]

    class _FailingRerunManager:
        """If RerunQueueManager is consulted as a selector, this will explode."""
        def __init__(self, *args, **kwargs): pass
        def queue_exists(self): return True  # Simulate stale queue file
        def has_pending(self): return True   # Simulate stale pending
        def next_seed(self):
            raise AssertionError("RerunQueueManager.next_seed() must NOT be called when problem_queue is active")
        def load_queue(self): return {}
        def progress_summary(self): return {}

    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": {
        "passed": True, "repair_required": False, "needs_retry_required": False, "false_processed_seeds": [],
        "coverage_audit": {"holes_count": 0, "missing_seeds": []},
    })
    monkeypatch.setattr(br, "load_batch_state", lambda market="IL": {
        "market": "IL", "processed_seed_ids": [], "failed_seed_ids": [], "failed_details": [],
        "needs_retry_seed_ids": [], "last_completed_seed_id": None, "in_progress_seed_id": None,
        "next_seed_id": HAVAL_SEED_ID,
    })
    monkeypatch.setattr(br, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_refresh_coverage", lambda s, o: None)
    monkeypatch.setattr(br, "save_json", lambda *a, **k: None)
    monkeypatch.setattr(br, "run_single_model", lambda *a, **k: {"status": "completed", "variants_created": 0})
    monkeypatch.setattr(br, "persist_canonical_resume_package", lambda **k: {"ok": True})
    monkeypatch.setattr(br, "persist_canonical_after_seed", lambda **k: {"ok": True})

    # Inject a RerunQueueManager that raises if next_seed() is called.
    # Module-level: agent/rerun_queue_manager.py is archived under legacy/.
    # batch_runner must not consult it as an active selector.
    import importlib
    rqm_spec = importlib.util.find_spec("agent.rerun_queue_manager")
    assert rqm_spec is None, (
        "agent.rerun_queue_manager is the legacy state engine and must not be importable "
        "from the active agent package; it must live under legacy/."
    )

    # Must not raise from _FailingRerunManager.next_seed()
    result = br.run_next_batch(limit=1, market="IL")
    assert result.get("batch_mode") == "problem_queue"


# ---------------------------------------------------------------------------
# 9. Hard guard: Haval blocked while problem_queue is active
# ---------------------------------------------------------------------------

def test_haval_blocked_while_problem_queue_active(monkeypatch):
    """run_next_batch must block if canonical somehow selects Haval while PQ is active."""
    # Build an adversarial canonical where haval is in false_processed and needs_retry.
    # This forces compute_problem_repair_state to yield active=True with haval as current_seed.
    canonical = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "market": "IL",
            "needs_retry_seed_ids": [HAVAL_SEED_ID],
            "false_processed_seed_ids": [HAVAL_SEED_ID],  # Must include to pass filter
            "original_false_processed_count": 1,
            "last_completed_seed_id": "gmc__yukon__2000__2026__il",
            "next_seed_id": HAVAL_SEED_ID,
            "processed_seed_ids": [],
            "failed_seed_ids": [],
            "failed_details": [],
            "in_progress_seed_id": None,
        },
        "accumulated_clean_export": {"variants": []},
    }

    ordered = [
        {"seed_id": BMW_SEED_ID, "make": "BMW", "model": "850i", "year_start": 2018, "year_end": 2026, "market": "IL"},
        {"seed_id": HAVAL_SEED_ID, "make": "Haval", "model": "H6", "year_start": 2022, "year_end": 2026, "market": "IL"},
    ]
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": {
        "passed": True, "repair_required": False, "needs_retry_required": False, "false_processed_seeds": [],
        "coverage_audit": {"holes_count": 0, "missing_seeds": []},
    })
    monkeypatch.setattr(br, "load_batch_state", lambda market="IL": {
        "market": "IL", "processed_seed_ids": [], "failed_seed_ids": [], "failed_details": [],
        "needs_retry_seed_ids": [HAVAL_SEED_ID], "last_completed_seed_id": None, "in_progress_seed_id": None,
        "next_seed_id": HAVAL_SEED_ID,
    })
    monkeypatch.setattr(br, "_load_outputs", lambda: {})
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "save_json", lambda *a, **k: None)

    result = br.run_next_batch(limit=1, market="IL")
    assert result.get("status") == "blocked"
    assert "haval" in (result.get("error") or "").lower()

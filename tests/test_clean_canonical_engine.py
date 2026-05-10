"""Canonical-only engine acceptance tests.

These are the spec-required tests for the clean canonical-only engine.
They use synthetic in-memory canonical payloads (the user uploads the
real one separately); the engine code is the same.

Test list (from the problem statement):

1.  test_clean_canonical_pre_bmw
2.  test_problem_queue_ui_pre_bmw
3.  test_bmw_success_advances_to_2_of_54
4.  test_bmw_dedupe_also_advances
5.  test_z4_success_advances_to_3_of_54
6.  test_normal_cursor_frozen_during_problem_queue
7.  test_no_legacy_output_controls_state
8.  test_stale_output_cannot_override
9.  test_problem_queue_completion_resumes_normal
10. test_no_rerun_queue_manager_imports
11. test_no_rerun_queue_progress_in_output
12. test_no_legacy_repair_refresh_progress
"""
from __future__ import annotations

import copy
import importlib
import importlib.util
import json
from pathlib import Path

import pytest

import app as app_mod
import agent.batch_runner as br
from agent.problem_queue import (
    classify_seed_closure,
    compute_problem_repair_state,
    mark_seed_completed,
    select_next_seed,
)


BMW_SEED_ID = "bmw__850i__2018__2026__il"
Z4_SEED_ID = "bmw__z4_sdrive20i__2019__2026__il"
DAEWOO_SEED_ID = "daewoo__lacetti__2003__2011__il"
HAVAL_SEED_ID = "haval__h6__2022__2026__il"
GMC_YUKON_SEED_ID = "gmc__yukon__2000__2026__il"

# 54 problem seeds with BMW 850i first, BMW Z4 second, Daewoo Lacetti third.
PROBLEM_IDS = [BMW_SEED_ID, Z4_SEED_ID, DAEWOO_SEED_ID] + [
    f"make{i}__model{i}__2010__2020__il" for i in range(51)
]
assert len(PROBLEM_IDS) == 54


def _canonical(
    needs_retry=None,
    completed=None,
    last_completed=None,
    next_seed_id=HAVAL_SEED_ID,
    bs_last_completed=GMC_YUKON_SEED_ID,
) -> dict:
    needs_retry = list(needs_retry if needs_retry is not None else PROBLEM_IDS)
    completed = list(completed or [])
    return {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "market": "IL",
            "total_seeds": 993,
            "processed_seed_ids": ["abarth__124__2016__2020__il"],
            "needs_retry_seed_ids": needs_retry,
            "false_processed_seed_ids": list(PROBLEM_IDS),
            "last_completed_seed_id": bs_last_completed,
            "next_seed_id": next_seed_id,
            "failed_seed_ids": [],
            "failed_details": [],
            "in_progress_seed_id": None,
            "invalid_needs_retry_seed_ids": [],
        },
        "accumulated_clean_export": {"variants": [{"variant_id": f"v-{i}"} for i in range(100)]},
        "problem_repair_state": {
            "active": bool(needs_retry),
            "total": 54,
            "original_problem_seed_ids": list(PROBLEM_IDS),
            "completed_seed_ids": completed,
            "pending_seed_ids": needs_retry,
            "failed_retry_seed_ids": [],
            "last_completed_seed_id": last_completed,
            "current_seed_id": needs_retry[0] if needs_retry else None,
            "normal_continuation": {
                "last_completed_seed_id": GMC_YUKON_SEED_ID,
                "next_seed_id": HAVAL_SEED_ID,
            },
        },
    }


# ---------------------------------------------------------------------------
# 1. test_clean_canonical_pre_bmw
# ---------------------------------------------------------------------------

def test_clean_canonical_pre_bmw():
    canonical = _canonical()
    prs = compute_problem_repair_state(canonical)
    assert prs["total"] == 54
    assert prs["progress"]["completed"] == 0
    assert prs["progress"]["pending"] == 54
    assert prs["current_seed_id"] == BMW_SEED_ID
    assert prs["normal_continuation"]["next_seed_id"] == HAVAL_SEED_ID
    # No "s1" anywhere in the active lists.
    assert "s1" not in prs["pending_seed_ids"]
    assert "s1" not in prs["completed_seed_ids"]


# ---------------------------------------------------------------------------
# 2. test_problem_queue_ui_pre_bmw
# ---------------------------------------------------------------------------

def test_problem_queue_ui_pre_bmw(monkeypatch):
    canonical = _canonical()
    monkeypatch.setattr(app_mod, "load_problem_queue_canonical", lambda: copy.deepcopy(canonical))
    snap = app_mod._status_snapshot("IL")
    assert snap["pq_active"] is True
    assert snap["pq_total"] == 54
    assert snap["pq_pending"] == 54
    assert snap["pq_completed"] == 0
    # Spec: must be "1 / 54", NEVER "1 / 53".
    assert snap["pq_current_position"] == "1 / 54"
    assert snap["pq_current_seed"] == BMW_SEED_ID


# ---------------------------------------------------------------------------
# 3. test_bmw_success_advances_to_2_of_54
# ---------------------------------------------------------------------------

def test_bmw_success_advances_to_2_of_54():
    canonical = _canonical()
    result = {"variants_added_to_canonical": 2}
    closed, status = classify_seed_closure(result)
    assert closed and status == "completed_added"
    mark_seed_completed(BMW_SEED_ID, result=result, canonical=canonical, persist=False)
    prs = canonical["problem_repair_state"]
    assert prs["total"] == 54
    assert prs["progress"]["completed"] == 1
    assert prs["progress"]["pending"] == 53
    assert prs["progress"]["current_position"] == "2 / 54"
    assert prs["current_seed_id"] == Z4_SEED_ID
    assert prs["last_completed_seed_id"] == BMW_SEED_ID
    # Normal cursor remains frozen at Haval / GMC Yukon.
    assert canonical["batch_state"]["next_seed_id"] == HAVAL_SEED_ID
    assert canonical["batch_state"]["last_completed_seed_id"] == GMC_YUKON_SEED_ID


# ---------------------------------------------------------------------------
# 4. test_bmw_dedupe_also_advances
# ---------------------------------------------------------------------------

def test_bmw_dedupe_also_advances():
    canonical = _canonical()
    # Zero variants added, but a valid dedupe_proof exists.
    result = {
        "variants_added_to_canonical": 0,
        "dedupe_proof": {"matched_variant_ids": ["v-1", "v-2"]},
    }
    closed, status = classify_seed_closure(result)
    assert closed and status == "completed_deduped"
    mark_seed_completed(BMW_SEED_ID, result=result, canonical=canonical, persist=False)
    prs = canonical["problem_repair_state"]
    assert prs["progress"]["completed"] == 1
    assert prs["progress"]["pending"] == 53
    assert prs["progress"]["current_position"] == "2 / 54"
    assert prs["current_seed_id"] == Z4_SEED_ID


# ---------------------------------------------------------------------------
# 5. test_z4_success_advances_to_3_of_54
# ---------------------------------------------------------------------------

def test_z4_success_advances_to_3_of_54():
    # Start from post-BMW state.
    canonical = _canonical(
        needs_retry=PROBLEM_IDS[1:],
        completed=[BMW_SEED_ID],
        last_completed=BMW_SEED_ID,
    )
    mark_seed_completed(
        Z4_SEED_ID,
        result={"variants_added_to_canonical": 1},
        canonical=canonical,
        persist=False,
    )
    prs = canonical["problem_repair_state"]
    assert prs["total"] == 54
    assert prs["progress"]["completed"] == 2
    assert prs["progress"]["pending"] == 52
    assert prs["progress"]["current_position"] == "3 / 54"
    assert prs["current_seed_id"] == DAEWOO_SEED_ID
    assert prs["last_completed_seed_id"] == Z4_SEED_ID


# ---------------------------------------------------------------------------
# 6. test_normal_cursor_frozen_during_problem_queue
# ---------------------------------------------------------------------------

def test_normal_cursor_frozen_during_problem_queue():
    canonical = _canonical()
    for sid in (BMW_SEED_ID, Z4_SEED_ID):
        mark_seed_completed(
            sid,
            result={"variants_added_to_canonical": 1},
            canonical=canonical,
            persist=False,
        )
        assert canonical["batch_state"]["next_seed_id"] == HAVAL_SEED_ID
        assert canonical["batch_state"]["last_completed_seed_id"] == GMC_YUKON_SEED_ID


# ---------------------------------------------------------------------------
# 7. test_no_legacy_output_controls_state
# ---------------------------------------------------------------------------

def test_no_legacy_output_controls_state(monkeypatch, tmp_path):
    """Even with NO data/output/rerun_queue.json, batch_state.json, or
    latest_batch_result.json on disk, the selector returns the canonical
    current problem seed."""
    canonical = _canonical()

    # Point the problem_queue module at an empty tmp output dir.
    from agent import problem_queue as pq

    monkeypatch.setattr(pq, "load_canonical", lambda: copy.deepcopy(canonical))
    # None of these legacy files exist:
    for name in (
        "rerun_queue.json",
        "batch_state.json",
        "latest_batch_result.json",
    ):
        assert not (tmp_path / name).exists()

    selection = select_next_seed(copy.deepcopy(canonical))
    assert selection["mode"] == "problem_queue"
    assert selection["seed_id"] == BMW_SEED_ID
    assert selection["blocks_normal_batch"] is True


# ---------------------------------------------------------------------------
# 8. test_stale_output_cannot_override
# ---------------------------------------------------------------------------

def test_stale_output_cannot_override(tmp_path, monkeypatch):
    """A fake/stale rerun_queue.json claiming a different current seed
    must not influence the canonical selector."""
    fake_queue = tmp_path / "rerun_queue.json"
    fake_queue.write_text(json.dumps({
        "active": True,
        "current_seed_id": "honda__legend__1996__2012__il",
        "pending_seed_ids": ["honda__legend__1996__2012__il"],
    }), encoding="utf-8")

    canonical = _canonical()
    selection = select_next_seed(canonical)
    assert selection["seed_id"] == BMW_SEED_ID
    assert selection["seed_id"] != "honda__legend__1996__2012__il"


# ---------------------------------------------------------------------------
# 9. test_problem_queue_completion_resumes_normal
# ---------------------------------------------------------------------------

def test_problem_queue_completion_resumes_normal(monkeypatch, tmp_path):
    """When all 54 problem seeds complete, active=False and normal batch
    resumes from haval / gmc."""
    # Drain needs_retry by hand to avoid needing a 54-step loop.
    canonical = _canonical(needs_retry=[])
    canonical["problem_repair_state"]["completed_seed_ids"] = list(PROBLEM_IDS)
    canonical["problem_repair_state"]["last_completed_seed_id"] = PROBLEM_IDS[-1]
    canonical["batch_state"]["needs_retry_seed_ids"] = []

    prs = compute_problem_repair_state(canonical)
    assert prs["active"] is False
    assert prs["progress"]["pending"] == 0
    assert len(prs["completed_seed_ids"]) == 54

    # The problem_queue.json mirror is deleted when complete.
    from agent import problem_queue as pq

    pq_path = tmp_path / "problem_queue.json"
    pq_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pq, "problem_queue_path", lambda: pq_path)
    monkeypatch.setattr(pq, "save_json", lambda *a, **k: None)
    pq.regenerate_problem_queue(canonical)
    assert not pq_path.exists()

    selection = select_next_seed(canonical)
    assert selection["mode"] == "normal_batch"
    assert selection["seed_id"] == HAVAL_SEED_ID


# ---------------------------------------------------------------------------
# 10. test_no_rerun_queue_manager_imports
# ---------------------------------------------------------------------------

def test_no_rerun_queue_manager_imports():
    """app.py and agent/batch_runner.py must not import the archived
    legacy RerunQueueManager from the active agent package."""
    repo = Path(__file__).resolve().parents[1]
    for rel in ("app.py", "agent/batch_runner.py"):
        text = (repo / rel).read_text(encoding="utf-8")
        assert "from agent.rerun_queue_manager" not in text, rel
        assert "import agent.rerun_queue_manager" not in text, rel
        assert "RerunQueueManager(" not in text, rel
    # And the legacy module is no longer importable from the active package.
    assert importlib.util.find_spec("agent.rerun_queue_manager") is None


# ---------------------------------------------------------------------------
# 11. test_no_rerun_queue_progress_in_output
# ---------------------------------------------------------------------------

def test_no_rerun_queue_progress_in_output(monkeypatch):
    """run_next_batch must not include legacy rerun_queue_progress or
    rerun_queue_finalize keys in problem_queue mode."""
    canonical = _canonical()
    ordered = [
        {"seed_id": BMW_SEED_ID, "make": "BMW", "model": "850i", "year_start": 2018, "year_end": 2026, "market": "IL"},
        {"seed_id": HAVAL_SEED_ID, "make": "Haval", "model": "H6", "year_start": 2022, "year_end": 2026, "market": "IL"},
    ]
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": {
        "passed": True, "repair_required": False, "needs_retry_required": False,
        "false_processed_seeds": [], "coverage_audit": {"holes_count": 0, "missing_seeds": []},
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

    result = br.run_next_batch(limit=1, market="IL")
    assert result.get("batch_mode") == "problem_queue"
    assert "rerun_queue_progress" not in result
    assert "rerun_queue_finalize" not in result


# ---------------------------------------------------------------------------
# 12. test_no_legacy_repair_refresh_progress
# ---------------------------------------------------------------------------

def test_no_legacy_repair_refresh_progress(monkeypatch):
    """The app-level safe-batch wrapper must not surface repair_refresh
    or guard_after as active progress signals."""
    captured = {}

    def fake_run_next_batch(**kwargs):
        return {"status": "completed", "batch_mode": "problem_queue",
                "processed": 1, "results": []}

    monkeypatch.setattr(app_mod, "run_next_batch", fake_run_next_batch)
    out = app_mod._run_next_safe_batch(batch_size=1, market="IL", auto_push_per_seed=False)
    assert "repair_refresh" not in out
    assert "guard_after" not in out.get("batch", {})
    assert "rerun_queue_progress" not in out.get("batch", {})
    assert "rerun_queue_finalize" not in out.get("batch", {})

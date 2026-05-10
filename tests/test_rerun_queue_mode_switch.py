"""Tests for the explicit RERUN_QUEUE / NORMAL_BATCH mode-switch FSM.

These tests cover the behavior added to support the user-described
scenario where a missing rerun_queue.json must be auto-created from
canonical needs_retry seeds, and rerun-mode processing must never
advance the normal continuation cursor (``last_completed_seed_id`` /
``next_seed_id``) — only the rerun queue tracks rerun progress.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import agent.batch_runner as br
from agent.rerun_queue_manager import (
    DEFAULT_NORMAL_CONTINUATION,
    RerunQueueManager,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_canonical(tmp_path, monkeypatch):
    """Build a canonical resembling the user's real scenario.

    Ordered seeds: alpha (0..3), bmw__850i (4), more (5..7), gmc__yukon (8),
    haval__h6 (9).  ``bmw__850i`` and one other rerun seed are listed in
    ``needs_retry_seed_ids`` but NOT in ``processed_seed_ids`` (they were
    previously stripped from processed because of zero variants).
    """
    canonical_path = tmp_path / "resume_package_canonical.json"
    queue_path = tmp_path / "rerun_queue.json"

    ordered = [
        {"seed_id": "abarth__124__2016__2020__il", "make": "Abarth", "model": "124", "year_start": 2016, "year_end": 2020, "market": "IL"},
        {"seed_id": "audi__a1__2010__2018__il", "make": "Audi", "model": "A1", "year_start": 2010, "year_end": 2018, "market": "IL"},
        {"seed_id": "audi__a3__2003__2020__il", "make": "Audi", "model": "A3", "year_start": 2003, "year_end": 2020, "market": "IL"},
        {"seed_id": "audi__a4__1994__2026__il", "make": "Audi", "model": "A4", "year_start": 1994, "year_end": 2026, "market": "IL"},
        {"seed_id": "bmw__850i__2018__2026__il", "make": "BMW", "model": "850i", "year_start": 2018, "year_end": 2026, "market": "IL"},
        {"seed_id": "bmw__m3__2014__2026__il", "make": "BMW", "model": "M3", "year_start": 2014, "year_end": 2026, "market": "IL"},
        {"seed_id": "bmw__z4__2019__2026__il", "make": "BMW", "model": "Z4", "year_start": 2019, "year_end": 2026, "market": "IL"},
        {"seed_id": "ford__focus__2000__2020__il", "make": "Ford", "model": "Focus", "year_start": 2000, "year_end": 2020, "market": "IL"},
        {"seed_id": "gmc__yukon__2000__2026__il", "make": "GMC", "model": "Yukon", "year_start": 2000, "year_end": 2026, "market": "IL"},
        {"seed_id": "haval__h6__2022__2026__il", "make": "Haval", "model": "H6", "year_start": 2022, "year_end": 2026, "market": "IL"},
    ]
    # processed contains everything up to gmc__yukon EXCEPT the two rerun
    # seeds (bmw__850i, bmw__z4) which are out of processed but listed as
    # needs_retry.  Haval is NOT processed (it is the normal next seed).
    rerun_ids = {"bmw__850i__2018__2026__il", "bmw__z4__2019__2026__il"}
    processed_ids = [s["seed_id"] for s in ordered[:-1] if s["seed_id"] not in rerun_ids]
    yukon_variant = {
        "seed_id": "gmc__yukon__2000__2026__il",
        "make": "GMC", "model": "Yukon", "market": "IL",
        "year_start": 2000, "year_end": 2026,
        "variant_id": "v-yukon-1",
    }
    canonical = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "schema_version": "batch_state_v1",
            "market": "IL",
            "total_seeds": len(ordered),
            "processed_seed_ids": processed_ids,
            "processed_seeds": [{"seed_id": sid} for sid in processed_ids],
            "needs_retry_seed_ids": sorted(rerun_ids),
            "last_completed_seed_id": "gmc__yukon__2000__2026__il",
            "next_seed_id": "haval__h6__2022__2026__il",
            "failed_seed_ids": [],
            "failed_details": [],
            "in_progress_seed_id": None,
        },
        "accumulated_clean_export": {"variants": [yukon_variant]},
    }
    canonical_path.write_text(json.dumps(canonical), encoding="utf-8")

    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": ordered)

    return canonical_path, queue_path, ordered, sorted(rerun_ids)


# ---------------------------------------------------------------------------
# 1. Auto-creation from canonical needs_retry
# ---------------------------------------------------------------------------

def test_ensure_queue_exists_from_canonical_creates_queue(fake_canonical):
    canonical_path, queue_path, ordered, rerun_ids = fake_canonical
    mgr = RerunQueueManager(canonical_path=canonical_path, queue_path=queue_path, market="IL")
    assert not queue_path.exists()

    queue = mgr.ensure_queue_exists_from_canonical(ordered_seeds=ordered)

    assert queue.get("_action") == "created_from_needs_retry"
    assert queue_path.exists()
    assert queue["total"] == len(rerun_ids)
    assert sorted(p["seed_id"] for p in queue["pending"]) == sorted(rerun_ids)


def test_ensure_queue_exists_from_canonical_is_idempotent(fake_canonical):
    canonical_path, queue_path, ordered, _ = fake_canonical
    mgr = RerunQueueManager(canonical_path=canonical_path, queue_path=queue_path, market="IL")
    first = mgr.ensure_queue_exists_from_canonical(ordered_seeds=ordered)
    second = mgr.ensure_queue_exists_from_canonical(ordered_seeds=ordered)
    # Second call must not recreate the queue.
    assert second.get("_action") == "exists"
    assert first["total"] == second["total"]


def test_ensure_queue_exists_no_needs_retry_is_noop(tmp_path):
    canonical_path = tmp_path / "c.json"
    canonical_path.write_text(json.dumps({"batch_state": {}}), encoding="utf-8")
    mgr = RerunQueueManager(canonical_path=canonical_path, queue_path=tmp_path / "q.json", market="IL")
    result = mgr.ensure_queue_exists_from_canonical(ordered_seeds=[])
    assert result.get("_action") == "noop_no_needs_retry"
    assert not (tmp_path / "q.json").exists()


# ---------------------------------------------------------------------------
# 2. Auto-created queue preserves normal_continuation = Haval
# ---------------------------------------------------------------------------

def test_auto_created_queue_preserves_normal_continuation_at_haval(fake_canonical):
    canonical_path, queue_path, ordered, _ = fake_canonical
    mgr = RerunQueueManager(canonical_path=canonical_path, queue_path=queue_path, market="IL")
    queue = mgr.ensure_queue_exists_from_canonical(ordered_seeds=ordered)
    nc = queue.get("normal_continuation") or {}
    assert nc.get("next_seed_id") == "haval__h6__2022__2026__il"
    assert nc.get("last_completed_seed_id") == "gmc__yukon__2000__2026__il"


# ---------------------------------------------------------------------------
# 3. progress_summary exposes the spec fields
# ---------------------------------------------------------------------------

def test_progress_summary_active_mode_and_position(fake_canonical):
    canonical_path, queue_path, ordered, rerun_ids = fake_canonical
    mgr = RerunQueueManager(canonical_path=canonical_path, queue_path=queue_path, market="IL")
    mgr.ensure_queue_exists_from_canonical(ordered_seeds=ordered)
    summary = mgr.progress_summary()
    assert summary["active_mode"] == "rerun_queue"
    assert summary["total_rerun"] == len(rerun_ids)
    assert summary["completed_count"] == 0
    assert summary["pending_count"] == len(rerun_ids)
    assert summary["current_rerun_seed"] == sorted(rerun_ids)[0]
    assert summary["current_rerun_position"] == f"1 / {len(rerun_ids)}"
    assert summary["normal_continuation_paused_at"] == "haval__h6__2022__2026__il"
    assert summary["rerun_queue_closed"] is False


# ---------------------------------------------------------------------------
# 4. validate_canonical_update accepts rerun_queue candidates with frozen cursor
# ---------------------------------------------------------------------------

def test_validate_canonical_update_accepts_rerun_queue_no_movement(fake_canonical):
    canonical_path, _, ordered, _ = fake_canonical
    previous = json.loads(canonical_path.read_text())

    # Candidate has the same batch_state cursor (frozen) but +1 variant and
    # is tagged as the rerun_queue source.
    candidate = copy.deepcopy(previous)
    candidate["_candidate_source"] = br.CANDIDATE_SOURCE_RERUN_QUEUE
    candidate["accumulated_clean_export"]["variants"].append({
        "variant_id": "v-bmw-850-1",
        "make": "BMW", "model": "850i",
        "year_start": 2018, "year_end": 2026,
        "market": "IL",
    })
    res = br.validate_canonical_update(previous, candidate, market="IL")
    assert res.get("passed") is True
    assert not (res.get("issues") or [])


def test_validate_canonical_update_rejects_normal_candidate_moving_backward(fake_canonical):
    canonical_path, _, ordered, _ = fake_canonical
    previous = json.loads(canonical_path.read_text())

    # Same shape but without the rerun_queue tag — backward movement of
    # last_completed must still be rejected for non-rerun candidates.
    candidate = copy.deepcopy(previous)
    candidate["batch_state"]["last_completed_seed_id"] = "audi__a3__2003__2020__il"
    candidate["batch_state"]["next_seed_id"] = "audi__a4__1994__2026__il"
    candidate["_candidate_source"] = br.CANDIDATE_SOURCE_MERGED
    res = br.validate_canonical_update(previous, candidate, market="IL")
    assert res.get("passed") is False
    issues = res.get("issues") or []
    assert any("moved backward" in str(i) for i in issues)


# ---------------------------------------------------------------------------
# 5. End-to-end persist after a rerun seed leaves normal continuation frozen
# ---------------------------------------------------------------------------

def test_persist_canonical_after_seed_rerun_mode_freezes_normal_continuation(
    fake_canonical, monkeypatch, tmp_path
):
    canonical_path, queue_path, ordered, rerun_ids = fake_canonical
    monkeypatch.setattr(br, "_canonical_resume_path", lambda: canonical_path)
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: json.loads(canonical_path.read_text()))
    # Disable backup writes / GitHub fetch / GitHub config
    monkeypatch.setattr(br, "save_local_canonical_backup", lambda *a, **k: None)
    monkeypatch.setattr(br, "fetch_file_from_github", lambda *a, **k: None)
    monkeypatch.setattr(br, "get_github_config", lambda: {})
    monkeypatch.setattr(br, "_validate_saved_canonical", lambda *a, **k: {"ok": True, "issues": []})

    written = {}

    def fake_save(pkg):
        written["pkg"] = pkg
        canonical_path.write_text(json.dumps(pkg), encoding="utf-8")

    monkeypatch.setattr(br, "save_local_canonical_resume_package", fake_save)
    # Provide a fake final_export with the new BMW variant added on top
    yukon_variant = json.loads(canonical_path.read_text())["accumulated_clean_export"]["variants"][0]
    bmw_variant = {
        "variant_id": "v-bmw-850-1",
        "make": "BMW", "model": "850i",
        "year_start": 2018, "year_end": 2026,
        "market": "IL",
    }
    monkeypatch.setattr(br, "build_final_export", lambda: {"variants": [yukon_variant, bmw_variant], "quality_gate": {}, "audit": {}})

    seed = {"seed_id": "bmw__850i__2018__2026__il", "make": "BMW", "model": "850i", "year_start": 2018, "year_end": 2026, "market": "IL"}
    # Simulate the in-memory state after _process_seeds' freeze restore
    # (processed_seed_ids and cursor unchanged from previous canonical).
    previous = json.loads(canonical_path.read_text())
    batch_state = copy.deepcopy(previous["batch_state"])

    res = br.persist_canonical_after_seed(
        seed=seed,
        batch_state=batch_state,
        push_to_github=False,
        market="IL",
        rerun_mode=True,
        rerun_normal_continuation={
            "last_completed_seed_id": "gmc__yukon__2000__2026__il",
            "next_seed_id": "haval__h6__2022__2026__il",
        },
    )
    assert res.get("ok") is True, res
    saved = written["pkg"]
    bs = saved["batch_state"]
    # The rerun seed must NOT appear in processed_seed_ids; the canonical
    # cursor must remain frozen at gmc / haval; the candidate source must
    # be tagged as rerun_queue.
    assert "bmw__850i__2018__2026__il" not in bs["processed_seed_ids"]
    assert bs["last_completed_seed_id"] == "gmc__yukon__2000__2026__il"
    assert bs["next_seed_id"] == "haval__h6__2022__2026__il"
    assert saved.get("_candidate_source") == br.CANDIDATE_SOURCE_RERUN_QUEUE
    # The resolved rerun seed must be removed from canonical needs_retry.
    assert "bmw__850i__2018__2026__il" not in bs.get("needs_retry_seed_ids", [])
    # Variant count should reflect the new BMW variant.
    assert len(saved["accumulated_clean_export"]["variants"]) == 2


# ---------------------------------------------------------------------------
# 6. finalize_if_complete clears merged seeds from canonical needs_retry
# ---------------------------------------------------------------------------

def test_finalize_clears_canonical_needs_retry_for_merged_seeds(fake_canonical):
    canonical_path, queue_path, ordered, rerun_ids = fake_canonical
    mgr = RerunQueueManager(canonical_path=canonical_path, queue_path=queue_path, market="IL")
    mgr.ensure_queue_exists_from_canonical(ordered_seeds=ordered)
    for sid in rerun_ids:
        mgr.mark_success(sid, variants_added=1, result={})
    finalize = mgr.finalize_if_complete()
    assert finalize["ok"] is True
    canonical = json.loads(canonical_path.read_text())
    # All merged seeds must be gone from needs_retry_seed_ids so the queue
    # is not auto-recreated on the next run.
    leftover = set(canonical["batch_state"].get("needs_retry_seed_ids") or []) & set(rerun_ids)
    assert leftover == set()

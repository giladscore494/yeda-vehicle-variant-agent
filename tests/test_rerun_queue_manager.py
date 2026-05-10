"""Tests for RerunQueueManager and the rerun-queue runner integration."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import agent.batch_runner as br
from agent.rerun_queue_manager import (
    EXACT_54_RERUN_SEEDS,
    RerunQueueManager,
    DEFAULT_NORMAL_CONTINUATION,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def manager(tmp_path, monkeypatch):
    canonical_path = tmp_path / "canonical.json"
    queue_path = tmp_path / "rerun_queue.json"
    # Build a canonical containing all 54 + a Haval H6 + Yukon + a couple of
    # safe variants.  Variants are kept artificial but distinct.
    seed_dicts = [
        {**s, "market": "IL"} for s in EXACT_54_RERUN_SEEDS
    ] + [
        {"seed_id": "gmc__yukon__2000__2026__il", "make": "GMC", "model": "Yukon", "year_start": 2000, "year_end": 2026, "market": "IL"},
        {"seed_id": "haval__h6__2022__2026__il", "make": "Haval", "model": "H6", "year_start": 2022, "year_end": 2026, "market": "IL"},
    ]
    yukon_variant = {
        "seed_id": "gmc__yukon__2000__2026__il",
        "make": "GMC", "model": "Yukon", "market": "IL",
        "year_start": 2000, "year_end": 2026,
        "variant_id": "v-yukon-1",
    }
    canonical = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "processed_seed_ids": [s["seed_id"] for s in seed_dicts[:-1]],  # all but Haval
            "processed_seeds": [dict(s) for s in seed_dicts[:-1]],
            "last_completed_seed_id": "gmc__yukon__2000__2026__il",
            "next_seed_id": "haval__h6__2022__2026__il",
        },
        "accumulated_clean_export": {"variants": [yukon_variant]},
    }
    canonical_path.write_text(json.dumps(canonical), encoding="utf-8")

    # Make batch_runner think the ordered seeds are seed_dicts.
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seed_dicts)

    mgr = RerunQueueManager(
        canonical_path=canonical_path,
        queue_path=queue_path,
        market="IL",
    )
    return mgr, canonical_path, queue_path, seed_dicts


# ---------------------------------------------------------------------------
# 1-6. Scan / curated creation
# ---------------------------------------------------------------------------

def test_create_queue_from_exact_seed_list_produces_54(manager):
    mgr, canonical_path, queue_path, _ = manager
    queue = mgr.create_queue_from_exact_seed_list()
    assert queue_path.exists()
    assert queue["total"] == 54
    assert len(queue["pending"]) == 54
    assert queue["completed"] == []
    assert queue["failed_retry"] == []
    assert queue["progress"]["pending"] == 54
    assert queue["progress"]["percent"] == 0


def test_first_pending_seed_is_bmw_850i(manager):
    mgr, _, _, _ = manager
    mgr.create_queue_from_exact_seed_list()
    head = mgr.next_seed()
    assert head["seed_id"] == "bmw__850i__2018__2026__il"


def test_haval_h6_is_not_in_pending(manager):
    mgr, _, _, _ = manager
    queue = mgr.create_queue_from_exact_seed_list()
    pending_ids = {p["seed_id"] for p in queue["pending"]}
    assert "haval__h6__2022__2026__il" not in pending_ids


def test_scan_removes_54_from_processed_seed_ids(manager):
    mgr, canonical_path, _, _ = manager
    mgr.create_queue_from_exact_seed_list()
    canonical = json.loads(canonical_path.read_text())
    bs = canonical["batch_state"]
    rerun_ids = {s["seed_id"] for s in EXACT_54_RERUN_SEEDS}
    assert not (rerun_ids & set(bs["processed_seed_ids"]))
    assert not (rerun_ids & {s["seed_id"] for s in bs["processed_seeds"]})


def test_scan_preserves_variants_count(manager):
    mgr, canonical_path, _, _ = manager
    before = len(json.loads(canonical_path.read_text())["accumulated_clean_export"]["variants"])
    mgr.create_queue_from_exact_seed_list()
    after = len(json.loads(canonical_path.read_text())["accumulated_clean_export"]["variants"])
    assert before == after


def test_normal_continuation_remains_haval(manager):
    mgr, canonical_path, _, _ = manager
    queue = mgr.create_queue_from_exact_seed_list()
    assert queue["normal_continuation"]["next_seed_id"] == "haval__h6__2022__2026__il"
    canonical = json.loads(canonical_path.read_text())
    assert canonical["batch_state"]["next_seed_id"] == "haval__h6__2022__2026__il"


# ---------------------------------------------------------------------------
# 7-8. Runner integration / guardrails
# ---------------------------------------------------------------------------

def test_validate_selected_seed_rejects_haval_while_pending(manager):
    mgr, _, _, _ = manager
    mgr.create_queue_from_exact_seed_list()
    result = mgr.validate_selected_seed("haval__h6__2022__2026__il")
    assert result["ok"] is False
    assert result["expected_seed_id"] == "bmw__850i__2018__2026__il"


def test_validate_selected_seed_accepts_queue_head(manager):
    mgr, _, _, _ = manager
    mgr.create_queue_from_exact_seed_list()
    result = mgr.validate_selected_seed("bmw__850i__2018__2026__il")
    assert result["ok"] is True
    assert result["mode"] == "rerun_queue"


def test_validate_selected_seed_passes_when_queue_absent(tmp_path):
    mgr = RerunQueueManager(
        canonical_path=tmp_path / "nope.json",
        queue_path=tmp_path / "missing.json",
        market="IL",
    )
    assert mgr.validate_selected_seed("anything")["ok"] is True


# ---------------------------------------------------------------------------
# 9-11. mark_success / mark_failed_retry / progress
# ---------------------------------------------------------------------------

def test_zero_variant_no_proof_stays_in_failed_retry(manager):
    mgr, _, _, _ = manager
    mgr.create_queue_from_exact_seed_list()
    result = mgr.mark_failed_retry("bmw__850i__2018__2026__il", {"variants_added": 0})
    assert result["ok"] is True
    q = mgr.load_queue()
    failed_ids = {e["seed_id"] for e in q["failed_retry"]}
    assert "bmw__850i__2018__2026__il" in failed_ids
    completed_ids = {e["seed_id"] for e in q["completed"]}
    assert "bmw__850i__2018__2026__il" not in completed_ids


def test_mark_success_moves_seed_to_completed_and_updates_progress(manager):
    mgr, _, _, _ = manager
    mgr.create_queue_from_exact_seed_list()
    r = mgr.mark_success("bmw__850i__2018__2026__il", variants_added=3, result={})
    assert r["ok"] is True
    q = mgr.load_queue()
    assert "bmw__850i__2018__2026__il" in {e["seed_id"] for e in q["completed"]}
    assert "bmw__850i__2018__2026__il" not in {e["seed_id"] for e in q["pending"]}
    summary = mgr.progress_summary()
    assert summary["completed_count"] == 1
    assert summary["pending_count"] == 53
    assert summary["progress_percent"] >= 1


def test_mark_success_without_variants_or_proof_is_rejected(manager):
    mgr, _, _, _ = manager
    mgr.create_queue_from_exact_seed_list()
    r = mgr.mark_success("bmw__850i__2018__2026__il", variants_added=0, result={})
    assert r["ok"] is False
    q = mgr.load_queue()
    # Seed must have been moved out of pending into failed_retry, not completed.
    assert "bmw__850i__2018__2026__il" in {e["seed_id"] for e in q["failed_retry"]}
    assert "bmw__850i__2018__2026__il" not in {e["seed_id"] for e in q["completed"]}


# ---------------------------------------------------------------------------
# 12-14. finalize_if_complete behavior
# ---------------------------------------------------------------------------

def test_finalize_merges_completed_into_processed_lists_and_deletes_queue(manager):
    mgr, canonical_path, queue_path, _ = manager
    mgr.create_queue_from_exact_seed_list()
    # Resolve all 54 successfully.
    for seed in EXACT_54_RERUN_SEEDS:
        mgr.mark_success(seed["seed_id"], variants_added=1, result={})
    finalize = mgr.finalize_if_complete()
    assert finalize["ok"] is True
    assert finalize["merged_count"] == 54
    assert not queue_path.exists()
    canonical = json.loads(canonical_path.read_text())
    processed_ids = set(canonical["batch_state"]["processed_seed_ids"])
    rerun_ids = {s["seed_id"] for s in EXACT_54_RERUN_SEEDS}
    assert rerun_ids.issubset(processed_ids)
    processed_seed_ids_from_objects = {s["seed_id"] for s in canonical["batch_state"]["processed_seeds"] if isinstance(s, dict)}
    assert rerun_ids.issubset(processed_seed_ids_from_objects)
    assert canonical["batch_state"]["next_seed_id"] == "haval__h6__2022__2026__il"


def test_finalize_blocks_when_failed_retry_not_empty(manager):
    mgr, _, queue_path, _ = manager
    mgr.create_queue_from_exact_seed_list()
    mgr.mark_failed_retry("bmw__850i__2018__2026__il", {"variants_added": 0})
    finalize = mgr.finalize_if_complete()
    assert finalize["ok"] is False
    assert finalize["reason"] == "queue_not_complete"
    assert queue_path.exists()


def test_after_finalize_normal_batch_resumes_from_haval(manager):
    mgr, canonical_path, _, _ = manager
    mgr.create_queue_from_exact_seed_list()
    for seed in EXACT_54_RERUN_SEEDS:
        mgr.mark_success(seed["seed_id"], variants_added=1, result={})
    mgr.finalize_if_complete()
    canonical = json.loads(canonical_path.read_text())
    assert canonical["batch_state"]["next_seed_id"] == "haval__h6__2022__2026__il"
    assert canonical["batch_state"]["last_completed_seed_id"] == "gmc__yukon__2000__2026__il"


# ---------------------------------------------------------------------------
# 15. Invalid IDs never enter the active pending queue
# ---------------------------------------------------------------------------

def test_invalid_seed_ids_are_excluded(manager):
    mgr, _, queue_path, _ = manager
    seeds = [{"seed_id": "s1"}, {"seed_id": "bmw__850i__2018__2026__il", "make": "BMW", "model": "850i", "year_start": 2018, "year_end": 2026}]
    queue = mgr.create_queue_from_exact_seed_list(seeds=seeds)
    pending_ids = {p["seed_id"] for p in queue["pending"]}
    assert "s1" not in pending_ids
    assert "s1" in queue["invalid_seed_ids"]


# ---------------------------------------------------------------------------
# Helper sanity checks
# ---------------------------------------------------------------------------

def test_progress_summary_when_no_queue(tmp_path):
    mgr = RerunQueueManager(
        canonical_path=tmp_path / "c.json",
        queue_path=tmp_path / "q.json",
        market="IL",
    )
    s = mgr.progress_summary()
    assert s["can_run_normal_batch"] is True
    assert s["total_rerun"] == 0


def test_has_pending_reflects_state(manager):
    mgr, _, _, _ = manager
    assert mgr.has_pending() is False
    mgr.create_queue_from_exact_seed_list()
    assert mgr.has_pending() is True

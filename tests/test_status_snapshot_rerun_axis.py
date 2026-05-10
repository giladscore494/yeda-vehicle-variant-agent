"""Tests for the dedicated RERUN_QUEUE progress axis surfaced by
``app._status_snapshot``.

These tests pin the contract that:

* When canonical reports needs_retry seeds but ``rerun_queue.json`` is
  missing, ``_status_snapshot`` must auto-create the queue and report
  ``active_mode == "rerun_queue"``.
* The dedicated rerun progress axis fields (``rerun_total``,
  ``current_rerun_position`` etc.) reflect the actual queue state and
  advance correctly as seeds are completed.
* The normal batch axis is frozen at Haval H6 / GMC Yukon while the
  rerun queue is active — BMW (a rerun-queue seed) must never appear
  as ``next_normal_seed``.
* Once the queue is fully drained and finalized, the UI returns to
  normal batch mode with ``next_normal_seed == haval__h6__...``.
"""
from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest

import agent.batch_runner as br
import app as app_mod
from agent.rerun_queue_manager import RerunQueueManager


# ---------------------------------------------------------------------------
# Fixture: realistic canonical + ordered list mirroring the user's scenario
# ---------------------------------------------------------------------------

ORDERED_SEEDS = [
    {"seed_id": "abarth__124__2016__2020__il", "make": "Abarth", "model": "124", "year_start": 2016, "year_end": 2020, "market": "IL"},
    {"seed_id": "bmw__850i__2018__2026__il", "make": "BMW", "model": "850i", "year_start": 2018, "year_end": 2026, "market": "IL"},
    {"seed_id": "bmw__z4__2019__2026__il", "make": "BMW", "model": "Z4", "year_start": 2019, "year_end": 2026, "market": "IL"},
    {"seed_id": "daewoo__lacetti__2003__2011__il", "make": "Daewoo", "model": "Lacetti", "year_start": 2003, "year_end": 2011, "market": "IL"},
    # ... 50 more synthesized rerun seeds to reach the required 54 ...
] + [
    {"seed_id": f"make{i}__model{i}__2010__2020__il", "make": f"Make{i}", "model": f"Model{i}",
     "year_start": 2010, "year_end": 2020, "market": "IL"}
    for i in range(50)
] + [
    {"seed_id": "gmc__yukon__2000__2026__il", "make": "GMC", "model": "Yukon", "year_start": 2000, "year_end": 2026, "market": "IL"},
    {"seed_id": "haval__h6__2022__2026__il", "make": "Haval", "model": "H6", "year_start": 2022, "year_end": 2026, "market": "IL"},
]

RERUN_IDS = (
    ["bmw__850i__2018__2026__il", "bmw__z4__2019__2026__il", "daewoo__lacetti__2003__2011__il"]
    + [f"make{i}__model{i}__2010__2020__il" for i in range(50)]
    + ["haval__h6__2022__2026__il"]  # placeholder to round up to 54
)
# Exactly 54 rerun seeds; "haval__h6" replaced with a synthetic to keep
# normal_continuation pointed at the real Haval seed.
RERUN_IDS = RERUN_IDS[:53] + ["zzz__zlast__2010__2020__il"]
ORDERED_SEEDS = ORDERED_SEEDS + [
    {"seed_id": "zzz__zlast__2010__2020__il", "make": "Zzz", "model": "Zlast",
     "year_start": 2010, "year_end": 2020, "market": "IL"}
]


@pytest.fixture
def fake_canonical(tmp_path, monkeypatch):
    canonical_path = tmp_path / "resume_package_canonical.json"
    queue_path = tmp_path / "rerun_queue.json"

    rerun_set = set(RERUN_IDS)
    processed_ids = [s["seed_id"] for s in ORDERED_SEEDS
                     if s["seed_id"] not in rerun_set and s["seed_id"] != "haval__h6__2022__2026__il"]
    yukon_variant = {
        "variant_id": "v-yukon-1",
        "make": "GMC", "model": "Yukon",
        "year_start": 2000, "year_end": 2026, "market": "IL",
    }
    variants = [{"variant_id": f"v-{i}"} for i in range(1322)] + [yukon_variant]
    assert len(variants) == 1323
    assert len(RERUN_IDS) == 54

    canonical = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "schema_version": "batch_state_v1",
            "market": "IL",
            "total_seeds": len(ORDERED_SEEDS),
            "processed_seed_ids": processed_ids,
            "processed_seeds": [{"seed_id": sid} for sid in processed_ids],
            "needs_retry_seed_ids": list(RERUN_IDS),
            "last_completed_seed_id": "gmc__yukon__2000__2026__il",
            "next_seed_id": "haval__h6__2022__2026__il",
            "failed_seed_ids": [],
            "failed_details": [],
            "in_progress_seed_id": None,
        },
        "accumulated_clean_export": {"variants": variants, "quality_gate": {}, "audit": {}},
    }
    canonical_path.write_text(json.dumps(canonical), encoding="utf-8")

    # Patch the rerun manager class so it uses tmp paths.
    monkeypatch.setattr(
        app_mod,
        "RerunQueueManager",
        partial(RerunQueueManager, canonical_path=canonical_path, queue_path=queue_path),
    )
    # Patch ordered list and canonical loaders/batch helpers used by snapshot.
    monkeypatch.setattr(app_mod, "get_ordered_seed_list", lambda market="IL": ORDERED_SEEDS)
    monkeypatch.setattr(app_mod, "load_local_canonical_resume_package", lambda: json.loads(canonical_path.read_text()))
    monkeypatch.setattr(
        app_mod,
        "sync_batch_state_from_canonical",
        lambda market="IL": dict(json.loads(canonical_path.read_text())["batch_state"]),
    )
    monkeypatch.setattr(app_mod, "sanitize_repair_queue_state", lambda s, o: s)
    monkeypatch.setattr(app_mod, "get_batch_progress", lambda market="IL": {"next_seed": {"seed_id": "haval__h6__2022__2026__il"}})
    monkeypatch.setattr(app_mod, "build_final_export", lambda: {"variants": variants})
    # batch_runner also needs the ordered list for ensure_queue_exists_from_canonical
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": ORDERED_SEEDS)

    return canonical_path, queue_path


# ---------------------------------------------------------------------------
# 1. Initial status: rerun_queue.json auto-created; position 1 / 54
# ---------------------------------------------------------------------------

def test_status_snapshot_auto_creates_rerun_queue_from_needs_retry(fake_canonical):
    canonical_path, queue_path = fake_canonical
    assert not queue_path.exists()
    snap = app_mod._status_snapshot("IL")
    assert queue_path.exists()
    assert snap["active_mode"] == "rerun_queue"
    assert snap["rerun_active"] is True
    assert snap["rerun_queue_closed"] is False


def test_status_snapshot_initial_position_is_1_of_54(fake_canonical):
    snap = app_mod._status_snapshot("IL")
    assert snap["rerun_total"] == 54
    assert snap["rerun_pending"] == 54
    assert snap["rerun_completed"] == 0
    assert snap["rerun_failed_retry"] == 0
    assert snap["current_rerun_position"] == "1 / 54"
    assert snap["current_rerun_seed"] == "bmw__850i__2018__2026__il"


def test_status_snapshot_normal_axis_frozen_at_haval(fake_canonical):
    snap = app_mod._status_snapshot("IL")
    # Normal batch axis is frozen while rerun queue is active.
    assert snap["normal_continuation_paused_at"] == "haval__h6__2022__2026__il"
    assert snap["normal_next_seed_paused_at"] == "haval__h6__2022__2026__il"
    assert snap["normal_last_completed_seed_id"] == "gmc__yukon__2000__2026__il"
    # next_normal_seed must never leak BMW (a rerun seed).
    assert snap["next_normal_seed"] == "haval__h6__2022__2026__il"
    assert snap["next_normal_seed"] != "bmw__850i__2018__2026__il"


def test_status_snapshot_initial_variants_and_processed_counts(fake_canonical):
    snap = app_mod._status_snapshot("IL")
    assert snap["variants_count"] == 1323
    # processed_count must match the canonical processed_seed_ids length
    assert snap["overall_processed_count"] == snap["processed_count"]
    assert snap["total_seeds"] == len(ORDERED_SEEDS)


# ---------------------------------------------------------------------------
# 2. After one successful rerun: 2 / 54, BMW is last_completed_rerun_seed,
#    next current_rerun_seed is BMW Z4
# ---------------------------------------------------------------------------

def test_status_snapshot_after_one_success_advances_position(fake_canonical):
    canonical_path, queue_path = fake_canonical
    # Prime the queue then mark BMW 850i success.
    app_mod._status_snapshot("IL")
    mgr = RerunQueueManager(canonical_path=canonical_path, queue_path=queue_path, market="IL")
    res = mgr.mark_success(
        "bmw__850i__2018__2026__il",
        variants_added=1,
        result={"variants": [{"variant_id": "v-bmw-850-1"}]},
    )
    assert res.get("ok") is True

    snap = app_mod._status_snapshot("IL")
    assert snap["active_mode"] == "rerun_queue"
    assert snap["rerun_total"] == 54
    assert snap["rerun_completed"] == 1
    assert snap["rerun_pending"] == 53
    assert snap["current_rerun_position"] == "2 / 54"
    assert snap["last_completed_rerun_seed"] == "bmw__850i__2018__2026__il"
    assert snap["current_rerun_seed"] == "bmw__z4__2019__2026__il"
    # Normal continuation must still be paused at Haval H6.
    assert snap["normal_continuation_paused_at"] == "haval__h6__2022__2026__il"
    assert snap["next_normal_seed"] != "bmw__850i__2018__2026__il"


# ---------------------------------------------------------------------------
# 3. After all 54 are completed, finalize and switch to normal_batch
# ---------------------------------------------------------------------------

def test_status_snapshot_after_finalize_returns_to_normal_batch(fake_canonical):
    canonical_path, queue_path = fake_canonical
    # Prime then complete all 54 seeds.
    app_mod._status_snapshot("IL")
    mgr = RerunQueueManager(canonical_path=canonical_path, queue_path=queue_path, market="IL")
    for sid in RERUN_IDS:
        mgr.mark_success(sid, variants_added=1, result={"variants": [{"variant_id": f"v-{sid}"}]})
    finalize = mgr.finalize_if_complete()
    assert finalize.get("ok") is True
    assert not queue_path.exists()

    # After finalize, canonical.needs_retry_seed_ids is empty so the
    # snapshot must return to normal_batch with next_seed at Haval H6.
    snap = app_mod._status_snapshot("IL")
    assert snap["active_mode"] == "normal_batch"
    assert snap["rerun_active"] is False
    assert snap["rerun_queue_closed"] is True
    assert snap["next_normal_seed"] == "haval__h6__2022__2026__il"

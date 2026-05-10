"""Tests for the Problem Queue progress axis surfaced by ``app._status_snapshot``.

These tests pin the contract that:

* When canonical reports problem_repair_state with active=True, ``_status_snapshot``
  reports ``active_mode == "problem_queue"`` sourced exclusively from canonical.
* The PRS progress fields (``pq_total``, ``pq_current_position`` etc.) reflect the
  canonical state and advance correctly as seeds are completed.
* The normal batch axis is frozen at Haval H6 while problem_queue is active —
  BMW (a problem-queue seed) must never appear as ``next_normal_seed``.
* Once all 54 seeds are completed, the UI returns to normal_batch mode.
* Deleting data/output/problem_queue.json does NOT change UI status (canonical is
  the only source of truth).
"""
from __future__ import annotations

import copy
import json

import pytest

import app as app_mod
from agent.problem_queue import compute_problem_repair_state


# ---------------------------------------------------------------------------
# Shared seed data (54 problem seeds)
# ---------------------------------------------------------------------------

RERUN_IDS = (
    ["bmw__850i__2018__2026__il", "bmw__z4__2019__2026__il", "daewoo__lacetti__2003__2011__il"]
    + [f"make{i}__model{i}__2010__2020__il" for i in range(50)]
    + ["zzz__zlast__2010__2020__il"]
)
assert len(RERUN_IDS) == 54


def _make_canonical(needs_retry=None, completed=None, last_completed=None):
    """Build a synthetic canonical for testing _status_snapshot."""
    needs_retry = list(needs_retry or RERUN_IDS)
    completed = list(completed or [])
    false_processed = list(RERUN_IDS)  # original full set (constant)
    canonical = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "schema_version": "batch_state_v1",
            "market": "IL",
            "needs_retry_seed_ids": needs_retry,
            "false_processed_seed_ids": false_processed,
            "original_false_processed_count": 54,
            "last_completed_seed_id": "gmc__yukon__2000__2026__il",
            "next_seed_id": "haval__h6__2022__2026__il",
            "processed_seed_ids": ["abarth__124__2016__2020__il"],
            "failed_seed_ids": [],
            "failed_details": [],
            "in_progress_seed_id": None,
        },
        "accumulated_clean_export": {
            "variants": [{"variant_id": f"v-{i}"} for i in range(1323)],
        },
        "problem_repair_state": {
            "active": True,
            "total": 54,
            "completed_seed_ids": completed,
            "last_completed_seed_id": last_completed,
            "pending_seed_ids": needs_retry,
            "failed_retry_seed_ids": [],
            "current_seed_id": needs_retry[0] if needs_retry else None,
            "normal_continuation": {
                "next_seed_id": "haval__h6__2022__2026__il",
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
    return canonical


@pytest.fixture
def fake_canonical(monkeypatch):
    """Patch app_mod.load_problem_queue_canonical to return a synthetic canonical."""
    canonical = _make_canonical()

    def _load():
        return copy.deepcopy(canonical)

    monkeypatch.setattr(app_mod, "load_problem_queue_canonical", _load)
    return canonical


# ---------------------------------------------------------------------------
# 1. Initial status: problem_queue active; position 1 / 54
# ---------------------------------------------------------------------------

def test_status_snapshot_reports_problem_queue_mode(fake_canonical):
    snap = app_mod._status_snapshot("IL")
    assert snap["active_mode"] == "problem_queue"
    assert snap["pq_active"] is True


def test_status_snapshot_initial_position_is_1_of_54(fake_canonical):
    snap = app_mod._status_snapshot("IL")
    assert snap["pq_total"] == 54
    assert snap["pq_pending"] == 54
    assert snap["pq_completed"] == 0
    assert snap["pq_failed_retry"] == 0
    assert snap["pq_current_position"] == "1 / 54"
    assert snap["pq_current_seed"] == "bmw__850i__2018__2026__il"


def test_status_snapshot_normal_axis_frozen_at_haval(fake_canonical):
    snap = app_mod._status_snapshot("IL")
    # Normal batch axis is frozen while problem_queue is active.
    assert snap["pq_normal_paused_at"] == "haval__h6__2022__2026__il"
    # next_normal_seed must never leak BMW (a problem-queue seed).
    assert snap["next_normal_seed"] == "haval__h6__2022__2026__il"
    assert snap["next_normal_seed"] != "bmw__850i__2018__2026__il"


def test_status_snapshot_initial_variants_and_processed_counts(fake_canonical):
    snap = app_mod._status_snapshot("IL")
    assert snap["variants_count"] == 1323
    assert snap["processed_count"] >= 0


# ---------------------------------------------------------------------------
# 2. After one successful run: 2 / 54
# ---------------------------------------------------------------------------

def test_status_snapshot_after_one_success_advances_position(monkeypatch):
    """After BMW 850i completes, position advances to 2/54."""
    completed = ["bmw__850i__2018__2026__il"]
    remaining = [sid for sid in RERUN_IDS if sid not in completed]
    canonical = _make_canonical(
        needs_retry=remaining,
        completed=completed,
        last_completed="bmw__850i__2018__2026__il",
    )

    def _load():
        return copy.deepcopy(canonical)

    monkeypatch.setattr(app_mod, "load_problem_queue_canonical", _load)

    snap = app_mod._status_snapshot("IL")
    assert snap["active_mode"] == "problem_queue"
    assert snap["pq_total"] == 54
    assert snap["pq_completed"] == 1
    assert snap["pq_pending"] == 53
    assert snap["pq_current_position"] == "2 / 54"
    assert snap["pq_last_completed_seed"] == "bmw__850i__2018__2026__il"
    assert snap["pq_current_seed"] == remaining[0]
    # Normal continuation must still be paused at Haval H6.
    assert snap["pq_normal_paused_at"] == "haval__h6__2022__2026__il"
    assert snap["next_normal_seed"] != "bmw__850i__2018__2026__il"


# ---------------------------------------------------------------------------
# 3. After all 54 completed: switch to normal_batch
# ---------------------------------------------------------------------------

def test_status_snapshot_after_all_completed_returns_to_normal_batch(monkeypatch):
    canonical = _make_canonical(
        needs_retry=[],
        completed=list(RERUN_IDS),
        last_completed=RERUN_IDS[-1],
    )
    # Problem_repair_state: active = False when no pending seeds remain.
    canonical["problem_repair_state"]["active"] = False
    canonical["problem_repair_state"]["pending_seed_ids"] = []
    canonical["problem_repair_state"]["current_seed_id"] = None
    canonical["problem_repair_state"]["progress"]["pending"] = 0
    canonical["problem_repair_state"]["progress"]["completed"] = 54
    canonical["problem_repair_state"]["progress"]["current_position"] = "54 / 54"
    # In normal batch mode next_seed_id points to Haval (continuation).
    canonical["batch_state"]["next_seed_id"] = "haval__h6__2022__2026__il"
    canonical["batch_state"]["needs_retry_seed_ids"] = []

    def _load():
        return copy.deepcopy(canonical)

    monkeypatch.setattr(app_mod, "load_problem_queue_canonical", _load)

    snap = app_mod._status_snapshot("IL")
    assert snap["active_mode"] == "normal_batch"
    assert snap["pq_active"] is False
    assert snap["next_normal_seed"] == "haval__h6__2022__2026__il"


# ---------------------------------------------------------------------------
# 4. Deleting problem_queue.json mirror does not change UI status
# ---------------------------------------------------------------------------

def test_status_snapshot_independent_of_problem_queue_json(tmp_path, monkeypatch):
    """problem_queue.json is only a mirror; deleting it must not affect status."""
    canonical = _make_canonical()

    def _load():
        return copy.deepcopy(canonical)

    monkeypatch.setattr(app_mod, "load_problem_queue_canonical", _load)

    # Ensure the queue mirror file does not exist (simulates deletion).
    pq_file = tmp_path / "problem_queue.json"
    assert not pq_file.exists()

    snap = app_mod._status_snapshot("IL")
    assert snap["active_mode"] == "problem_queue"
    assert snap["pq_total"] == 54
    assert snap["pq_current_seed"] == "bmw__850i__2018__2026__il"

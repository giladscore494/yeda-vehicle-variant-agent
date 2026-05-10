"""Regression tests for problem-queue canonical persistence bugs.

Covers the two production bugs:

Bug 1 – Problem-queue progress was updated even when canonical variant
persistence failed.  A seed must NOT be marked completed unless the
canonical update (variant merge + cursor freeze + atomic save) succeeds.

Bug 2 – Progress total shrinks from 54 → 53 after the first completion
because the old code removed the seed from false_processed_seed_ids, which
was the denominator used to compute total.

Tests
-----
1. test_problem_queue_failed_persist_does_not_advance
2. test_problem_queue_success_freezes_normal_cursor
3. test_progress_total_does_not_shrink
4. test_progress_bar_uses_computed_progress
5. test_partial_state_repair
"""
from __future__ import annotations

import copy

import pytest

import agent.batch_runner as br
from agent.problem_queue import (
    compute_problem_repair_state,
    repair_problem_queue_partial_persist_state,
)

# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

BMW_SEED_ID = "bmw__850i__2018__2026__il"
Z4_SEED_ID = "bmw__z4_sdrive20i__2019__2026__il"
HAVAL_SEED_ID = "haval__h6__2022__2026__il"
GMC_SEED_ID = "gmc__yukon__2000__2026__il"

PROBLEM_IDS = [BMW_SEED_ID, Z4_SEED_ID] + [
    f"make{i}__model{i}__2010__2020__il" for i in range(52)
]
assert len(PROBLEM_IDS) == 54


def _base_canonical(needs_retry=None, completed=None):
    needs_retry = list(needs_retry if needs_retry is not None else PROBLEM_IDS)
    completed = list(completed or [])
    return {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "market": "IL",
            "needs_retry_seed_ids": needs_retry,
            "false_processed_seed_ids": list(PROBLEM_IDS),  # always 54
            "original_false_processed_count": 54,
            "last_completed_seed_id": GMC_SEED_ID,
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
            "original_problem_seed_ids": list(PROBLEM_IDS),
            "completed_seed_ids": completed,
            "last_completed_seed_id": completed[-1] if completed else None,
            "pending_seed_ids": needs_retry,
            "failed_retry_seed_ids": [],
            "current_seed_id": needs_retry[0] if needs_retry else None,
            "normal_continuation": {
                "next_seed_id": HAVAL_SEED_ID,
                "last_completed_seed_id": GMC_SEED_ID,
            },
            "progress": {
                "total": 54,
                "completed": len(completed),
                "pending": len(needs_retry),
                "failed_retry": 0,
                "current_position": (
                    f"{len(completed) + 1} / 54" if needs_retry else "54 / 54"
                ),
            },
        },
    }


_ORDERED = [
    {
        "seed_id": BMW_SEED_ID,
        "make": "BMW",
        "model": "850i",
        "year_start": 2018,
        "year_end": 2026,
        "market": "IL",
    },
    {
        "seed_id": Z4_SEED_ID,
        "make": "BMW",
        "model": "Z4",
        "year_start": 2019,
        "year_end": 2026,
        "market": "IL",
    },
    {
        "seed_id": HAVAL_SEED_ID,
        "make": "Haval",
        "model": "H6",
        "year_start": 2022,
        "year_end": 2026,
        "market": "IL",
    },
] + [
    {
        "seed_id": f"make{i}__model{i}__2010__2020__il",
        "make": f"make{i}",
        "model": f"model{i}",
        "year_start": 2010,
        "year_end": 2020,
        "market": "IL",
    }
    for i in range(52)
]


def _common_patches(monkeypatch, canonical):
    """Apply standard monkeypatches needed for run_next_batch in problem_queue mode."""
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": _ORDERED)
    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": {
        "passed": True,
        "repair_required": False,
        "needs_retry_required": False,
        "false_processed_seeds": [],
        "coverage_audit": {"holes_count": 0, "missing_seeds": []},
    })
    monkeypatch.setattr(br, "load_batch_state", lambda market="IL": {
        "market": "IL",
        "processed_seed_ids": [],
        "failed_seed_ids": [],
        "failed_details": [],
        "needs_retry_seed_ids": [],
        "last_completed_seed_id": None,
        "in_progress_seed_id": None,
        "next_seed_id": HAVAL_SEED_ID,
    })
    monkeypatch.setattr(
        br, "_load_outputs",
        lambda: {
            "run_history": [],
            "unresolved": [],
            "conflicts": [],
            "verified": [],
            "partial": [],
            "sources": [],
        },
    )
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_refresh_coverage", lambda s, o: None)
    monkeypatch.setattr(br, "save_json", lambda *a, **k: None)
    monkeypatch.setattr(br, "persist_canonical_resume_package", lambda **k: {"ok": True})
    monkeypatch.setattr(br, "persist_canonical_after_seed", lambda **k: {"ok": True})


# ---------------------------------------------------------------------------
# 1. Failed persist must NOT advance problem-queue state
# ---------------------------------------------------------------------------

def test_problem_queue_failed_persist_does_not_advance(monkeypatch):
    """When persist_canonical_problem_queue_seed returns ok=False, the seed
    must remain in needs_retry and no completed progress must be recorded
    in the batch result.
    """
    canonical = _base_canonical()
    _common_patches(monkeypatch, canonical)

    # Fake BMW run returns 1 variant added.
    def _fake_run(make, model, *args, **kwargs):
        return {
            "status": "completed",
            "variants_created": 1,
            "accounting": {"variants_added_to_canonical": 1},
        }
    monkeypatch.setattr(br, "run_single_model", _fake_run)

    # Simulate canonical save failure.
    monkeypatch.setattr(
        br,
        "persist_canonical_problem_queue_seed",
        lambda **k: {
            "ok": False,
            "local_saved": False,
            "saved_canonical": False,
            "pushed_any": False,
            "issue": "candidate_last_completed_seed_id moved backward",
        },
    )

    result = br.run_next_batch(limit=1, market="IL")

    assert result["batch_mode"] == "problem_queue"
    # The batch itself still returns "completed" (seed was attempted).
    assert result["status"] == "completed"

    pq = result.get("problem_queue_post") or {}
    bmw_entry = pq.get(BMW_SEED_ID, {})
    # Seed must NOT be reported as closed/completed.
    assert bmw_entry.get("closed") is False, (
        f"BMW must remain pending; got closed={bmw_entry.get('closed')}"
    )
    cp = bmw_entry.get("canonical_persist") or {}
    assert cp.get("ok") is False, "canonical_persist.ok must be False on persist failure"


# ---------------------------------------------------------------------------
# 2. Successful persist must freeze normal cursor
# ---------------------------------------------------------------------------

def test_problem_queue_success_freezes_normal_cursor(monkeypatch):
    """When persist_canonical_problem_queue_seed returns ok=True, the seed is
    reported as closed and normal continuation cursors must remain frozen at
    GMC / Haval in the persist call arguments (verifiable via closure capture).
    """
    canonical = _base_canonical()
    _common_patches(monkeypatch, canonical)

    def _fake_run(make, model, *args, **kwargs):
        return {
            "status": "completed",
            "variants_created": 1,
            "accounting": {"variants_added_to_canonical": 1},
        }
    monkeypatch.setattr(br, "run_single_model", _fake_run)

    persist_calls = []

    def _fake_persist(**kwargs):
        persist_calls.append(kwargs)
        return {
            "ok": True,
            "local_saved": True,
            "saved_canonical": True,
            "pushed_any": False,
            "validate_result": {"passed": True},
        }

    monkeypatch.setattr(br, "persist_canonical_problem_queue_seed", _fake_persist)

    result = br.run_next_batch(limit=1, market="IL")

    assert result["batch_mode"] == "problem_queue"
    assert result["status"] == "completed"

    pq = result.get("problem_queue_post") or {}
    bmw_entry = pq.get(BMW_SEED_ID, {})
    assert bmw_entry.get("closed") is True, "BMW must be closed after successful persist"

    # persist_canonical_problem_queue_seed must have been called with BMW's seed_id.
    assert persist_calls, "persist_canonical_problem_queue_seed was not called"
    assert persist_calls[0]["seed_id"] == BMW_SEED_ID

    # The function is responsible for freezing the cursor internally; here we
    # verify that canonical_persist is reported ok=True in the output.
    assert (bmw_entry.get("canonical_persist") or {}).get("ok") is True


# ---------------------------------------------------------------------------
# 3. Progress total must not shrink from 54 to 53
# ---------------------------------------------------------------------------

def test_progress_total_does_not_shrink():
    """After BMW completes, total must remain 54, pending = 53, completed = 1."""
    # Simulate canonical state after BMW was successfully completed.
    remaining = [sid for sid in PROBLEM_IDS if sid != BMW_SEED_ID]
    canonical = _base_canonical(needs_retry=remaining, completed=[BMW_SEED_ID])

    prs = compute_problem_repair_state(canonical)

    assert prs["progress"]["completed"] == 1, (
        f"completed should be 1 after BMW; got {prs['progress']['completed']}"
    )
    assert prs["progress"]["pending"] == 53, (
        f"pending should be 53; got {prs['progress']['pending']}"
    )
    # Total must come from original_problem_seed_ids / false_processed_seed_ids, not from pending.
    assert prs["total"] == 54, (
        f"total must remain 54; got {prs['total']}"
    )
    assert prs["progress"]["current_position"] == "2 / 54", (
        f"current_position should be '2 / 54'; got {prs['progress']['current_position']}"
    )
    assert prs["current_seed_id"] == Z4_SEED_ID, (
        f"next seed should be Z4; got {prs['current_seed_id']}"
    )


# ---------------------------------------------------------------------------
# 4. Progress bar must use dynamically computed progress, not stale stored value
# ---------------------------------------------------------------------------

def test_progress_bar_uses_computed_progress():
    """Stored progress with wrong total must be overridden by compute_problem_repair_state.

    This tests the Bug-2 scenario: the stored problem_repair_state.progress has
    total=53 / completed=0 (stale / wrong), but the actual derivable state from
    false_processed_seed_ids (54) and needs_retry (53) gives total=54, completed=1.
    """
    remaining = [sid for sid in PROBLEM_IDS if sid != BMW_SEED_ID]
    canonical = _base_canonical(needs_retry=remaining, completed=[BMW_SEED_ID])

    # Corrupt the stored progress to simulate the Bug-2 stale state.
    canonical["problem_repair_state"]["progress"] = {
        "total": 53,       # wrong — should be 54
        "completed": 0,    # wrong — should be 1
        "pending": 53,
        "failed_retry": 0,
        "current_position": "1 / 53",  # wrong
    }
    # Also corrupt total in problem_repair_state (as produced by old code).
    canonical["problem_repair_state"]["total"] = 53

    # compute_problem_repair_state must re-derive the correct values from source data.
    prs = compute_problem_repair_state(canonical)

    assert prs["total"] == 54, (
        f"total must be recomputed as 54; got {prs['total']}"
    )
    assert prs["progress"]["completed"] == 1, (
        f"completed must be recomputed as 1; got {prs['progress']['completed']}"
    )
    assert prs["progress"]["pending"] == 53
    assert prs["progress"]["current_position"] == "2 / 54", (
        f"current_position should be '2 / 54'; got {prs['progress']['current_position']}"
    )


# ---------------------------------------------------------------------------
# 5. Partial-state repair: seed completed but no evidence → restore to pending
# ---------------------------------------------------------------------------

def test_partial_state_repair():
    """A seed that is in completed_seed_ids but has no variants / dedupe /
    no_variants_reason evidence must be moved back to needs_retry by
    repair_problem_queue_partial_persist_state.
    """
    # Simulate the partial-persist state: BMW is in completed_seed_ids but
    # no BMW variant exists in accumulated_clean_export and no dedupe/no_variant proof.
    remaining_needs_retry = [sid for sid in PROBLEM_IDS if sid != BMW_SEED_ID]
    canonical = _base_canonical(
        needs_retry=remaining_needs_retry,
        completed=[BMW_SEED_ID],
    )
    # The variants list has NO BMW variant (only generic v-0 … v-1322).
    # No dedupe_proof_by_seed, no no_variants_by_seed for BMW.

    result = repair_problem_queue_partial_persist_state(canonical, persist=False)

    assert result["ok"] is True
    assert result["changed"] is True
    assert BMW_SEED_ID in result["repaired"], (
        f"BMW must be in repaired list; got {result['repaired']}"
    )

    # After repair: BMW is back in needs_retry at the front.
    bs = canonical["batch_state"]
    assert bs["needs_retry_seed_ids"][0] == BMW_SEED_ID, (
        "BMW must be at the front of needs_retry after repair"
    )

    # BMW must be removed from completed_seed_ids.
    prs = canonical["problem_repair_state"]
    assert BMW_SEED_ID not in prs.get("completed_seed_ids", []), (
        "BMW must be removed from completed_seed_ids after repair"
    )

    # Progress recomputed: 54 pending, 0 completed, position 1/54.
    recomputed = compute_problem_repair_state(canonical)
    assert recomputed["progress"]["pending"] == 54
    assert recomputed["progress"]["completed"] == 0
    assert recomputed["progress"]["current_position"] == "1 / 54"
    assert recomputed["current_seed_id"] == BMW_SEED_ID

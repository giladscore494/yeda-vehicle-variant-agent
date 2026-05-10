"""Tests for Batch Runner needs_retry priority fix.

Verifies that:
1. When needs_retry_seed_ids has seeds, run_next_batch starts from needs_retry[0].
2. Normal checkpoint (next_seed_id) is ignored while needs_retry is non-empty.
3. Resolved retry seed is removed from needs_retry_seed_ids and added to processed_seed_ids.
4. Unresolved retry seed is not silently marked processed.
5. After needs_retry is empty, runner resumes normal next_seed_id (e.g. haval__h6__2022__2026__il).
6. evaluate_continue_guard exposes needs_retry_required=True when needs_retry is non-empty.
7. get_batch_progress includes retry_queue_count, current_retry_seed, remaining_retry_seeds,
   and normal_next_seed fields.

No Gemini calls are made; all agent calls are monkeypatched.
"""
from __future__ import annotations

import copy
import pytest

from agent import batch_runner


# ---------------------------------------------------------------------------
# Shared seed fixtures
# ---------------------------------------------------------------------------

BMW = {
    "seed_id": "bmw__850i__2018__2026__il",
    "make": "BMW",
    "model": "850i",
    "year_start": 2018,
    "year_end": 2026,
    "market": "IL",
}
JAGUAR = {
    "seed_id": "jaguar__f-type__2013__2024__il",
    "make": "Jaguar",
    "model": "F-Type",
    "year_start": 2013,
    "year_end": 2024,
    "market": "IL",
}
HAVAL = {
    "seed_id": "haval__h6__2022__2026__il",
    "make": "Haval",
    "model": "H6",
    "year_start": 2022,
    "year_end": 2026,
    "market": "IL",
}

ORDERED = [BMW, JAGUAR, HAVAL]


def _base_state(processed=None, needs_retry=None, next_seed=None, failed=None):
    """Build a minimal batch state dict."""
    return {
        "schema_version": batch_runner.BATCH_STATE_SCHEMA,
        "market": "IL",
        "processed_seed_ids": list(processed or []),
        "failed_seed_ids": list(failed or []),
        "failed_details": [],
        "needs_retry_seed_ids": list(needs_retry or []),
        "last_completed_seed_id": processed[-1] if processed else None,
        "next_seed_id": next_seed,
        "in_progress_seed_id": None,
    }


def _guard_pass():
    """Return a guard result that allows the batch to run."""
    return {
        "passed": True,
        "issues": [],
        "repair_required": False,
        "needs_retry_required": False,
        "false_processed_seeds": [],
        "coverage_audit": {"holes_count": 0, "missing_seeds": []},
    }


def _patch_run_next_batch(monkeypatch, state, guard=None, extra_monkeypatches=None):
    """Apply standard monkeypatches for run_next_batch tests."""
    monkeypatch.setattr(batch_runner, "evaluate_continue_guard", lambda market="IL": guard or _guard_pass())
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": ORDERED)
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": copy.deepcopy(state))
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {
        "run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []
    })
    saved_states = []
    monkeypatch.setattr(batch_runner, "_save_state", lambda s: saved_states.append(copy.deepcopy(s)))
    monkeypatch.setattr(batch_runner, "_refresh_coverage", lambda s, o: None)
    monkeypatch.setattr(batch_runner, "save_json", lambda *a, **kw: None)
    monkeypatch.setattr(batch_runner, "persist_canonical_resume_package", lambda **kw: {"ok": True})
    monkeypatch.setattr(batch_runner, "load_local_canonical_resume_package", lambda: None)
    if extra_monkeypatches:
        for attr, val in extra_monkeypatches.items():
            monkeypatch.setattr(batch_runner, attr, val)
    return saved_states


# ---------------------------------------------------------------------------
# Test 1: when needs_retry has seeds, batch runner starts from needs_retry[0]
# ---------------------------------------------------------------------------

def test_batch_runner_starts_from_needs_retry_not_checkpoint(monkeypatch):
    """Batch runner must start from needs_retry[0], not next_seed_id, when retry queue is non-empty."""
    # BMW is in needs_retry; Jaguar is the "normal" next_seed checkpoint.
    state = _base_state(
        processed=[],
        needs_retry=[BMW["seed_id"]],
        next_seed=JAGUAR["seed_id"],
    )
    called = {}

    def _fake_run(make, model, *args, **kwargs):
        called["make"] = make
        called["model"] = model
        return {"status": "completed", "variants_created": 2}

    saved = _patch_run_next_batch(monkeypatch, state, extra_monkeypatches={"run_single_model": _fake_run})
    result = batch_runner.run_next_batch(limit=1, market="IL", resume=True)

    assert result.get("status") == "completed", result
    assert result.get("batch_mode") == "needs_retry", (
        f"Expected batch_mode='needs_retry', got '{result.get('batch_mode')}'"
    )
    assert called.get("make") == "BMW", (
        f"Expected BMW to be processed first, got '{called.get('make')}'"
    )
    assert result["queue_diagnostics"]["first_seed"] == BMW["seed_id"]


# ---------------------------------------------------------------------------
# Test 2: normal checkpoint is ignored while needs_retry is non-empty
# ---------------------------------------------------------------------------

def test_checkpoint_next_seed_ignored_while_needs_retry_nonempty(monkeypatch):
    """Jaguar's next_seed_id checkpoint must not be used while BMW is in needs_retry."""
    state = _base_state(
        processed=[],
        needs_retry=[BMW["seed_id"]],
        next_seed=JAGUAR["seed_id"],
    )
    processed_seeds_in_batch = []

    def _fake_run(make, model, *args, **kwargs):
        processed_seeds_in_batch.append(make)
        return {"status": "completed", "variants_created": 2}

    _patch_run_next_batch(monkeypatch, state, extra_monkeypatches={"run_single_model": _fake_run})
    result = batch_runner.run_next_batch(limit=2, market="IL", resume=True)

    assert JAGUAR["make"] not in processed_seeds_in_batch, (
        "Jaguar (normal next_seed) must not be processed while BMW is in needs_retry"
    )
    assert BMW["make"] in processed_seeds_in_batch


# ---------------------------------------------------------------------------
# Test 3: resolved retry seed is removed from needs_retry, added to processed
# ---------------------------------------------------------------------------

def test_resolved_retry_seed_removed_from_needs_retry(monkeypatch):
    """After a successful run_single_model result, the retry seed leaves needs_retry."""
    state = _base_state(
        processed=[],
        needs_retry=[BMW["seed_id"]],
        next_seed=JAGUAR["seed_id"],
    )
    saved_states = _patch_run_next_batch(
        monkeypatch,
        state,
        extra_monkeypatches={
            "run_single_model": lambda *a, **kw: {"status": "completed", "variants_created": 2}
        },
    )
    batch_runner.run_next_batch(limit=1, market="IL", resume=True)

    # Find the last saved state (after seed processing)
    assert saved_states, "Expected at least one state save"
    final_state = saved_states[-1]
    assert BMW["seed_id"] not in final_state.get("needs_retry_seed_ids", []), (
        "BMW should be removed from needs_retry after successful processing"
    )
    assert BMW["seed_id"] in final_state.get("processed_seed_ids", []), (
        "BMW should be added to processed_seed_ids after successful processing"
    )


# ---------------------------------------------------------------------------
# Test 4: unresolved retry seed is not silently marked processed
# ---------------------------------------------------------------------------

def test_unresolved_retry_seed_not_marked_processed(monkeypatch):
    """A seed that fails during retry must not appear in processed_seed_ids."""
    state = _base_state(
        processed=[],
        needs_retry=[BMW["seed_id"]],
        next_seed=JAGUAR["seed_id"],
    )
    # Simulate a needs_retry/error result with no variants
    def _fail_run(*a, **kw):
        return {"status": "needs_retry", "variants_created": 0, "error": "zero_variants_without_no_variants_reason"}

    saved_states = _patch_run_next_batch(
        monkeypatch,
        state,
        extra_monkeypatches={"run_single_model": _fail_run},
    )
    batch_runner.run_next_batch(limit=1, market="IL", resume=True)

    assert saved_states, "Expected at least one state save"
    final_state = saved_states[-1]
    assert BMW["seed_id"] not in final_state.get("processed_seed_ids", []), (
        "Unresolved retry seed must never appear in processed_seed_ids"
    )


# ---------------------------------------------------------------------------
# Test 5: after needs_retry is empty, runner resumes normal next_seed_id
# ---------------------------------------------------------------------------

def test_runner_resumes_normal_next_seed_after_retry_empty(monkeypatch):
    """When needs_retry is empty, the runner must use next_seed_id (Haval) as normal."""
    state = _base_state(
        processed=[BMW["seed_id"], JAGUAR["seed_id"]],
        needs_retry=[],
        next_seed=HAVAL["seed_id"],
    )
    called = {}

    def _fake_run(make, model, *args, **kwargs):
        called["make"] = make
        return {"status": "completed", "variants_created": 2}

    _patch_run_next_batch(monkeypatch, state, extra_monkeypatches={"run_single_model": _fake_run})
    result = batch_runner.run_next_batch(limit=1, market="IL", resume=True)

    assert result.get("batch_mode") != "needs_retry", (
        "batch_mode must not be 'needs_retry' when the retry queue is empty"
    )
    assert called.get("make") == HAVAL["make"], (
        f"Expected Haval (normal next_seed), got '{called.get('make')}'"
    )


# ---------------------------------------------------------------------------
# Test 6: evaluate_continue_guard exposes needs_retry_required
# ---------------------------------------------------------------------------

def test_evaluate_continue_guard_exposes_needs_retry_required(monkeypatch):
    """evaluate_continue_guard must return needs_retry_required=True when needs_retry is non-empty."""
    ordered = [BMW, JAGUAR, HAVAL]
    # Canonical has 1 processed seed with a real variant; local state has BMW in needs_retry.
    canonical = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "processed_seed_ids": [JAGUAR["seed_id"]],
            "last_completed_seed_id": JAGUAR["seed_id"],
            "next_seed_id": HAVAL["seed_id"],
        },
        "accumulated_clean_export": {
            "variants": [
                {
                    "variant_id": "jag_v1",
                    "make": "Jaguar",
                    "model": "F-Type",
                    "year_start": 2013,
                    "year_end": 2024,
                    "market": "IL",
                    "verification_status": "verified",
                }
            ]
        },
    }
    local_bs = {
        "schema_version": batch_runner.BATCH_STATE_SCHEMA,
        "market": "IL",
        "processed_seed_ids": [JAGUAR["seed_id"]],
        "failed_seed_ids": [],
        "needs_retry_seed_ids": [BMW["seed_id"]],
        "last_completed_seed_id": JAGUAR["seed_id"],
        "next_seed_id": HAVAL["seed_id"],
        "in_progress_seed_id": None,
    }

    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(batch_runner, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": copy.deepcopy(local_bs))
    monkeypatch.setattr(batch_runner, "_save_state", lambda s: None)
    monkeypatch.setattr(batch_runner, "_batch_state_path", lambda: type("P", (), {"exists": lambda self: True})())
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {
        "run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []
    })
    monkeypatch.setattr(batch_runner, "fetch_file_from_github", lambda path: None)
    monkeypatch.setattr(batch_runner, "get_github_config", lambda: {"canonical_path": "canonical.json"})

    guard = batch_runner.evaluate_continue_guard(market="IL")

    assert guard.get("needs_retry_required") is True, (
        f"Expected needs_retry_required=True, got {guard.get('needs_retry_required')}"
    )
    assert guard.get("needs_retry_count") == 1
    assert BMW["seed_id"] in guard.get("needs_retry_seed_ids", [])


# ---------------------------------------------------------------------------
# Test 7: get_batch_progress includes retry queue fields
# ---------------------------------------------------------------------------

def test_get_batch_progress_includes_retry_fields(monkeypatch):
    """get_batch_progress must include retry_queue_count, current_retry_seed,
    remaining_retry_seeds, and normal_next_seed."""
    ordered = [BMW, JAGUAR, HAVAL]
    canonical = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "processed_seed_ids": [JAGUAR["seed_id"]],
            "last_completed_seed_id": JAGUAR["seed_id"],
            "needs_retry_seed_ids": [BMW["seed_id"]],
            "next_seed_id": HAVAL["seed_id"],
        },
        "accumulated_clean_export": {"variants": []},
    }
    local_bs = {
        "schema_version": batch_runner.BATCH_STATE_SCHEMA,
        "market": "IL",
        "processed_seed_ids": [JAGUAR["seed_id"]],
        "failed_seed_ids": [],
        "needs_retry_seed_ids": [BMW["seed_id"]],
        "last_completed_seed_id": JAGUAR["seed_id"],
        "next_seed_id": HAVAL["seed_id"],
        "in_progress_seed_id": None,
    }

    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(batch_runner, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": copy.deepcopy(local_bs))
    monkeypatch.setattr(batch_runner, "_save_state", lambda s: None)
    monkeypatch.setattr(batch_runner, "_refresh_coverage", lambda s, o: None)
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {
        "run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []
    })

    progress = batch_runner.get_batch_progress(market="IL")

    assert "retry_queue_count" in progress, "retry_queue_count must be present in get_batch_progress"
    assert "current_retry_seed" in progress, "current_retry_seed must be present"
    assert "remaining_retry_seeds" in progress, "remaining_retry_seeds must be present"
    assert "normal_next_seed" in progress, "normal_next_seed must be present"

    assert progress["retry_queue_count"] == 1
    assert progress["current_retry_seed"] == BMW["seed_id"]
    assert progress["remaining_retry_seeds"] == []


# ---------------------------------------------------------------------------
# Test 8: needs_retry_required allows batch to run even when guard.passed is False
# ---------------------------------------------------------------------------

def test_needs_retry_required_bypasses_guard_block(monkeypatch):
    """When needs_retry_required=True the batch must not be blocked even if guard.passed=False."""
    state = _base_state(
        processed=[],
        needs_retry=[BMW["seed_id"]],
        next_seed=JAGUAR["seed_id"],
    )
    guard = {
        "passed": False,
        "issues": ["some_non_repair_issue"],
        "repair_required": False,
        "needs_retry_required": True,
        "false_processed_seeds": [],
        "coverage_audit": {"holes_count": 0, "missing_seeds": []},
        "needs_retry_seed_ids": [BMW["seed_id"]],
    }
    _patch_run_next_batch(
        monkeypatch,
        state,
        guard=guard,
        extra_monkeypatches={
            "run_single_model": lambda *a, **kw: {"status": "completed", "variants_created": 2}
        },
    )
    result = batch_runner.run_next_batch(limit=1, market="IL", resume=True)

    assert result.get("status") != "blocked", (
        "Batch must not be blocked when needs_retry_required=True, even if guard.passed=False"
    )
    assert result.get("batch_mode") == "needs_retry"

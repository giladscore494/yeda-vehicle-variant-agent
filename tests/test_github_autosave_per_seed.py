"""Tests for per-seed GitHub auto-save feature.

Covers:
1. test_single_seed_local_save_after_completion
2. test_single_seed_github_push_enabled
3. test_single_seed_github_push_disabled
4. test_failed_seed_does_not_push
5. test_partial_batch_saves_each_completed_seed
6. test_no_commit_when_no_content_changed
7. test_coverage_metadata_stays_synced_after_each_seed
8. test_bmw_regression_no_zero_coverage
"""
from __future__ import annotations

import copy

from agent import batch_runner


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _seed(seed_id: str, make: str, model: str, year_start: int = 2000, year_end: int = 2026) -> dict:
    return {
        "seed_id": seed_id,
        "make": make,
        "model": model,
        "year_start": year_start,
        "year_end": year_end,
        "market": "IL",
    }


def _variant(idx: int, make: str = "BMW", model: str = "520d", status: str = "verified") -> dict:
    return {
        "variant_id": f"{make.lower()}-v{idx}",
        "make": make,
        "model": model,
        "market": "IL",
        "year_start": 2008,
        "year_end": 2020,
        "generation": "g1",
        "body_type": {"value": "Sedan", "status": status, "sources_count": 2, "source_ids": ["s1"]},
        "seats": {"value": 5, "status": "partial", "sources_count": 1, "source_ids": ["s1"]},
        "engine": {"value": f"e{idx}", "status": status, "sources_count": 2, "source_ids": ["s1"]},
        "transmission": {"value": "AT", "status": "partial", "sources_count": 1, "source_ids": ["s1"]},
        "fuel_type": {"value": "Diesel", "status": "partial", "sources_count": 1, "source_ids": ["s1"]},
        "drivetrain": {"value": "RWD", "status": "partial", "sources_count": 1, "source_ids": ["s1"]},
        "trim": {"value": f"trim-{idx}", "status": status, "sources_count": 1, "source_ids": ["s1"]},
        "verification_status": status,
        "classification": status,
    }


def _base_initial_state(seeds: list[dict]) -> dict:
    return {
        "market": "IL",
        "processed_seed_ids": [],
        "failed_seed_ids": [],
        "failed_details": [],
        "in_progress_seed_id": None,
        "last_completed_seed_id": None,
        "next_seed_id": seeds[0]["seed_id"] if seeds else None,
        "coverage_by_make": {},
        "schema_version": "batch_state_v2",
        "total_seeds": len(seeds),
    }


def _build_previous_canonical(seeds: list[dict], num_variants: int = 3, processed_ids: list[str] | None = None) -> dict:
    variants = [_variant(i) for i in range(num_variants)]
    processed = processed_ids or []
    return {
        "schema_version": "resume_package_v1",
        "accumulated_clean_export": {"variants": variants, "quality_gate": {"passed": True}},
        "batch_state": {
            "processed_seed_ids": processed,
            "last_completed_seed_id": processed[-1] if processed else None,
            "next_seed_id": seeds[len(processed)]["seed_id"] if len(processed) < len(seeds) else None,
            "failed_seed_ids": [],
            "coverage_by_make": {},
        },
        "verified_variants": [v for v in variants if v.get("verification_status") == "verified"],
        "partial_variants": [v for v in variants if v.get("verification_status") != "verified"],
    }


def _wire_persist_env(monkeypatch, seeds, previous_canonical, push_calls: list, saved_canonicals: list, push_ok: bool = True):
    """Wire up all I/O dependencies needed by persist_canonical_after_seed."""
    ordered_seeds = list(seeds)
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": ordered_seeds)
    monkeypatch.setattr(batch_runner, "load_local_canonical_resume_package", lambda: copy.deepcopy(previous_canonical))
    monkeypatch.setattr(batch_runner, "fetch_file_from_github", lambda *a, **kw: None)
    monkeypatch.setattr(batch_runner, "get_github_config", lambda: {"canonical_path": "data/canonical/resume_package_canonical.json", "backup_path": "data/canonical/resume_package_backup_previous.json"})
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(batch_runner, "load_imported_accumulated_variants", lambda: [])
    monkeypatch.setattr(batch_runner, "load_json_object", lambda *a, **kw: {})
    monkeypatch.setattr(batch_runner, "load_json_list", lambda *a, **kw: [])
    monkeypatch.setattr(batch_runner, "assert_no_mock_in_final_export", lambda *a, **kw: None)

    def _fake_save_canonical(pkg):
        saved_canonicals.append(copy.deepcopy(pkg))

    def _fake_save_backup(pkg):
        pass

    monkeypatch.setattr(batch_runner, "save_local_canonical_resume_package", _fake_save_canonical)
    monkeypatch.setattr(batch_runner, "save_local_canonical_backup", _fake_save_backup)
    monkeypatch.setattr(batch_runner, "save_json", lambda *a, **kw: None)
    monkeypatch.setattr(batch_runner, "_validate_saved_canonical", lambda *a, **kw: {"ok": True, "issues": []})
    monkeypatch.setattr(batch_runner, "_set_last_canonical_update_attempt", lambda *a, **kw: None)

    def _fake_push(package, previous_package=None, commit_message=None, batch_id=None):
        push_calls.append({"commit_message": commit_message, "package": package})
        if push_ok:
            return {"ok": True, "canonical": {"commit_sha": "abc123"}}
        return {"ok": False, "error": "Simulated GitHub push failure"}

    monkeypatch.setattr(batch_runner, "push_canonical_resume_package", _fake_push)


# ---------------------------------------------------------------------------
# 1. test_single_seed_local_save_after_completion
# ---------------------------------------------------------------------------

def test_single_seed_local_save_after_completion(monkeypatch):
    """After one successful seed, canonical must be locally saved with updated state."""
    seeds = [
        _seed("bmw__518i__1990__1996__il", "BMW", "518i", 1990, 1996),
        _seed("bmw__520d__2008__2020__il", "BMW", "520d", 2008, 2020),
    ]
    previous = _build_previous_canonical(seeds, num_variants=3, processed_ids=["bmw__518i__1990__1996__il"])
    push_calls: list = []
    saved: list = []
    _wire_persist_env(monkeypatch, seeds, previous, push_calls, saved)

    seed = seeds[1]  # 520d is next
    state = {
        "market": "IL",
        "processed_seed_ids": ["bmw__518i__1990__1996__il", "bmw__520d__2008__2020__il"],
        "last_completed_seed_id": "bmw__520d__2008__2020__il",
        "next_seed_id": None,
        "failed_seed_ids": [],
    }
    result = batch_runner.persist_canonical_after_seed(
        seed=seed,
        batch_state=state,
        push_to_github=False,
        market="IL",
    )

    assert result["ok"] is True, f"Expected ok=True, got: {result}"
    assert result["local_saved"] is True
    assert result["github_push_failed"] is False
    assert len(saved) >= 1, "save_local_canonical_resume_package must be called"

    saved_pkg = saved[-1]
    saved_bs = saved_pkg.get("batch_state") or {}
    assert "bmw__520d__2008__2020__il" in (saved_bs.get("processed_seed_ids") or []), \
        "Saved canonical must include the completed seed in processed_seed_ids"
    assert saved_bs.get("last_completed_seed_id") == "bmw__520d__2008__2020__il"
    assert len(push_calls) == 0, "No GitHub push when push_to_github=False"


# ---------------------------------------------------------------------------
# 2. test_single_seed_github_push_enabled
# ---------------------------------------------------------------------------

def test_single_seed_github_push_enabled(monkeypatch):
    """With push_to_github=True, push must be called exactly once with make/model in message."""
    seeds = [
        _seed("bmw__518i__1990__1996__il", "BMW", "518i", 1990, 1996),
        _seed("bmw__520d__2008__2020__il", "BMW", "520d", 2008, 2020),
    ]
    previous = _build_previous_canonical(seeds, num_variants=3, processed_ids=["bmw__518i__1990__1996__il"])
    push_calls: list = []
    saved: list = []
    _wire_persist_env(monkeypatch, seeds, previous, push_calls, saved)

    seed = seeds[1]
    state = {
        "market": "IL",
        "processed_seed_ids": ["bmw__518i__1990__1996__il", "bmw__520d__2008__2020__il"],
        "last_completed_seed_id": "bmw__520d__2008__2020__il",
        "next_seed_id": None,
        "failed_seed_ids": [],
    }
    result = batch_runner.persist_canonical_after_seed(
        seed=seed,
        batch_state=state,
        push_to_github=True,
        commit_message_prefix="Update canonical vehicle variants",
        market="IL",
    )

    assert result["ok"] is True
    assert result["local_saved"] is True
    assert result["github_push_failed"] is False
    assert len(push_calls) == 1, "push must be called exactly once"
    commit_msg = push_calls[0].get("commit_message") or ""
    assert "BMW" in commit_msg, f"Commit message must include make 'BMW', got: {commit_msg!r}"
    assert "520d" in commit_msg, f"Commit message must include model '520d', got: {commit_msg!r}"


# ---------------------------------------------------------------------------
# 3. test_single_seed_github_push_disabled
# ---------------------------------------------------------------------------

def test_single_seed_github_push_disabled(monkeypatch):
    """With push_to_github=False, local canonical must be saved and push must NOT be called."""
    seeds = [_seed("bmw__518i__1990__1996__il", "BMW", "518i", 1990, 1996)]
    previous = _build_previous_canonical(seeds, num_variants=2)
    push_calls: list = []
    saved: list = []
    _wire_persist_env(monkeypatch, seeds, previous, push_calls, saved)

    seed = seeds[0]
    state = {
        "market": "IL",
        "processed_seed_ids": ["bmw__518i__1990__1996__il"],
        "last_completed_seed_id": "bmw__518i__1990__1996__il",
        "next_seed_id": None,
        "failed_seed_ids": [],
    }
    result = batch_runner.persist_canonical_after_seed(
        seed=seed,
        batch_state=state,
        push_to_github=False,
        market="IL",
    )

    assert result["ok"] is True
    assert len(saved) >= 1, "Local canonical must be saved"
    assert len(push_calls) == 0, "push must NOT be called when push_to_github=False"


# ---------------------------------------------------------------------------
# 4. test_failed_seed_does_not_push
# ---------------------------------------------------------------------------

def test_failed_seed_does_not_push(monkeypatch):
    """persist_canonical_after_seed must NOT be called for a failed seed.

    The guard is in _process_seeds: it only calls persist_canonical_after_seed
    when status is 'completed' or 'partial'.  We test this by directly checking
    that a fake run_next_batch with one failing seed produces no per_seed_canonical.
    """
    seeds = [
        _seed("bmw__518i__1990__1996__il", "BMW", "518i", 1990, 1996),
        _seed("bmw__520d__2008__2020__il", "BMW", "520d", 2008, 2020),
    ]
    initial_state = _base_initial_state(seeds)
    per_seed_calls: list = []

    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(batch_runner, "load_local_canonical_resume_package", lambda: None)
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": dict(initial_state))
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(batch_runner, "_save_state", lambda state: None)
    monkeypatch.setattr(batch_runner, "_refresh_coverage", lambda state, ordered: None)
    monkeypatch.setattr(batch_runner, "save_json", lambda *a, **kw: None)
    monkeypatch.setattr(batch_runner, "evaluate_continue_guard", lambda market="IL": {"passed": True, "issues": [], "coverage_audit": {"holes_count": 0}})
    # Simulate a failing run
    monkeypatch.setattr(batch_runner, "run_single_model", lambda *a, **kw: {"status": "error", "error": "Simulated failure"})
    monkeypatch.setattr(batch_runner, "persist_canonical_resume_package", lambda *a, **kw: {"ok": True})

    def _track_per_seed(*a, **kw):
        per_seed_calls.append(kw)
        return {"ok": True, "local_saved": True, "github_push_failed": False, "push_result": None}

    monkeypatch.setattr(batch_runner, "persist_canonical_after_seed", _track_per_seed)

    result = batch_runner.run_next_batch(limit=1, market="IL", resume=False, auto_push_per_seed=True)

    assert result["status"] == "completed"
    assert len(per_seed_calls) == 0, "persist_canonical_after_seed must NOT be called for a failed seed"
    assert result.get("per_seed_canonical") == []


# ---------------------------------------------------------------------------
# 5. test_partial_batch_saves_each_completed_seed
# ---------------------------------------------------------------------------

def test_partial_batch_saves_each_completed_seed(monkeypatch):
    """In a batch of 3 where seeds 1 and 2 complete and seed 3 fails,
    persist_canonical_after_seed must be called exactly twice (once per success)
    and GitHub push must be called exactly twice when auto_push_per_seed=True.
    """
    seeds = [
        _seed("bmw__518i__1990__1996__il", "BMW", "518i"),
        _seed("bmw__520d__2008__2020__il", "BMW", "520d"),
        _seed("bmw__530d__2019__2023__il", "BMW", "530d"),
    ]
    initial_state = _base_initial_state(seeds)
    per_seed_calls: list = []

    call_counter = {"n": 0}

    def _run_single_model(make, model, *a, **kw):
        call_counter["n"] += 1
        if call_counter["n"] <= 2:
            return {"status": "completed", "variants_created": 2}
        return {"status": "error", "error": "Third seed failed"}

    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(batch_runner, "load_local_canonical_resume_package", lambda: None)
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": dict(initial_state))
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(batch_runner, "_save_state", lambda state: None)
    monkeypatch.setattr(batch_runner, "_refresh_coverage", lambda state, ordered: None)
    monkeypatch.setattr(batch_runner, "save_json", lambda *a, **kw: None)
    monkeypatch.setattr(batch_runner, "evaluate_continue_guard", lambda market="IL": {"passed": True, "issues": [], "coverage_audit": {"holes_count": 0}})
    monkeypatch.setattr(batch_runner, "run_single_model", _run_single_model)
    monkeypatch.setattr(batch_runner, "persist_canonical_resume_package", lambda *a, **kw: {"ok": True})

    def _track_per_seed(seed, batch_state, push_to_github=False, **kw):
        per_seed_calls.append({"seed_id": seed.get("seed_id"), "push_to_github": push_to_github})
        return {"ok": True, "local_saved": True, "github_push_failed": False, "push_result": {"ok": True} if push_to_github else None}

    monkeypatch.setattr(batch_runner, "persist_canonical_after_seed", _track_per_seed)

    result = batch_runner.run_next_batch(limit=3, market="IL", resume=False, auto_push_per_seed=True)

    assert result["status"] == "completed"
    assert len(per_seed_calls) == 2, f"Expected 2 per-seed calls, got {len(per_seed_calls)}: {per_seed_calls}"
    assert per_seed_calls[0]["seed_id"] == "bmw__518i__1990__1996__il"
    assert per_seed_calls[1]["seed_id"] == "bmw__520d__2008__2020__il"
    assert all(c["push_to_github"] for c in per_seed_calls), "GitHub push must be True for both completed seeds"

    per_seed_canonical = result.get("per_seed_canonical", [])
    assert len(per_seed_canonical) == 2
    completed_ids = [p["seed_id"] for p in per_seed_canonical]
    assert "bmw__530d__2019__2023__il" not in completed_ids, "Failed seed must not appear in per_seed_canonical"


# ---------------------------------------------------------------------------
# 6. test_no_commit_when_no_content_changed
# ---------------------------------------------------------------------------

def test_no_commit_when_no_content_changed(monkeypatch):
    """When all seeds in the queue are already processed, run_next_batch returns
    completed_all and persist_canonical_after_seed must NOT be called at all.
    """
    seeds = [_seed("bmw__518i__1990__1996__il", "BMW", "518i")]
    # All seeds already processed
    initial_state = {
        "market": "IL",
        "processed_seed_ids": ["bmw__518i__1990__1996__il"],
        "failed_seed_ids": [],
        "failed_details": [],
        "in_progress_seed_id": None,
        "last_completed_seed_id": "bmw__518i__1990__1996__il",
        "next_seed_id": None,
    }
    per_seed_calls: list = []

    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(batch_runner, "load_local_canonical_resume_package", lambda: None)
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": dict(initial_state))
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(batch_runner, "_save_state", lambda state: None)
    monkeypatch.setattr(batch_runner, "_refresh_coverage", lambda state, ordered: None)
    monkeypatch.setattr(batch_runner, "save_json", lambda *a, **kw: None)
    monkeypatch.setattr(batch_runner, "evaluate_continue_guard", lambda market="IL": {"passed": True, "issues": [], "coverage_audit": {"holes_count": 0}})

    def _track_per_seed(*a, **kw):
        per_seed_calls.append(kw)
        return {"ok": True}

    monkeypatch.setattr(batch_runner, "persist_canonical_after_seed", _track_per_seed)

    result = batch_runner.run_next_batch(limit=1, market="IL", resume=True, auto_push_per_seed=True)

    assert result["status"] == "completed_all", f"Expected completed_all, got {result['status']}"
    assert len(per_seed_calls) == 0, "No per-seed persist should happen when all seeds are already processed"


# ---------------------------------------------------------------------------
# 7. test_coverage_metadata_stays_synced_after_each_seed
# ---------------------------------------------------------------------------

def test_coverage_metadata_stays_synced_after_each_seed(monkeypatch):
    """After persist_canonical_after_seed, coverage_by_make counts must match
    accumulated_clean_export.variants grouped by make.
    """
    seeds = [
        _seed("bmw__518i__1990__1996__il", "BMW", "518i"),
        _seed("bmw__520d__2008__2020__il", "BMW", "520d"),
    ]
    # Previous canonical: 2 verified BMW variants
    previous_variants = [_variant(1, "BMW", "518i", "verified"), _variant(2, "BMW", "518i", "partial")]
    previous = {
        "schema_version": "resume_package_v1",
        "accumulated_clean_export": {"variants": previous_variants, "quality_gate": {"passed": True}},
        "batch_state": {
            "processed_seed_ids": ["bmw__518i__1990__1996__il"],
            "last_completed_seed_id": "bmw__518i__1990__1996__il",
            "next_seed_id": "bmw__520d__2008__2020__il",
            "failed_seed_ids": [],
            "coverage_by_make": {"BMW": {"total": 2, "processed": 1, "verified_variants": 1, "partial_variants": 1, "failed": 0, "unresolved": 0, "completed": False}},
        },
        "verified_variants": [v for v in previous_variants if v.get("verification_status") == "verified"],
        "partial_variants": [v for v in previous_variants if v.get("verification_status") != "verified"],
    }

    # New variants that would be added after processing 520d
    new_variants = previous_variants + [_variant(3, "BMW", "520d", "verified"), _variant(4, "BMW", "520d", "partial")]
    push_calls: list = []
    saved: list = []
    _wire_persist_env(monkeypatch, seeds, previous, push_calls, saved)

    # Override build_final_export to return known variants
    monkeypatch.setattr(batch_runner, "build_final_export", lambda *a, **kw: {
        "variants": new_variants,
        "quality_gate": {"passed": True},
        "audit": {},
        "counts": {},
    })

    seed = seeds[1]
    state = {
        "market": "IL",
        "processed_seed_ids": ["bmw__518i__1990__1996__il", "bmw__520d__2008__2020__il"],
        "last_completed_seed_id": "bmw__520d__2008__2020__il",
        "next_seed_id": None,
        "failed_seed_ids": [],
    }
    result = batch_runner.persist_canonical_after_seed(
        seed=seed,
        batch_state=state,
        push_to_github=False,
        market="IL",
    )

    assert result["ok"] is True
    assert len(saved) >= 1

    saved_pkg = saved[-1]
    saved_bs = saved_pkg.get("batch_state") or {}
    coverage = saved_bs.get("coverage_by_make") or {}

    # Compute expected counts from saved accumulated variants
    saved_acc = saved_pkg.get("accumulated_clean_export") or {}
    saved_variants = [v for v in (saved_acc.get("variants") or []) if isinstance(v, dict)]
    expected_verified = sum(1 for v in saved_variants if str(v.get("make") or "").strip() == "BMW" and v.get("verification_status") == "verified")
    expected_partial = sum(1 for v in saved_variants if str(v.get("make") or "").strip() == "BMW" and v.get("verification_status") != "verified")

    bmw_cov = coverage.get("BMW") or {}
    assert bmw_cov.get("verified_variants") == expected_verified, (
        f"coverage_by_make BMW verified_variants={bmw_cov.get('verified_variants')} "
        f"must equal accumulated count={expected_verified}"
    )
    assert bmw_cov.get("partial_variants") == expected_partial, (
        f"coverage_by_make BMW partial_variants={bmw_cov.get('partial_variants')} "
        f"must equal accumulated count={expected_partial}"
    )


# ---------------------------------------------------------------------------
# 8. test_bmw_regression_no_zero_coverage
# ---------------------------------------------------------------------------

def test_bmw_regression_no_zero_coverage(monkeypatch):
    """After saving a package with BMW variants, coverage_by_make['BMW'] must NOT
    show both verified_variants=0 and partial_variants=0.
    """
    seeds = [
        _seed("bmw__518i__1990__1996__il", "BMW", "518i"),
        _seed("bmw__520d__2008__2020__il", "BMW", "520d"),
    ]
    bmw_variants = [
        _variant(1, "BMW", "520d", "verified"),
        _variant(2, "BMW", "520d", "verified"),
        _variant(3, "BMW", "520d", "partial"),
    ]
    previous = {
        "schema_version": "resume_package_v1",
        "accumulated_clean_export": {"variants": bmw_variants, "quality_gate": {"passed": True}},
        "batch_state": {
            "processed_seed_ids": ["bmw__518i__1990__1996__il"],
            "last_completed_seed_id": "bmw__518i__1990__1996__il",
            "next_seed_id": "bmw__520d__2008__2020__il",
            "failed_seed_ids": [],
            "coverage_by_make": {
                "BMW": {"total": 2, "processed": 1, "verified_variants": 0, "partial_variants": 0, "failed": 0, "unresolved": 0, "completed": False}
            },
        },
        "verified_variants": [],
        "partial_variants": [],
    }
    push_calls: list = []
    saved: list = []
    _wire_persist_env(monkeypatch, seeds, previous, push_calls, saved)

    # build_final_export returns the same BMW variants
    monkeypatch.setattr(batch_runner, "build_final_export", lambda *a, **kw: {
        "variants": bmw_variants,
        "quality_gate": {"passed": True},
        "audit": {},
        "counts": {},
    })

    seed = seeds[1]
    state = {
        "market": "IL",
        "processed_seed_ids": ["bmw__518i__1990__1996__il", "bmw__520d__2008__2020__il"],
        "last_completed_seed_id": "bmw__520d__2008__2020__il",
        "next_seed_id": None,
        "failed_seed_ids": [],
    }
    result = batch_runner.persist_canonical_after_seed(
        seed=seed,
        batch_state=state,
        push_to_github=False,
        market="IL",
    )

    assert result["ok"] is True
    saved_pkg = saved[-1]
    saved_bs = saved_pkg.get("batch_state") or {}
    coverage = saved_bs.get("coverage_by_make") or {}
    bmw_cov = coverage.get("BMW") or {}

    assert not (bmw_cov.get("verified_variants", 0) == 0 and bmw_cov.get("partial_variants", 0) == 0), (
        "coverage_by_make BMW must NOT have both verified_variants and partial_variants equal to 0 "
        f"when BMW variants exist. Got: {bmw_cov}"
    )
    assert bmw_cov.get("verified_variants", 0) == 2
    assert bmw_cov.get("partial_variants", 0) == 1

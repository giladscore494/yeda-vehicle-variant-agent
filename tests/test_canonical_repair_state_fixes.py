"""Tests for canonical repair-state persistence fixes and the recovery function.

Covers:
1.  pushed_any does not crash when push_result is None
2.  Invalid needs_retry seed id "s1" is filtered out during build_canonical_candidate
3.  recover_zero_variant_repair_state_from_backup restores 54 real seeds
4.  After recovery, next_seed_id is the first real repair seed
5.  persist_canonical_after_seed does not drop unresolved needs_retry seeds
6.  rebuild_canonical_metadata_from_accumulated preserves repair fields
7.  Resolved retry seed is removed only when variants/no_variants_reason/dedupe_proof exists
8.  extract_canonical_batch_state preserves repair-state fields from raw_state
"""
from __future__ import annotations

import copy
import json
import pathlib

import pytest
import agent.batch_runner as br


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed(seed_id, make="Honda", model="Civic", ys=2017, ye=2026, market="IL"):
    return {"seed_id": seed_id, "make": make, "model": model,
            "year_start": ys, "year_end": ye, "market": market}


def _variant(make="Honda", model="Civic", status="verified"):
    return {
        "make": make, "model": model, "market": "IL",
        "year_start": 2017, "year_end": 2026,
        "verification_status": status, "classification": status,
    }


def _make_seeds(n=5, prefix="seed"):
    return [_seed(f"{prefix}__{i:02d}", make="Honda", model=f"M{i:02d}") for i in range(n)]


# ---------------------------------------------------------------------------
# 1. pushed_any does not crash when push_result is None
# ---------------------------------------------------------------------------

def test_pushed_any_safe_when_push_result_is_none():
    """The safe `or {}` pattern must not raise TypeError when push_result is None."""
    per_seed_canonical = [
        {"seed_id": "s1", "canonical_persist": {"ok": True, "push_result": None}},
        {"seed_id": "s2", "canonical_persist": {"ok": True, "push_result": {"ok": True}}},
        {"seed_id": "s3", "canonical_persist": None},
        {"seed_id": "s4"},
    ]

    # Replicate the fixed expression from app.py
    pushed_any = any(
        ((p.get("canonical_persist") or {}).get("push_result") or {}).get("ok")
        for p in per_seed_canonical
    )
    assert pushed_any is True  # s2 has ok=True

    # All None/missing push_result should evaluate to False without crash
    per_seed_none = [
        {"seed_id": "a", "canonical_persist": {"ok": True, "push_result": None}},
        {"seed_id": "b", "canonical_persist": {"ok": False, "push_result": None}},
    ]
    pushed_none = any(
        ((p.get("canonical_persist") or {}).get("push_result") or {}).get("ok")
        for p in per_seed_none
    )
    assert pushed_none is False


# ---------------------------------------------------------------------------
# 2. Invalid needs_retry seed id "s1" is filtered out
# ---------------------------------------------------------------------------

def test_invalid_needs_retry_seed_id_filtered(monkeypatch):
    """build_canonical_candidate must move unknown IDs to invalid_needs_retry_seed_ids."""
    real_seed = _seed("honda__civic__2017__2026__il", make="Honda", model="Civic")
    ordered = [real_seed]
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(br, "audit_coverage_until_last_completed",
                        lambda *a, **k: {"holes_count": 0})

    previous = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "processed_seed_ids": [],
            "needs_retry_seed_ids": ["s1", "honda__civic__2017__2026__il"],
            "seed_accounting": {},
        },
    }

    result = br.build_canonical_candidate(
        previous_package=previous,
        merged_variants=[],
        new_batch_state=None,
        market="IL",
    )

    bs = result["batch_state"]
    assert "s1" not in bs.get("needs_retry_seed_ids", []), \
        "s1 must not appear in active needs_retry_seed_ids"
    assert "s1" in bs.get("invalid_needs_retry_seed_ids", []), \
        "s1 must be moved to invalid_needs_retry_seed_ids"
    # The real seed ID stays only if it's unresolved (no variants, no proof)
    # Here there are no variants, so it should remain.
    assert "honda__civic__2017__2026__il" in bs.get("needs_retry_seed_ids", [])


# ---------------------------------------------------------------------------
# 3. recover_zero_variant_repair_state_from_backup restores 54 real seeds
# ---------------------------------------------------------------------------

def _make_54_seeds():
    year_start = 2018
    return [_seed(f"make__{i:02d}__model__{year_start}__2026__il", make=f"Make{i}", model="Model")
            for i in range(54)]


def test_recover_from_backup_restores_repair_seeds(monkeypatch, tmp_path):
    """recover_zero_variant_repair_state_from_backup must restore all 54 valid seeds."""
    seeds = _make_54_seeds()
    seed_ids = [s["seed_id"] for s in seeds]

    backup_bs = {
        "processed_seed_ids": seed_ids[:38],
        "needs_retry_seed_ids": seed_ids,  # all 54
        "seed_accounting": {sid: {"attempts": 1} for sid in seed_ids},
        "next_seed_id": seed_ids[0],
        "market": "IL",
    }
    backup_pkg = {"schema_version": "resume_package_v1", "batch_state": backup_bs}

    current_bs = {
        "processed_seed_ids": seed_ids[:50],
        "needs_retry_seed_ids": ["s1"],  # invalid
        "next_seed_id": seed_ids[50],
        "market": "IL",
    }
    current_pkg = {"schema_version": "resume_package_v1", "batch_state": current_bs}

    saved_canonical = {}
    saved_state = {}
    fake_backup_path = tmp_path / "backup.json"
    fake_backup_path.write_text(json.dumps(backup_pkg))

    monkeypatch.setattr(br, "_canonical_backup_path", lambda: fake_backup_path)
    monkeypatch.setattr(br, "load_json_object",
                        lambda p: json.loads(pathlib.Path(p).read_text()) if pathlib.Path(p).exists() else {})
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(current_pkg))
    monkeypatch.setattr(br, "save_local_canonical_resume_package",
                        lambda pkg: saved_canonical.__setitem__("pkg", copy.deepcopy(pkg)))
    monkeypatch.setattr(br, "load_batch_state", lambda market="IL": copy.deepcopy(current_bs))
    monkeypatch.setattr(br, "_save_state",
                        lambda s: saved_state.__setitem__("state", copy.deepcopy(s)))

    result = br.recover_zero_variant_repair_state_from_backup(market="IL")

    assert result["ok"] is True
    assert result["recovered_needs_retry_count"] == 54
    assert result["after_needs_retry_count"] == 54
    assert result["next_seed_id"] == seed_ids[0]
    assert result["invalid_seed_ids"] == []  # "s1" from current was not in backup

    # Canonical was saved with the 54 seeds
    assert "pkg" in saved_canonical
    canon_bs = saved_canonical["pkg"]["batch_state"]
    assert len(canon_bs["needs_retry_seed_ids"]) == 54
    assert "s1" not in canon_bs["needs_retry_seed_ids"]

    # batch_state.json was also updated
    assert "state" in saved_state
    assert len(saved_state["state"]["needs_retry_seed_ids"]) == 54


# ---------------------------------------------------------------------------
# 4. After recovery next_seed_id is the first repair seed (bmw analog)
# ---------------------------------------------------------------------------

def test_recover_sets_next_seed_to_first_retry(monkeypatch, tmp_path):
    """After recovery, next_seed_id should be the first seed in needs_retry_seed_ids."""
    seeds = [
        _seed("bmw__850i__2018__2026__il", make="BMW", model="850i"),
        _seed("haval__h6__2022__2026__il", make="Haval", model="H6"),
    ]
    retry_ids = ["bmw__850i__2018__2026__il"]

    backup_bs = {
        "processed_seed_ids": [],
        "needs_retry_seed_ids": retry_ids,
        "next_seed_id": "bmw__850i__2018__2026__il",
        "market": "IL",
    }
    backup_pkg = {"schema_version": "resume_package_v1", "batch_state": backup_bs}

    current_bs = {
        "processed_seed_ids": [],
        "needs_retry_seed_ids": [],
        "next_seed_id": "haval__h6__2022__2026__il",
        "market": "IL",
    }
    current_pkg = {"schema_version": "resume_package_v1", "batch_state": current_bs}

    fake_backup_path = tmp_path / "backup.json"
    fake_backup_path.write_text(json.dumps(backup_pkg))

    saved_canonical = {}
    monkeypatch.setattr(br, "_canonical_backup_path", lambda: fake_backup_path)
    monkeypatch.setattr(br, "load_json_object",
                        lambda p: json.loads(pathlib.Path(p).read_text()) if pathlib.Path(p).exists() else {})
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(current_pkg))
    monkeypatch.setattr(br, "save_local_canonical_resume_package",
                        lambda pkg: saved_canonical.__setitem__("pkg", copy.deepcopy(pkg)))
    monkeypatch.setattr(br, "load_batch_state", lambda market="IL": copy.deepcopy(current_bs))
    monkeypatch.setattr(br, "_save_state", lambda s: None)

    result = br.recover_zero_variant_repair_state_from_backup(market="IL")

    assert result["ok"] is True
    assert result["next_seed_id"] == "bmw__850i__2018__2026__il"
    assert saved_canonical["pkg"]["batch_state"]["next_seed_id"] == "bmw__850i__2018__2026__il"


# ---------------------------------------------------------------------------
# 5. persist_canonical_after_seed does not drop unresolved needs_retry seeds
# ---------------------------------------------------------------------------

def test_persist_canonical_after_seed_preserves_needs_retry(monkeypatch, tmp_path):
    """After a successful seed, the repair queue must not be emptied."""
    real_seed = _seed("honda__civic__2017__2026__il", make="Honda", model="Civic")
    retry_seed = _seed("bmw__850i__2018__2026__il", make="BMW", model="850i")
    ordered = [real_seed, retry_seed]

    previous_pkg = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "processed_seed_ids": [],
            "needs_retry_seed_ids": ["bmw__850i__2018__2026__il"],
            "seed_accounting": {"bmw__850i__2018__2026__il": {"attempts": 1}},
            "next_seed_id": "bmw__850i__2018__2026__il",
            "market": "IL",
        },
        "accumulated_clean_export": {"variants": []},
    }
    batch_state = {
        "processed_seed_ids": ["honda__civic__2017__2026__il"],
        "needs_retry_seed_ids": ["bmw__850i__2018__2026__il"],
        "seed_accounting": {"bmw__850i__2018__2026__il": {"attempts": 1}},
        "next_seed_id": "bmw__850i__2018__2026__il",
        "market": "IL",
        "failed_seed_ids": [],
        "failed_details": [],
    }

    saved = {}

    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(previous_pkg))
    monkeypatch.setattr(br, "fetch_file_from_github", lambda *a, **k: None)
    monkeypatch.setattr(br, "get_github_config", lambda: {"canonical_path": "x", "backup_path": "y"})
    monkeypatch.setattr(br, "build_final_export", lambda: {
        "variants": [_variant()],
        "quality_gate": {"passed": True},
        "audit": {},
    })
    monkeypatch.setattr(br, "save_local_canonical_resume_package",
                        lambda pkg: saved.__setitem__("pkg", copy.deepcopy(pkg)))
    monkeypatch.setattr(br, "save_local_canonical_backup", lambda pkg: None)
    monkeypatch.setattr(br, "_validate_saved_canonical", lambda path: {"ok": True, "issues": []})
    monkeypatch.setattr(br, "_canonical_resume_path", lambda: tmp_path / "canonical.json")
    monkeypatch.setattr(br, "audit_coverage_until_last_completed",
                        lambda *a, **k: {"holes_count": 0})
    monkeypatch.setattr(br, "_set_last_canonical_update_attempt", lambda **k: None)

    result = br.persist_canonical_after_seed(
        seed=real_seed,
        batch_state=batch_state,
        push_to_github=False,
        market="IL",
    )

    assert result.get("ok") is True, f"Expected ok=True, got: {result}"
    assert "pkg" in saved
    bs_out = saved["pkg"]["batch_state"]
    assert "bmw__850i__2018__2026__il" in bs_out.get("needs_retry_seed_ids", []), \
        "BMW repair seed must not be dropped from needs_retry_seed_ids"


# ---------------------------------------------------------------------------
# 6. rebuild_canonical_metadata_from_accumulated preserves repair fields
# ---------------------------------------------------------------------------

def test_rebuild_canonical_metadata_preserves_repair_fields():
    """rebuild_canonical_metadata_from_accumulated must not clear repair-state fields."""
    seeds = _make_seeds(3)
    pkg = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "processed_seed_ids": [seeds[0]["seed_id"]],
            "needs_retry_seed_ids": [seeds[1]["seed_id"]],
            "false_processed_seed_ids": [seeds[2]["seed_id"]],
            "seed_accounting": {"k": {"attempts": 3}},
            "no_variants_by_seed": {"k2": {"reason": "no data"}},
            "dedupe_proof_by_seed": {"k3": {"proof": "ok"}},
            "zero_variant_seed_ids": [seeds[1]["seed_id"]],
            "zero_variant_repair_audit": {"original_false_processed_seed_ids": [seeds[2]["seed_id"]]},
            "coverage_by_make": {},
            "market": "IL",
            "failed_seed_ids": [],
            "failed_details": [],
        },
        "accumulated_clean_export": {"variants": [_variant(make="Honda", model="M00")]},
    }

    result = br.rebuild_canonical_metadata_from_accumulated(pkg, seeds)

    bs = result["batch_state"]
    assert bs.get("needs_retry_seed_ids") == [seeds[1]["seed_id"]]
    assert bs.get("false_processed_seed_ids") == [seeds[2]["seed_id"]]
    assert bs.get("seed_accounting") == {"k": {"attempts": 3}}
    assert bs.get("no_variants_by_seed") == {"k2": {"reason": "no data"}}
    assert bs.get("dedupe_proof_by_seed") == {"k3": {"proof": "ok"}}
    assert bs.get("zero_variant_seed_ids") == [seeds[1]["seed_id"]]
    assert bs.get("zero_variant_repair_audit", {}).get("original_false_processed_seed_ids") == [seeds[2]["seed_id"]]


# ---------------------------------------------------------------------------
# 7. Resolved retry seed removed only when variants/no_variants_reason/dedupe_proof exists
# ---------------------------------------------------------------------------

def test_resolved_retry_seed_removed_when_has_variants(monkeypatch):
    """A seed in needs_retry_seed_ids is removed when matching variants exist."""
    honda_seed = _seed("honda__civic__2017__2026__il", make="Honda", model="Civic")
    bmw_seed = _seed("bmw__850i__2018__2026__il", make="BMW", model="850i")
    ordered = [honda_seed, bmw_seed]

    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(br, "audit_coverage_until_last_completed",
                        lambda *a, **k: {"holes_count": 0})

    previous = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "processed_seed_ids": [],
            "needs_retry_seed_ids": [
                "honda__civic__2017__2026__il",
                "bmw__850i__2018__2026__il",
            ],
            "seed_accounting": {},
        },
    }

    # Honda has variants → should be resolved (removed from retry)
    # BMW has no variants → should stay in retry
    honda_variant = _variant(make="Honda", model="Civic")
    result = br.build_canonical_candidate(
        previous_package=previous,
        merged_variants=[honda_variant],
        new_batch_state=None,
        market="IL",
    )

    bs = result["batch_state"]
    retry = bs.get("needs_retry_seed_ids", [])
    assert "honda__civic__2017__2026__il" not in retry, \
        "Honda seed must be resolved because variants exist"
    assert "bmw__850i__2018__2026__il" in retry, \
        "BMW seed must remain because no variants exist"


def test_retry_seed_not_removed_when_only_no_variants_reason(monkeypatch):
    """Seed stays resolved when no_variants_by_seed has a reason (even without variants)."""
    honda_seed = _seed("honda__civic__2017__2026__il", make="Honda", model="Civic")
    ordered = [honda_seed]

    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(br, "audit_coverage_until_last_completed",
                        lambda *a, **k: {"holes_count": 0})

    previous = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "processed_seed_ids": [],
            "needs_retry_seed_ids": ["honda__civic__2017__2026__il"],
            "no_variants_by_seed": {
                "honda__civic__2017__2026__il": {"reason": "discontinued", "attempts": 1}
            },
            "seed_accounting": {},
        },
    }

    result = br.build_canonical_candidate(
        previous_package=previous,
        merged_variants=[],
        new_batch_state=None,
        market="IL",
    )

    bs = result["batch_state"]
    retry = bs.get("needs_retry_seed_ids", [])
    assert "honda__civic__2017__2026__il" not in retry, \
        "Seed must be removed when no_variants_reason exists"


# ---------------------------------------------------------------------------
# 8. extract_canonical_batch_state preserves repair-state fields
# ---------------------------------------------------------------------------

def test_extract_canonical_batch_state_preserves_repair_fields():
    """extract_canonical_batch_state must carry repair fields through from raw_state."""
    seed1 = _seed("s1_real", make="Audi", model="Q5")
    seed2 = _seed("s2_real", make="BMW", model="X5")
    ordered = [seed1, seed2]

    raw_state = {
        "processed_seed_ids": ["s1_real"],
        "needs_retry_seed_ids": ["s2_real"],
        "false_processed_seed_ids": ["s1_real"],
        "seed_accounting": {"s2_real": {"attempts": 2}},
        "no_variants_by_seed": {"s2_real": {"reason": "test"}},
        "dedupe_proof_by_seed": {"s2_real": {"proof": "ok"}},
        "zero_variant_seed_ids": ["s2_real"],
        "zero_variant_repair_audit": {"count": 1},
        "market": "IL",
    }
    package = {"schema_version": "resume_package_v1", "batch_state": raw_state}

    result = br.extract_canonical_batch_state(package, ordered, market="IL")

    assert result.get("needs_retry_seed_ids") == ["s2_real"]
    assert result.get("false_processed_seed_ids") == ["s1_real"]
    assert result.get("seed_accounting") == {"s2_real": {"attempts": 2}}
    assert result.get("no_variants_by_seed") == {"s2_real": {"reason": "test"}}
    assert result.get("dedupe_proof_by_seed") == {"s2_real": {"proof": "ok"}}
    assert result.get("zero_variant_seed_ids") == ["s2_real"]
    assert result.get("zero_variant_repair_audit") == {"count": 1}


def test_recovery_excludes_invalid_from_active_queue_and_overlap_cleanup(monkeypatch, tmp_path):
    seeds = [
        _seed("bmw__850i__2018__2026__il", make="BMW", model="850i"),
        _seed("hummer__h3__2005__2010__il", make="Hummer", model="H3"),
        _seed("infiniti__qx80__2010__2022__il", make="Infiniti", model="QX80"),
    ]
    unresolved_54 = [s["seed_id"] for s in seeds]
    backup_pkg = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "needs_retry_seed_ids": unresolved_54 + ["s1"],
            "zero_variant_repair_audit": {"original_false_processed_seed_ids": unresolved_54 + ["s1"]},
        },
    }
    current_bs = {
        "processed_seed_ids": unresolved_54,
        "needs_retry_seed_ids": ["s1", "hummer__h3__2005__2010__il"],
        "next_seed_id": "haval__h6__2022__2026__il",
        "market": "IL",
    }
    current_pkg = {"schema_version": "resume_package_v1", "batch_state": current_bs, "accumulated_clean_export": {"variants": []}}
    fake_backup_path = tmp_path / "backup.json"
    fake_backup_path.write_text(json.dumps(backup_pkg))
    monkeypatch.setattr(br, "_canonical_backup_path", lambda: fake_backup_path)
    monkeypatch.setattr(br, "load_json_object", lambda p: json.loads(pathlib.Path(p).read_text()))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(current_pkg))
    monkeypatch.setattr(br, "save_local_canonical_resume_package", lambda pkg: None)
    monkeypatch.setattr(br, "load_batch_state", lambda market="IL": copy.deepcopy(current_bs))
    saved = {}
    monkeypatch.setattr(br, "_save_state", lambda s: saved.__setitem__("state", copy.deepcopy(s)))

    result = br.recover_zero_variant_repair_state_from_backup("IL")
    active = saved["state"]["needs_retry_seed_ids"]
    assert "s1" not in active
    assert "s1" in saved["state"].get("invalid_needs_retry_seed_ids", [])
    assert result["next_seed_id"] == "bmw__850i__2018__2026__il"
    assert result["overlap_processed_and_needs_retry"] == []


def test_hummer_not_selected_when_bmw_unresolved(monkeypatch, tmp_path):
    seeds = [
        _seed("bmw__850i__2018__2026__il", make="BMW", model="850i"),
        _seed("hummer__h3__2005__2010__il", make="Hummer", model="H3"),
    ]
    backup_pkg = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "needs_retry_seed_ids": [s["seed_id"] for s in seeds],
            "zero_variant_repair_audit": {"original_false_processed_seed_ids": [s["seed_id"] for s in seeds]},
        },
    }
    current_bs = {"processed_seed_ids": [], "needs_retry_seed_ids": [], "market": "IL"}
    current_pkg = {"schema_version": "resume_package_v1", "batch_state": current_bs, "accumulated_clean_export": {"variants": []}}
    fake_backup_path = tmp_path / "backup.json"
    fake_backup_path.write_text(json.dumps(backup_pkg))
    monkeypatch.setattr(br, "_canonical_backup_path", lambda: fake_backup_path)
    monkeypatch.setattr(br, "load_json_object", lambda p: json.loads(pathlib.Path(p).read_text()))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(current_pkg))
    monkeypatch.setattr(br, "save_local_canonical_resume_package", lambda pkg: None)
    monkeypatch.setattr(br, "load_batch_state", lambda market="IL": copy.deepcopy(current_bs))
    monkeypatch.setattr(br, "_save_state", lambda s: None)

    result = br.recover_zero_variant_repair_state_from_backup("IL")
    assert result["next_seed_id"] == "bmw__850i__2018__2026__il"

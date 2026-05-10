"""Tests for repair_and_audit_zero_variant_processed_seeds().

Covers all 12 required test cases from the problem statement:
1.  A processed seed with 0 variants is removed from processed_seed_ids and moved to needs_retry_seed_ids.
2.  A previously false-processed seed that later has variants is counted as fixed.
3.  variants_added_by_seed correctly records the number of variants for every fixed seed.
4.  original_false_processed_seed_ids persists across runs.
5.  Newly detected zero-variant processed seeds are added to the audit.
6.  seed_accounting is populated for fixed and unresolved seeds.
7.  find_processed_zero_variant_seeds() returns 0 after repair (unresolved seeds removed from processed).
8.  No valid variants are deleted.
9.  processed_seed_count_after == processed_seed_count_before - removed + kept-fixed.
10. If all zero-variant processed seeds are resolved, next_seed_id returns to haval__h6__2022__2026__il.
11. If Haval still has 0 variants and failed status, safe_to_continue remains false.
12. batch_state.json and resume_package_canonical.json both contain the same repair audit after saving.
"""
from __future__ import annotations

import copy
import json

import pytest
import agent.batch_runner as br


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed(seed_id, make="Honda", model="Civic", ys=2017, ye=2026, market="IL"):
    return {"seed_id": seed_id, "make": make, "model": model,
            "year_start": ys, "year_end": ye, "market": market}


def _variant(seed_id, make="Honda", model="Civic", ys=2017, ye=2026):
    return {"seed_id": seed_id, "make": make, "model": model,
            "market": "IL", "year_start": ys, "year_end": ye,
            "verification_status": "verified"}


def _make_canonical(seeds, variants=None, extra_bs=None):
    pkg = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "processed_seed_ids": [s["seed_id"] for s in seeds],
            "last_completed_seed_id": seeds[-1]["seed_id"] if seeds else None,
            "next_seed_id": None,
        },
        "accumulated_clean_export": {"variants": list(variants or [])},
    }
    if extra_bs:
        pkg["batch_state"].update(extra_bs)
    return pkg


def _monkeypatch_audit(monkeypatch, tmp_path, canonical, seeds, saved=None):
    """Apply standard monkeypatches for repair_and_audit_zero_variant_processed_seeds."""
    _saved = {} if saved is None else saved

    def _fake_save_canonical(pkg):
        _saved["pkg"] = copy.deepcopy(pkg)

    saved_state = {}

    def _fake_save_state(s):
        saved_state["state"] = copy.deepcopy(s)

    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "save_local_canonical_resume_package", _fake_save_canonical)
    monkeypatch.setattr(br, "_save_state", _fake_save_state)
    monkeypatch.setattr(br, "_canonical_resume_path", lambda: tmp_path / "canonical.json")
    monkeypatch.setattr(br, "_batch_state_path", lambda: tmp_path / "batch_state.json")
    monkeypatch.setattr(br, "evaluate_continue_guard",
                        lambda market="IL": {"passed": True, "issues": [], "false_processed_seed_count": 0})
    return _saved, saved_state


# ---------------------------------------------------------------------------
# Test 1: processed seed with 0 variants moved to needs_retry_seed_ids
# ---------------------------------------------------------------------------

def test_audit_removes_zero_variant_seed_from_processed(monkeypatch, tmp_path):
    seeds = [_seed("bmw__850i__2018__2026__il", make="BMW", model="850i")]
    canonical = _make_canonical(seeds, variants=[])
    saved, _ = _monkeypatch_audit(monkeypatch, tmp_path, canonical, seeds)

    result = br.repair_and_audit_zero_variant_processed_seeds(market="IL")

    assert result["ok"] is True
    assert result["repaired_count"] == 1
    assert result["unresolved_count"] == 1
    assert "bmw__850i__2018__2026__il" in result["unresolved_seed_ids"]
    assert result["processed_seed_count_after"] == 0
    bs = saved["pkg"]["batch_state"]
    assert "bmw__850i__2018__2026__il" not in bs.get("processed_seed_ids", [])
    assert "bmw__850i__2018__2026__il" in bs.get("needs_retry_seed_ids", [])


# ---------------------------------------------------------------------------
# Test 2: Previously false-processed seed that later has variants is fixed
# ---------------------------------------------------------------------------

def test_audit_counts_seed_with_variants_as_fixed(monkeypatch, tmp_path):
    seeds = [_seed("bmw__850i__2018__2026__il", make="BMW", model="850i")]
    # Seed is in processed AND has a variant → it is fixed (not false-processed)
    v = _variant("bmw__850i__2018__2026__il", make="BMW", model="850i", ys=2018, ye=2026)
    # Seed was previously in original_false_processed list (persisted audit)
    prior_audit = {
        "original_false_processed_seed_ids": ["bmw__850i__2018__2026__il"],
        "original_false_processed_count": 1,
    }
    canonical = _make_canonical(seeds, variants=[v], extra_bs={"zero_variant_repair_audit": prior_audit})
    saved, _ = _monkeypatch_audit(monkeypatch, tmp_path, canonical, seeds)

    result = br.repair_and_audit_zero_variant_processed_seeds(market="IL")

    assert result["ok"] is True
    assert result["fixed_count"] == 1
    assert "bmw__850i__2018__2026__il" in result["fixed_seed_ids"]
    assert result["unresolved_count"] == 0
    assert result["repaired_count"] == 0


# ---------------------------------------------------------------------------
# Test 3: variants_added_by_seed correctly records variant count for fixed seeds
# ---------------------------------------------------------------------------

def test_audit_variants_added_by_seed_correct(monkeypatch, tmp_path):
    seeds = [
        _seed("toyota__corolla__2017__2026__il", make="Toyota", model="Corolla"),
        _seed("honda__civic__2017__2026__il", make="Honda", model="Civic"),
    ]
    # Toyota had 2 variants, Honda had 3
    variants = [
        _variant("toyota__corolla__2017__2026__il", make="Toyota", model="Corolla"),
        _variant("toyota__corolla__2017__2026__il", make="Toyota", model="Corolla"),
        _variant("honda__civic__2017__2026__il", make="Honda", model="Civic"),
        _variant("honda__civic__2017__2026__il", make="Honda", model="Civic"),
        _variant("honda__civic__2017__2026__il", make="Honda", model="Civic"),
    ]
    prior_audit = {
        "original_false_processed_seed_ids": [
            "toyota__corolla__2017__2026__il",
            "honda__civic__2017__2026__il",
        ],
        "original_false_processed_count": 2,
    }
    canonical = _make_canonical(seeds, variants=variants, extra_bs={"zero_variant_repair_audit": prior_audit})
    saved, _ = _monkeypatch_audit(monkeypatch, tmp_path, canonical, seeds)

    result = br.repair_and_audit_zero_variant_processed_seeds(market="IL")

    assert result["ok"] is True
    vabs = result["variants_added_by_seed"]
    assert vabs.get("toyota__corolla__2017__2026__il") == 2
    assert vabs.get("honda__civic__2017__2026__il") == 3


# ---------------------------------------------------------------------------
# Test 4: original_false_processed_seed_ids persists across runs
# ---------------------------------------------------------------------------

def test_audit_original_list_persists_across_runs(monkeypatch, tmp_path):
    seeds = [
        _seed("bmw__850i__2018__2026__il", make="BMW", model="850i"),
        _seed("toyota__corolla__2017__2026__il", make="Toyota", model="Corolla"),
    ]
    # First run: no variants for either seed
    canonical_v1 = _make_canonical(seeds, variants=[])
    _saved1 = {}
    _saved1_state = {}
    call_count = {"n": 0}
    _pkgs = [copy.deepcopy(canonical_v1)]

    def _load():
        return copy.deepcopy(_pkgs[-1])

    def _save_canonical(pkg):
        _pkgs.append(copy.deepcopy(pkg))
        _saved1["pkg"] = copy.deepcopy(pkg)

    monkeypatch.setattr(br, "load_local_canonical_resume_package", _load)
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "save_local_canonical_resume_package", _save_canonical)
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_canonical_resume_path", lambda: tmp_path / "canonical.json")
    monkeypatch.setattr(br, "_batch_state_path", lambda: tmp_path / "batch_state.json")
    monkeypatch.setattr(br, "evaluate_continue_guard",
                        lambda market="IL": {"passed": True, "issues": [], "false_processed_seed_count": 0})

    r1 = br.repair_and_audit_zero_variant_processed_seeds(market="IL")
    assert r1["ok"] is True
    original_after_r1 = set(r1["original_false_processed_seed_ids"])
    assert "bmw__850i__2018__2026__il" in original_after_r1
    assert "toyota__corolla__2017__2026__il" in original_after_r1

    # Second run: BMW now has a variant (fixed), Toyota still 0 (unresolved)
    # The saved package after first run is in _pkgs[-1]; Toyota is in needs_retry, not processed.
    # Simulate Toyota still being unresolved (it's in needs_retry already).
    # Run again and check original list is preserved
    r2 = br.repair_and_audit_zero_variant_processed_seeds(market="IL")
    assert r2["ok"] is True
    # original list must still include both seeds even though they're now in needs_retry
    original_after_r2 = set(r2["original_false_processed_seed_ids"])
    # BMW and Toyota were in the original list; they must still be recorded
    assert original_after_r2 >= original_after_r1 or original_after_r2 == original_after_r1


# ---------------------------------------------------------------------------
# Test 5: Newly detected zero-variant processed seeds added to audit
# ---------------------------------------------------------------------------

def test_audit_newly_detected_seeds_added(monkeypatch, tmp_path):
    seeds = [
        _seed("bmw__850i__2018__2026__il", make="BMW", model="850i"),
        _seed("infiniti__qx80__2010__2022__il", make="Infiniti", model="QX80",
              ys=2010, ye=2022),
    ]
    # Prior audit only recorded BMW; Infiniti is newly processed with 0 variants
    prior_audit = {
        "original_false_processed_seed_ids": ["bmw__850i__2018__2026__il"],
        "original_false_processed_count": 1,
    }
    canonical = _make_canonical(seeds, variants=[], extra_bs={"zero_variant_repair_audit": prior_audit})
    saved, _ = _monkeypatch_audit(monkeypatch, tmp_path, canonical, seeds)

    result = br.repair_and_audit_zero_variant_processed_seeds(market="IL")

    assert result["ok"] is True
    assert result["newly_detected_count"] >= 1
    assert "infiniti__qx80__2010__2022__il" in result["newly_detected_seed_ids"]
    # Both should end up in original list
    assert "infiniti__qx80__2010__2022__il" in result["original_false_processed_seed_ids"]


# ---------------------------------------------------------------------------
# Test 6: seed_accounting populated for fixed and unresolved seeds
# ---------------------------------------------------------------------------

def test_audit_seed_accounting_populated(monkeypatch, tmp_path):
    seeds = [
        _seed("bmw__850i__2018__2026__il", make="BMW", model="850i"),
        _seed("toyota__corolla__2017__2026__il", make="Toyota", model="Corolla"),
    ]
    # Toyota has a variant (fixed), BMW has none (unresolved)
    v = _variant("toyota__corolla__2017__2026__il", make="Toyota", model="Corolla")
    prior_audit = {
        "original_false_processed_seed_ids": [
            "bmw__850i__2018__2026__il",
            "toyota__corolla__2017__2026__il",
        ],
        "original_false_processed_count": 2,
    }
    canonical = _make_canonical(seeds, variants=[v], extra_bs={"zero_variant_repair_audit": prior_audit})
    saved, _ = _monkeypatch_audit(monkeypatch, tmp_path, canonical, seeds)

    br.repair_and_audit_zero_variant_processed_seeds(market="IL")

    accounting = saved["pkg"]["batch_state"].get("seed_accounting", {})
    bmw = accounting.get("bmw__850i__2018__2026__il", {})
    assert bmw.get("repair_status") == "moved_from_processed_to_needs_retry"
    assert bmw.get("repair_reason") == "processed_seed_with_zero_variants"
    toyota = accounting.get("toyota__corolla__2017__2026__il", {})
    assert toyota.get("repair_status") == "fixed_by_later_variants"
    assert toyota.get("repair_reason") == "seed_now_has_valid_variants"
    assert toyota.get("variant_count") == 1


# ---------------------------------------------------------------------------
# Test 7: find_processed_zero_variant_seeds returns 0 after repair
# ---------------------------------------------------------------------------

def test_audit_find_zero_variant_returns_empty_after_repair(monkeypatch, tmp_path):
    seeds = [_seed("bmw__850i__2018__2026__il", make="BMW", model="850i")]
    canonical = _make_canonical(seeds, variants=[])
    saved, _ = _monkeypatch_audit(monkeypatch, tmp_path, canonical, seeds)

    br.repair_and_audit_zero_variant_processed_seeds(market="IL")

    repaired_pkg = saved["pkg"]
    # BMW is now in needs_retry, NOT in processed_seed_ids
    remaining = br.find_processed_zero_variant_seeds(repaired_pkg, ordered_seeds=seeds)
    assert remaining == [], f"Expected empty but got: {remaining}"


# ---------------------------------------------------------------------------
# Test 8: No valid variants are deleted
# ---------------------------------------------------------------------------

def test_audit_does_not_delete_valid_variants(monkeypatch, tmp_path):
    seeds = [
        _seed("bmw__850i__2018__2026__il", make="BMW", model="850i"),
        _seed("toyota__corolla__2017__2026__il", make="Toyota", model="Corolla"),
    ]
    toyota_v = _variant("toyota__corolla__2017__2026__il", make="Toyota", model="Corolla")
    canonical = _make_canonical(seeds, variants=[toyota_v])
    saved, _ = _monkeypatch_audit(monkeypatch, tmp_path, canonical, seeds)

    br.repair_and_audit_zero_variant_processed_seeds(market="IL")

    saved_variants = saved["pkg"].get("accumulated_clean_export", {}).get("variants", [])
    assert any(v.get("make") == "Toyota" for v in saved_variants), "Toyota variant was deleted!"
    assert len(saved_variants) == 1


# ---------------------------------------------------------------------------
# Test 9: processed_seed_count_after accounts for removed and kept seeds
# ---------------------------------------------------------------------------

def test_audit_processed_count_correct_after_repair(monkeypatch, tmp_path):
    seeds = [
        _seed("s1", make="A", model="X"),
        _seed("s2", make="B", model="Y"),
        _seed("s3", make="C", model="Z"),
    ]
    # s1: 0 variants (unresolved) → removed; s2: 1 variant (kept); s3: 0 variants (unresolved) → removed
    s2_variant = {"seed_id": "s2", "make": "B", "model": "Y", "market": "IL",
                  "year_start": 2017, "year_end": 2026}
    canonical = _make_canonical(seeds, variants=[s2_variant])
    saved, _ = _monkeypatch_audit(monkeypatch, tmp_path, canonical, seeds)

    result = br.repair_and_audit_zero_variant_processed_seeds(market="IL")

    assert result["ok"] is True
    assert result["processed_seed_count_before"] == 3
    # s1 and s3 removed (unresolved); s2 kept
    assert result["processed_seed_count_after"] == 1


# ---------------------------------------------------------------------------
# Test 10: When all zero-variant seeds resolved, next_seed_id → haval
# ---------------------------------------------------------------------------

def test_audit_returns_to_haval_when_all_resolved(monkeypatch, tmp_path):
    seeds = [
        _seed("bmw__850i__2018__2026__il", make="BMW", model="850i"),
    ]
    bmw_v = _variant("bmw__850i__2018__2026__il", make="BMW", model="850i", ys=2018, ye=2026)
    # Prior audit said BMW was false-processed; now it has variants (fixed)
    prior_audit = {
        "original_false_processed_seed_ids": ["bmw__850i__2018__2026__il"],
        "original_false_processed_count": 1,
    }
    canonical = _make_canonical(seeds, variants=[bmw_v], extra_bs={"zero_variant_repair_audit": prior_audit})
    saved, _ = _monkeypatch_audit(monkeypatch, tmp_path, canonical, seeds)

    result = br.repair_and_audit_zero_variant_processed_seeds(market="IL")

    assert result["ok"] is True
    assert result["remaining_to_fix_count"] == 0
    # When no false-processed seeds remain, must return to the blocking failed seed
    assert result["next_seed_after_repair"] == br.BLOCKING_FAILED_SEED_ID


# ---------------------------------------------------------------------------
# Test 11: If Haval still has 0 variants, safe_to_continue == False
# ---------------------------------------------------------------------------

def test_audit_safe_to_continue_false_when_haval_unresolved(monkeypatch, tmp_path):
    seeds = [
        _seed("bmw__850i__2018__2026__il", make="BMW", model="850i"),
    ]
    bmw_v = _variant("bmw__850i__2018__2026__il", make="BMW", model="850i", ys=2018, ye=2026)
    prior_audit = {
        "original_false_processed_seed_ids": ["bmw__850i__2018__2026__il"],
        "original_false_processed_count": 1,
    }
    canonical = _make_canonical(seeds, variants=[bmw_v], extra_bs={"zero_variant_repair_audit": prior_audit})
    saved, _ = _monkeypatch_audit(monkeypatch, tmp_path, canonical, seeds)

    result = br.repair_and_audit_zero_variant_processed_seeds(market="IL")

    assert result["ok"] is True
    # Haval has no variants (not in seeds or variants), so safe_to_continue must be False
    assert result["safe_to_continue_after_repair"] is False
    assert result["next_seed_after_repair"] == br.BLOCKING_FAILED_SEED_ID


# ---------------------------------------------------------------------------
# Test 12: Both files contain the same repair audit after saving
# ---------------------------------------------------------------------------

def test_audit_both_files_contain_same_audit(monkeypatch, tmp_path):
    seeds = [_seed("bmw__850i__2018__2026__il", make="BMW", model="850i")]
    canonical = _make_canonical(seeds, variants=[])

    saved_canonical = {}
    saved_state = {}

    def _fake_save_canonical(pkg):
        saved_canonical["pkg"] = copy.deepcopy(pkg)

    def _fake_save_state(state):
        saved_state["state"] = copy.deepcopy(state)

    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "save_local_canonical_resume_package", _fake_save_canonical)
    monkeypatch.setattr(br, "_save_state", _fake_save_state)
    monkeypatch.setattr(br, "_canonical_resume_path", lambda: tmp_path / "canonical.json")
    monkeypatch.setattr(br, "_batch_state_path", lambda: tmp_path / "batch_state.json")
    monkeypatch.setattr(br, "evaluate_continue_guard",
                        lambda market="IL": {"passed": True, "issues": [], "false_processed_seed_count": 0})

    result = br.repair_and_audit_zero_variant_processed_seeds(market="IL")

    assert result["ok"] is True
    canonical_audit = saved_canonical["pkg"]["batch_state"].get("zero_variant_repair_audit", {})
    state_audit = saved_state["state"].get("zero_variant_repair_audit", {})
    # Both must contain identical audit data
    assert canonical_audit == state_audit
    assert canonical_audit.get("schema_version") == 1
    assert "original_false_processed_seed_ids" in canonical_audit

"""Regression tests for repair_false_processed_zero_variant_seeds().

Covers:
- Seeds with no variants/no dedupe_proof/no no_variants_reason are moved to needs_retry.
- Seeds that genuinely have variants are NOT moved.
- Canonical variant list is NOT changed.
- processed_seed_count decreases by exactly the number of repaired seeds.
- Guard passes after repair when no other issues remain.
- Repair is idempotent: running it twice changes nothing the second time.
- seed_accounting gains repair_status/repair_reason markers for each repaired seed.
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


def _make_canonical(seeds, variants=None, extra_bs=None):
    """Build a minimal canonical resume package."""
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


# ---------------------------------------------------------------------------
# 1. False-processed seeds are removed from processed_seed_ids
# ---------------------------------------------------------------------------

def test_repair_removes_false_processed_from_processed(monkeypatch, tmp_path):
    seeds = [_seed("bmw__850i__2018__2026__il", make="BMW", model="850i")]
    canonical = _make_canonical(seeds, variants=[])

    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "save_local_canonical_resume_package", lambda pkg: None)
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_canonical_resume_path", lambda: tmp_path / "canonical.json")
    monkeypatch.setattr(br, "_batch_state_path", lambda: tmp_path / "batch_state.json")
    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": {"passed": True, "issues": [], "false_processed_seed_count": 0})

    result = br.repair_false_processed_zero_variant_seeds(market="IL")

    assert result["ok"] is True
    assert result["repaired_count"] == 1
    assert "bmw__850i__2018__2026__il" in result["repaired_seed_ids"]
    assert result["processed_seed_count_before"] == 1
    assert result["processed_seed_count_after"] == 0


# ---------------------------------------------------------------------------
# 2. Repaired seeds are added to needs_retry_seed_ids
# ---------------------------------------------------------------------------

def test_repair_adds_to_needs_retry(monkeypatch, tmp_path):
    seeds = [_seed("bmw__850i__2018__2026__il", make="BMW", model="850i")]
    canonical = _make_canonical(seeds, variants=[])

    saved = {}

    def _fake_save_canonical(pkg):
        saved["pkg"] = copy.deepcopy(pkg)

    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "save_local_canonical_resume_package", _fake_save_canonical)
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_canonical_resume_path", lambda: tmp_path / "canonical.json")
    monkeypatch.setattr(br, "_batch_state_path", lambda: tmp_path / "batch_state.json")
    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": {"passed": True, "issues": [], "false_processed_seed_count": 0})

    br.repair_false_processed_zero_variant_seeds(market="IL")

    bs = saved["pkg"].get("batch_state", {})
    assert "bmw__850i__2018__2026__il" in bs.get("needs_retry_seed_ids", [])
    assert "bmw__850i__2018__2026__il" not in bs.get("processed_seed_ids", [])


# ---------------------------------------------------------------------------
# 3. Canonical variant list is NOT changed
# ---------------------------------------------------------------------------

def test_repair_does_not_delete_canonical_variants(monkeypatch, tmp_path):
    seeds = [
        _seed("bmw__850i__2018__2026__il", make="BMW", model="850i"),
        _seed("toyota__corolla__2017__2026__il", make="Toyota", model="Corolla"),
    ]
    toyota_variant = {
        "seed_id": "toyota__corolla__2017__2026__il",
        "make": "Toyota", "model": "Corolla", "market": "IL",
        "year_start": 2017, "year_end": 2026,
        "verification_status": "verified",
    }
    canonical = _make_canonical(seeds, variants=[toyota_variant])

    saved = {}

    def _fake_save_canonical(pkg):
        saved["pkg"] = copy.deepcopy(pkg)

    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "save_local_canonical_resume_package", _fake_save_canonical)
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_canonical_resume_path", lambda: tmp_path / "canonical.json")
    monkeypatch.setattr(br, "_batch_state_path", lambda: tmp_path / "batch_state.json")
    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": {"passed": True, "issues": [], "false_processed_seed_count": 0})

    result = br.repair_false_processed_zero_variant_seeds(market="IL")

    assert result["ok"] is True
    # Toyota variant must still be present
    saved_variants = (saved["pkg"].get("accumulated_clean_export") or {}).get("variants", [])
    assert any(v.get("make") == "Toyota" for v in saved_variants), "Toyota variant was deleted!"
    # Only BMW (false-processed) was repaired
    assert result["repaired_count"] == 1
    assert "bmw__850i__2018__2026__il" in result["repaired_seed_ids"]


# ---------------------------------------------------------------------------
# 4. processed_seed_count decreases by exactly repaired_count
# ---------------------------------------------------------------------------

def test_repair_processed_count_decreases_by_repaired_count(monkeypatch, tmp_path):
    seeds = [
        _seed("s1", make="A", model="X"),
        _seed("s2", make="B", model="Y"),
        _seed("s3", make="C", model="Z"),
    ]
    # s1 and s3 have no variants → false-processed; s2 has a variant → genuine
    s2_variant = {"seed_id": "s2", "make": "B", "model": "Y", "market": "IL",
                  "year_start": 2017, "year_end": 2026}
    canonical = _make_canonical(seeds, variants=[s2_variant])

    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "save_local_canonical_resume_package", lambda pkg: None)
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_canonical_resume_path", lambda: tmp_path / "canonical.json")
    monkeypatch.setattr(br, "_batch_state_path", lambda: tmp_path / "batch_state.json")
    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": {"passed": True, "issues": [], "false_processed_seed_count": 0})

    result = br.repair_false_processed_zero_variant_seeds(market="IL")

    assert result["ok"] is True
    assert result["repaired_count"] == 2
    assert result["processed_seed_count_before"] == 3
    assert result["processed_seed_count_after"] == 1


# ---------------------------------------------------------------------------
# 5. seed_accounting gets repair_status/repair_reason markers
# ---------------------------------------------------------------------------

def test_repair_stamps_seed_accounting(monkeypatch, tmp_path):
    seeds = [_seed("bmw__850i__2018__2026__il", make="BMW", model="850i")]
    canonical = _make_canonical(seeds, variants=[])

    saved = {}

    def _fake_save_canonical(pkg):
        saved["pkg"] = copy.deepcopy(pkg)

    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "save_local_canonical_resume_package", _fake_save_canonical)
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_canonical_resume_path", lambda: tmp_path / "canonical.json")
    monkeypatch.setattr(br, "_batch_state_path", lambda: tmp_path / "batch_state.json")
    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": {"passed": True, "issues": [], "false_processed_seed_count": 0})

    br.repair_false_processed_zero_variant_seeds(market="IL")

    accounting = saved["pkg"].get("batch_state", {}).get("seed_accounting", {})
    entry = accounting.get("bmw__850i__2018__2026__il", {})
    assert entry.get("repair_status") == "moved_from_processed_to_needs_retry"
    assert entry.get("repair_reason") == "false_processed_zero_variant_without_proof"
    assert entry.get("repair_timestamp")


# ---------------------------------------------------------------------------
# 6. Guard passes after repair when no other issues remain
# ---------------------------------------------------------------------------

def test_guard_passes_after_repair(monkeypatch, tmp_path):
    seeds = [_seed("bmw__850i__2018__2026__il", make="BMW", model="850i")]
    canonical = _make_canonical(seeds, variants=[])

    guard_calls = {"n": 0}

    def _fake_guard(market="IL"):
        guard_calls["n"] += 1
        # First call (at start of function) would return real; we simulate passed after repair
        return {"passed": True, "issues": [], "false_processed_seed_count": 0}

    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "save_local_canonical_resume_package", lambda pkg: None)
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_canonical_resume_path", lambda: tmp_path / "canonical.json")
    monkeypatch.setattr(br, "_batch_state_path", lambda: tmp_path / "batch_state.json")
    monkeypatch.setattr(br, "evaluate_continue_guard", _fake_guard)

    result = br.repair_false_processed_zero_variant_seeds(market="IL")

    assert result["ok"] is True
    assert result["guard_after"]["passed"] is True
    assert result["guard_after"]["false_processed_seed_count"] == 0


# ---------------------------------------------------------------------------
# 7. Repair is idempotent: running it twice changes nothing the second time
# ---------------------------------------------------------------------------

def test_repair_is_idempotent(monkeypatch, tmp_path):
    seeds = [_seed("bmw__850i__2018__2026__il", make="BMW", model="850i")]
    canonical = _make_canonical(seeds, variants=[])

    # Simulate: after first repair canonical has no false-processed seeds
    repaired_canonical = copy.deepcopy(canonical)
    repaired_canonical["batch_state"]["processed_seed_ids"] = []
    repaired_canonical["batch_state"].setdefault("needs_retry_seed_ids", ["bmw__850i__2018__2026__il"])

    call_count = {"n": 0}
    _saved = [copy.deepcopy(canonical)]

    def _load_canonical():
        return copy.deepcopy(_saved[-1])

    def _save_canonical(pkg):
        _saved.append(copy.deepcopy(pkg))

    monkeypatch.setattr(br, "load_local_canonical_resume_package", _load_canonical)
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "save_local_canonical_resume_package", _save_canonical)
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_canonical_resume_path", lambda: tmp_path / "canonical.json")
    monkeypatch.setattr(br, "_batch_state_path", lambda: tmp_path / "batch_state.json")
    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": {"passed": True, "issues": [], "false_processed_seed_count": 0})

    # First repair
    r1 = br.repair_false_processed_zero_variant_seeds(market="IL")
    assert r1["repaired_count"] == 1

    # Second repair — the saved canonical no longer has the seed in processed_seed_ids
    # Use last saved state
    r2 = br.repair_false_processed_zero_variant_seeds(market="IL")
    assert r2["ok"] is True
    assert r2["repaired_count"] == 0  # nothing to repair


# ---------------------------------------------------------------------------
# 8. Backups are created
# ---------------------------------------------------------------------------

def test_repair_creates_backups(monkeypatch, tmp_path):
    seeds = [_seed("bmw__850i__2018__2026__il", make="BMW", model="850i")]
    canonical = _make_canonical(seeds, variants=[])

    # Write a fake canonical file and batch_state file so backups can be created
    canonical_file = tmp_path / "canonical.json"
    bs_file = tmp_path / "batch_state.json"
    canonical_file.write_text(json.dumps(canonical))
    bs_file.write_text(json.dumps({"schema_version": "batch_state_v1"}))

    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "save_local_canonical_resume_package", lambda pkg: None)
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_canonical_resume_path", lambda: canonical_file)
    monkeypatch.setattr(br, "_batch_state_path", lambda: bs_file)
    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": {"passed": True, "issues": [], "false_processed_seed_count": 0})

    result = br.repair_false_processed_zero_variant_seeds(market="IL")

    assert result["ok"] is True
    # At least canonical backup must be created
    assert result["backup_canonical_path"] is not None
    from pathlib import Path
    assert Path(result["backup_canonical_path"]).exists()


# ---------------------------------------------------------------------------
# 9. No false-processed seeds → repaired_count == 0, no changes
# ---------------------------------------------------------------------------

def test_repair_no_op_when_no_false_processed(monkeypatch, tmp_path):
    seeds = [_seed("honda__civic__1990__2026__il", model="Civic")]
    variant = {"seed_id": "honda__civic__1990__2026__il", "make": "Honda", "model": "Civic",
               "market": "IL", "year_start": 1990, "year_end": 2026}
    canonical = _make_canonical(seeds, variants=[variant])

    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(canonical))
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "save_local_canonical_resume_package", lambda pkg: None)
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_canonical_resume_path", lambda: tmp_path / "canonical.json")
    monkeypatch.setattr(br, "_batch_state_path", lambda: tmp_path / "batch_state.json")
    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": {"passed": True, "issues": [], "false_processed_seed_count": 0})

    result = br.repair_false_processed_zero_variant_seeds(market="IL")

    assert result["ok"] is True
    assert result["repaired_count"] == 0
    assert result["processed_seed_count_before"] == 1
    assert result["processed_seed_count_after"] == 1


# ---------------------------------------------------------------------------
# 10. Missing canonical → error returned, no crash
# ---------------------------------------------------------------------------

def test_repair_returns_error_when_canonical_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: None)
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": [])
    monkeypatch.setattr(br, "_canonical_resume_path", lambda: tmp_path / "canonical.json")
    monkeypatch.setattr(br, "_batch_state_path", lambda: tmp_path / "batch_state.json")

    result = br.repair_false_processed_zero_variant_seeds(market="IL")

    assert result["ok"] is False
    assert "error" in result

"""Tests for canonical coverage/count synchronization.

These tests verify that:
- coverage_by_make is rebuilt from accumulated_clean_export.variants, not run_history
- top-level verified_variants / partial_variants are kept in sync with accumulated
- variant counts never shrink after a metadata rebuild
- batch_state continuation pointers are unchanged after rebuild
- BMW (or any make) coverage is never zero when variants for that make exist
"""
from __future__ import annotations

import copy

from agent import batch_runner


def _make_seed(seed_id: str, make: str, model: str = "M") -> dict:
    return {"seed_id": seed_id, "make": make, "model": model, "year_start": 2000, "year_end": 2026, "market": "IL"}


def _bmw_variant(idx: int, status: str = "verified") -> dict:
    return {
        "variant_id": f"bmw-v{idx}",
        "make": "BMW",
        "model": "520d",
        "market": "IL",
        "year_start": 2008,
        "year_end": 2020,
        "verification_status": status,
        "classification": status,
    }


def _generic_variant(idx: int, make: str, status: str = "verified") -> dict:
    return {
        "variant_id": f"{make.lower()}-v{idx}",
        "make": make,
        "model": "TestModel",
        "market": "IL",
        "year_start": 2000,
        "year_end": 2026,
        "verification_status": status,
        "classification": status,
    }


# ---------------------------------------------------------------------------
# 1. Coverage is rebuilt from accumulated, not from run_history
# ---------------------------------------------------------------------------

def test_coverage_rebuilt_from_accumulated_not_run_history():
    """BMW variants in accumulated_clean_export must produce non-zero coverage
    even when run_history is empty."""
    bmw_seeds = [
        _make_seed("bmw__518i__1990__1996__il", "BMW"),
        _make_seed("bmw__520d__2008__2020__il", "BMW"),
    ]
    package = {
        "schema_version": "resume_package_v1",
        "accumulated_clean_export": {
            "variants": [
                _bmw_variant(1, "verified"),
                _bmw_variant(2, "partial"),
                _bmw_variant(3, "verified"),
            ]
        },
        "batch_state": {
            "processed_seed_ids": ["bmw__518i__1990__1996__il"],
            "failed_seed_ids": [],
            "coverage_by_make": {
                "BMW": {"total": 2, "processed": 1, "verified_variants": 0, "partial_variants": 0, "unresolved": 0, "failed": 0, "completed": False}
            },
        },
        "run_history": [],  # empty — must NOT be used for coverage
    }

    result = batch_runner.rebuild_canonical_metadata_from_accumulated(package, bmw_seeds)

    bmw_cov = result["batch_state"]["coverage_by_make"]["BMW"]
    assert bmw_cov["verified_variants"] > 0, "BMW verified_variants must be > 0 after rebuild"
    assert bmw_cov["partial_variants"] > 0, "BMW partial_variants must be > 0 after rebuild"
    assert bmw_cov["verified_variants"] == 2
    assert bmw_cov["partial_variants"] == 1


# ---------------------------------------------------------------------------
# 2. Top-level lists are synced from accumulated
# ---------------------------------------------------------------------------

def test_top_level_lists_synced_from_accumulated():
    """After rebuild, top-level verified_variants has 10 items and partial_variants has 5."""
    seeds = [_make_seed(f"make__m{i}__2000__2026__il", "Make") for i in range(20)]
    variants = (
        [_generic_variant(i, "Make", "verified") for i in range(10)]
        + [_generic_variant(i + 100, "Make", "partial") for i in range(5)]
    )
    package = {
        "schema_version": "resume_package_v1",
        "accumulated_clean_export": {"variants": variants},
        "batch_state": {"processed_seed_ids": [], "failed_seed_ids": []},
        "verified_variants": [],   # intentionally wrong
        "partial_variants": [],    # intentionally wrong
    }

    result = batch_runner.rebuild_canonical_metadata_from_accumulated(package, seeds)

    assert len(result["verified_variants"]) == 10
    assert len(result["partial_variants"]) == 5


# ---------------------------------------------------------------------------
# 3. Accumulated variant count must not shrink after metadata rebuild
# ---------------------------------------------------------------------------

def test_no_variant_shrink_after_metadata_rebuild():
    """432 accumulated variants must remain 432 after rebuild."""
    seeds = [_make_seed(f"audi__m{i}__2000__2026__il", "Audi") for i in range(50)]
    variants = [_generic_variant(i, "Audi", "verified" if i % 2 == 0 else "partial") for i in range(432)]
    package = {
        "schema_version": "resume_package_v1",
        "accumulated_clean_export": {"variants": variants},
        "batch_state": {"processed_seed_ids": [], "failed_seed_ids": []},
    }

    result = batch_runner.rebuild_canonical_metadata_from_accumulated(package, seeds)

    acc_variants = result["accumulated_clean_export"]["variants"]
    assert len(acc_variants) == 432, "accumulated_clean_export.variants must not shrink after rebuild"


# ---------------------------------------------------------------------------
# 4. Processed-state continuation pointers are unchanged
# ---------------------------------------------------------------------------

def test_processed_state_unchanged():
    """last_completed_seed_id, next_seed_id and processed_seed_ids must survive rebuild."""
    seeds = [
        _make_seed("bmw__518i__1990__1996__il", "BMW"),
        _make_seed("bmw__520d__2008__2020__il", "BMW"),
    ]
    original_state = {
        "processed_seed_ids": ["bmw__518i__1990__1996__il"],
        "last_completed_seed_id": "bmw__518i__1990__1996__il",
        "next_seed_id": "bmw__520d__2008__2020__il",
        "failed_seed_ids": [],
    }
    package = {
        "schema_version": "resume_package_v1",
        "accumulated_clean_export": {"variants": [_bmw_variant(1)]},
        "batch_state": copy.deepcopy(original_state),
    }

    result = batch_runner.rebuild_canonical_metadata_from_accumulated(package, seeds)

    bs = result["batch_state"]
    assert bs["last_completed_seed_id"] == "bmw__518i__1990__1996__il"
    assert bs["next_seed_id"] == "bmw__520d__2008__2020__il"
    assert bs["processed_seed_ids"] == ["bmw__518i__1990__1996__il"]


# ---------------------------------------------------------------------------
# 5. BMW zero-coverage regression guard
# ---------------------------------------------------------------------------

def test_bmw_zero_coverage_regression():
    """coverage_by_make BMW must NOT show zero verified+partial when BMW variants exist."""
    bmw_seeds = [
        _make_seed("bmw__518i__1990__1996__il", "BMW"),
        _make_seed("bmw__520d__2008__2020__il", "BMW"),
    ]
    # Simulate stale/wrong state where coverage was built from empty run_history
    stale_coverage = {
        "BMW": {
            "total": 2,
            "processed": 1,
            "verified_variants": 0,
            "partial_variants": 0,
            "unresolved": 0,
            "failed": 0,
            "completed": False,
        }
    }
    package = {
        "schema_version": "resume_package_v1",
        "accumulated_clean_export": {
            "variants": [
                _bmw_variant(1, "verified"),
                _bmw_variant(2, "verified"),
                _bmw_variant(3, "partial"),
            ]
        },
        "batch_state": {
            "processed_seed_ids": ["bmw__518i__1990__1996__il"],
            "failed_seed_ids": [],
            "coverage_by_make": stale_coverage,
        },
    }

    result = batch_runner.rebuild_canonical_metadata_from_accumulated(package, bmw_seeds)

    bmw_cov = result["batch_state"]["coverage_by_make"]["BMW"]
    assert not (bmw_cov["verified_variants"] == 0 and bmw_cov["partial_variants"] == 0), (
        "BMW coverage must not both be zero when BMW variants exist in accumulated"
    )
    assert bmw_cov["verified_variants"] == 2
    assert bmw_cov["partial_variants"] == 1


# ---------------------------------------------------------------------------
# 6. _validate_canonical_coverage_sync detects mismatch
# ---------------------------------------------------------------------------

def test_validate_coverage_sync_detects_mismatch():
    """_validate_canonical_coverage_sync must return warnings when coverage is stale."""
    package = {
        "accumulated_clean_export": {
            "variants": [_bmw_variant(1, "verified"), _bmw_variant(2, "partial")]
        },
        "verified_variants": [],
        "partial_variants": [],
        "batch_state": {
            "coverage_by_make": {
                "BMW": {"total": 2, "processed": 1, "verified_variants": 0, "partial_variants": 0, "failed": 0, "completed": False}
            }
        },
    }
    warnings = batch_runner._validate_canonical_coverage_sync(package)
    assert len(warnings) > 0, "Should detect mismatch in stale package"


def test_validate_coverage_sync_clean_package():
    """_validate_canonical_coverage_sync must return no warnings for a consistent package."""
    seeds = [_make_seed("bmw__520d__2008__2020__il", "BMW")]
    package = {
        "accumulated_clean_export": {"variants": [_bmw_variant(1, "verified")]},
        "batch_state": {"processed_seed_ids": [], "failed_seed_ids": []},
    }
    # Rebuild first so it's consistent
    rebuilt = batch_runner.rebuild_canonical_metadata_from_accumulated(package, seeds)
    warnings = batch_runner._validate_canonical_coverage_sync(rebuilt)
    assert warnings == [], f"Unexpected warnings on rebuilt package: {warnings}"

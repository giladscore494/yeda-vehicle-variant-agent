from agent import batch_runner


def test_audit_detects_missing_middle_seed():
    ordered = [
        {"seed_id": "a", "make": "A", "model": "1", "year_start": 1, "year_end": 1},
        {"seed_id": "b", "make": "A", "model": "2", "year_start": 1, "year_end": 1},
        {"seed_id": "c", "make": "A", "model": "3", "year_start": 1, "year_end": 1},
    ]
    state = {"processed_seed_ids": ["a", "c"], "last_completed_seed_id": "c"}
    outputs = {"run_history": [], "unresolved": [], "conflicts": []}
    audit = batch_runner.audit_coverage_until_last_completed(ordered, state, outputs)
    assert audit["holes_count"] == 1
    assert audit["missing_seed_ids"] == ["b"]


def test_detect_import_file_types():
    assert batch_runner.detect_import_file_type({"schema_version": "resume_package_v1"}) == "resume_package"
    assert batch_runner.detect_import_file_type({"processed_seed_ids": []}) == "batch_state"
    assert batch_runner.detect_import_file_type({"batch": {}, "results": []}) == "latest_batch_result"
    assert batch_runner.detect_import_file_type({"schema_version": "vehicle_variants_final_v1", "variants": []}) == "final_export"
    assert batch_runner.detect_import_file_type([{"run_id": "x"}]) == "run_history"


def test_resume_package_contains_required_keys():
    pkg = batch_runner.build_resume_package()
    for key in ["schema_version", "batch_state", "run_history", "verified_variants", "partial_variants", "sources", "unresolved", "conflicts"]:
        assert key in pkg

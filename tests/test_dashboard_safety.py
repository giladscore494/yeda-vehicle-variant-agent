from pathlib import Path

from storage.json_store import load_json_list, safe_get


def test_safe_get_run_history_without_status():
    run = {"run_id": "r1"}
    assert safe_get(run, "status", "n/a") == "n/a"
    assert safe_get(run, "run_id", "n/a") == "r1"


def test_safe_get_raw_gemini_missing_fields():
    record = {"run_id": "r2"}
    assert safe_get(record, "trace", {}) == {}
    assert safe_get(record, "field_verifications", {}) == {}
    assert safe_get(record, "candidate_variants", []) == []


def test_load_json_list_empty_file_does_not_crash(tmp_path: Path):
    p = tmp_path / "empty.json"
    p.write_text("", encoding="utf-8")
    assert load_json_list(p) == []

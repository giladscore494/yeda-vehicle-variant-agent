from agent.batch_runner import build_seed_id, build_final_export, get_ordered_seed_list
from agent import batch_runner


def test_seed_id_stable():
    assert build_seed_id("Abarth", "500", 2008, 2026, "IL") == "abarth__500__2008__2026__il"


def test_ordered_seed_list_deterministic():
    ordered = get_ordered_seed_list("IL")
    keys = [(s["make"].lower(), s["model"].lower(), s["year_start"], s["year_end"]) for s in ordered]
    assert keys == sorted(keys)


def test_build_final_export_shape():
    payload = build_final_export(include_partial=True, include_verified=True)
    assert payload["schema_version"] == "vehicle_variants_final_v2"
    assert "variants" in payload and isinstance(payload["variants"], list)


def test_load_all_accumulated_variants_includes_combined_clean(monkeypatch):
    monkeypatch.setattr(batch_runner, "get_output_paths", lambda: {"vehicle_variants_verified": "vv", "vehicle_variants_partial": "vp", "vehicle_sources": "vs", "run_history": "rh"})
    monkeypatch.setattr(batch_runner, "load_json_list", lambda p: [] if p != "rh" else [])
    def _obj(path):
        s = str(path)
        if s.endswith("combined_vehicle_variants_final_clean.json"):
            return {"variants": [{"variant_id": "v1", "classification": "verified"}]}
        return {}
    monkeypatch.setattr(batch_runner, "load_json_object", _obj)
    loaded = batch_runner.load_all_accumulated_variants()
    assert loaded["inputs_loaded"]["combined_clean"] == 1
    assert len(loaded["verified"]) == 1


def test_resume_package_includes_accumulated_clean_export(monkeypatch):
    monkeypatch.setattr(batch_runner, "get_output_paths", lambda: {"run_history": "rh", "vehicle_variants_verified": "vv", "vehicle_variants_partial": "vp", "vehicle_sources": "vs", "unresolved_models": "um", "vehicle_conflicts": "vc"})
    monkeypatch.setattr(batch_runner, "load_json_list", lambda p: [])
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": {"processed_seed_ids": []})
    monkeypatch.setattr(batch_runner, "build_final_export", lambda **kwargs: {"variants": [{"variant_id": "v1", "make": "A", "model": "B"}]})
    pkg = batch_runner.build_resume_package()
    assert "accumulated_clean_export" in pkg
    assert pkg["counts"]["total_variants"] == 1

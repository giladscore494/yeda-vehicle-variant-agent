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
    assert batch_runner.detect_import_file_type({"schema_version": "vehicle_variant_resume_package_v1"}) == "resume_package"
    assert batch_runner.detect_import_file_type({"batch_state": {}, "final_export": {"variants": []}}) == "resume_package"
    assert batch_runner.detect_import_file_type({"processed_seed_ids": []}) == "batch_state"
    assert batch_runner.detect_import_file_type({"batch": {}, "results": []}) == "latest_batch_result"
    assert batch_runner.detect_import_file_type({"schema_version": "vehicle_variants_final_v1", "variants": []}) == "final_export"
    assert batch_runner.detect_import_file_type([{"run_id": "x"}]) == "run_history"


def test_resume_package_contains_required_keys():
    pkg = batch_runner.build_resume_package()
    for key in ["schema_version", "batch_state", "run_history", "verified_variants", "partial_variants", "sources", "unresolved", "conflicts"]:
        assert key in pkg


def test_seed_to_dict_always_includes_market():
    seed = {"seed_id": "x", "make": "A", "model": "B", "year_start": 2000, "year_end": 2001}
    payload = batch_runner.seed_to_dict(seed)
    assert payload["market"] == "IL"


def test_audit_missing_seeds_include_market():
    ordered = [
        {"seed_id": "a", "make": "A", "model": "1", "year_start": 1, "year_end": 1, "market": "IL"},
        {"seed_id": "b", "make": "A", "model": "2", "year_start": 1, "year_end": 1, "market": "IL"},
    ]
    state = {"processed_seed_ids": ["a"], "last_completed_seed_id": "b"}
    outputs = {"run_history": [], "unresolved": [], "conflicts": []}
    audit = batch_runner.audit_coverage_until_last_completed(ordered, state, outputs)
    assert audit["missing_seeds"][0]["market"] == "IL"


def test_run_next_batch_defaults_missing_market_in_hole_repair(monkeypatch):
    seed = {"seed_id": "abarth__punto__2007__2015__il", "make": "Abarth", "model": "Punto", "year_start": 2007, "year_end": 2015}
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": [dict(seed, market="IL")])
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": {"market": market, "processed_seed_ids": [], "failed_seed_ids": [], "failed_details": [], "last_completed_seed_id": seed["seed_id"], "in_progress_seed_id": None})
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(batch_runner, "_save_state", lambda state: None)
    monkeypatch.setattr(batch_runner, "_refresh_coverage", lambda state, ordered: None)
    monkeypatch.setattr(batch_runner, "save_json", lambda *args, **kwargs: None)
    called = {}
    def _fake_run(make, model, year_start, year_end, market, **kwargs):
        called["market"] = market
        return {"status": "completed"}
    monkeypatch.setattr(batch_runner, "run_single_model", _fake_run)
    result = batch_runner.run_next_batch(limit=1, market="IL", resume=True)
    assert result["status"] == "completed"
    assert called["market"] == "IL"


def test_market_error_not_marked_processed(monkeypatch):
    seed = {"seed_id": "s1", "make": "A", "model": "B", "year_start": 1, "year_end": 2}
    state = {"market": "IL", "processed_seed_ids": [], "failed_seed_ids": [], "failed_details": [], "last_completed_seed_id": None}
    monkeypatch.setattr(batch_runner, "_save_state", lambda state: None)
    monkeypatch.setattr(batch_runner, "_refresh_coverage", lambda state, ordered: None)
    monkeypatch.setattr(batch_runner, "run_single_model", lambda *args, **kwargs: (_ for _ in ()).throw(KeyError("market")))
    results = batch_runner._process_seeds([seed], state, [dict(seed, market="IL")], 1)
    assert results[0]["result"]["status"] == "error"
    assert "s1" not in state["processed_seed_ids"]
    assert "s1" in state["failed_seed_ids"]


def test_cleanup_retryable_schema_error(monkeypatch):
    state = {
        "schema_version": batch_runner.BATCH_STATE_SCHEMA,
        "market": "IL",
        "processed_seed_ids": ["abarth__punto__2007__2015__il"],
        "failed_seed_ids": ["abarth__punto__2007__2015__il"],
        "failed_details": [{"seed_id": "abarth__punto__2007__2015__il", "reason": "'market'"}],
        "last_completed_seed_id": "abarth__punto__2007__2015__il",
    }
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": state)
    monkeypatch.setattr(batch_runner, "_save_state", lambda state: None)
    monkeypatch.setattr(batch_runner, "_refresh_coverage", lambda state, ordered: None)
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": [])
    result = batch_runner.cleanup_retryable_schema_errors("IL")
    assert result["cleaned_count"] == 1
    assert "abarth__punto__2007__2015__il" not in state["processed_seed_ids"]


def test_import_accumulated_variants_restores_90(monkeypatch):
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": {"processed_seed_ids": [], "failed_seed_ids": []})
    monkeypatch.setattr(batch_runner, "rebuild_batch_state_from_outputs", lambda market="IL": {})
    store = {
        "vehicle_variants_verified": [],
        "vehicle_variants_partial": [],
    }
    monkeypatch.setattr(batch_runner, "get_output_paths", lambda: {k: k for k in ["vehicle_variants_verified", "vehicle_variants_partial", "run_history", "vehicle_sources", "unresolved_models", "vehicle_conflicts"]})
    monkeypatch.setattr(batch_runner, "load_json_list", lambda path: list(store.get(path, [])))
    monkeypatch.setattr(batch_runner, "save_json", lambda path, data: store.__setitem__(path, data))
    variants = [{"variant_id": f"v{i}", "classification": "verified" if i < 52 else "partial"} for i in range(90)]
    result = batch_runner.import_progress_json(variants)
    assert result["file_type"] == "accumulated_variants"
    assert len(store["vehicle_variants_verified"]) + len(store["vehicle_variants_partial"]) == 90


def test_latest_batch_export_is_separate_file_path():
    from storage.json_store import project_root
    assert str((project_root() / "data/output/latest_batch_result.json")).endswith("latest_batch_result.json")


def test_import_vehicle_variant_resume_package_restores_progress(monkeypatch):
    state = {"processed_seed_ids": [], "failed_seed_ids": []}
    saved = {}
    ordered = [
        {"seed_id": "abarth__124_spider__2016__2020__il", "make": "Abarth", "model": "124 Spider", "year_start": 2016, "year_end": 2020, "market": "IL"},
        {"seed_id": "aiways__u5__2021__2024__il", "make": "Aiways", "model": "U5", "year_start": 2021, "year_end": 2024, "market": "IL"},
        {"seed_id": "alfa_romeo__giulia__2016__2020__il", "make": "Alfa Romeo", "model": "Giulia", "year_start": 2016, "year_end": 2020, "market": "IL"},
        {"seed_id": "alpine__a110__2018__2024__il", "make": "Alpine", "model": "A110", "year_start": 2018, "year_end": 2024, "market": "IL"},
    ]
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": state)
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(batch_runner, "get_output_paths", lambda: {k: k for k in ["vehicle_variants_verified", "vehicle_variants_partial", "run_history", "vehicle_sources", "unresolved_models", "vehicle_conflicts"]})
    monkeypatch.setattr(batch_runner, "load_json_list", lambda path: list(saved.get(path, [])))
    monkeypatch.setattr(batch_runner, "save_json", lambda path, data: saved.__setitem__(str(path), data))
    monkeypatch.setattr(batch_runner, "_batch_state_path", lambda: "batch_state.json")
    monkeypatch.setattr(batch_runner, "rebuild_batch_state_from_outputs", lambda market="IL": {"processed_seed_ids": [], "next_seed_id": "abarth__124_spider__2016__2020__il"})
    variants = [{"variant_id": f"v{i}", "classification": "verified" if i < 60 else "partial"} for i in range(90)]
    pkg = {"schema_version": "vehicle_variant_resume_package_v1", "batch_state": {"processed_seed_ids": ["abarth__124_spider__2016__2020__il", "aiways__u5__2021__2024__il", "alfa_romeo__giulia__2016__2020__il"], "processed_seeds": 3}, "final_export": {"variants": variants, "sources": [], "counts": {"makes_count": 4, "models_count": 30}}}
    out = batch_runner.import_progress_json(pkg)
    assert out["file_type"] == "resume_package"
    assert out["imported_variants"] == 90
    assert len(saved["batch_state.json"]["processed_seed_ids"]) == 3
    assert saved["batch_state.json"]["last_completed_seed_id"] is not None
    assert saved["batch_state.json"]["next_seed_id"] != "abarth__124_spider__2016__2020__il"
    assert len(saved["vehicle_variants_verified"]) + len(saved["vehicle_variants_partial"]) == 90
    assert "imported_accumulated_dataset.json" in " ".join(saved.keys())


def test_normalize_batch_state_recomputes_next_seed_and_shapes_processed_seeds():
    ordered = [
        {"seed_id": "abarth__500__2008__2026__il", "make": "Abarth", "model": "500", "year_start": 2008, "year_end": 2026, "market": "IL"},
        {"seed_id": "abarth__500e__2023__2026__il", "make": "Abarth", "model": "500e", "year_start": 2023, "year_end": 2026, "market": "IL"},
        {"seed_id": "aston_martin__db9__2004__2016__il", "make": "Aston Martin", "model": "DB9", "year_start": 2004, "year_end": 2016, "market": "IL"},
    ]
    dirty = {
        "processed_seed_ids": ["abarth__500__2008__2026__il"],
        "processed_seeds": [{"seed_id": "abarth__500__2008__2016__il"}],
        "next_seed_id": "abarth__500__2008__2026__il",
        "failed_seed_ids": ["abarth__500__2008__2026__il", "aston_martin__db9__2004__2016__il"],
        "failed_details": [{"seed_id": "abarth__500__2008__2026__il", "reason": "x"}],
    }
    out = batch_runner.normalize_batch_state_for_resume(dirty, ordered, market="IL")
    assert out["next_seed_id"] == "abarth__500e__2023__2026__il"
    assert out["next_seed_id"] not in out["processed_seed_ids"]
    assert len(out["processed_seeds"]) == len(out["processed_seed_ids"])
    assert out["processed_seeds"][0]["seed_id"] == out["processed_seed_ids"][0]
    assert "abarth__500__2008__2026__il" not in out["failed_seed_ids"]
    assert out["failed_details"] == []


def test_normalize_batch_state_maps_legacy_split_with_variants():
    ordered = [
        {"seed_id": "abarth__500__2008__2026__il", "make": "Abarth", "model": "500", "year_start": 2008, "year_end": 2026, "market": "IL"},
        {"seed_id": "abarth__500e__2023__2026__il", "make": "Abarth", "model": "500e", "year_start": 2023, "year_end": 2026, "market": "IL"},
    ]
    dirty = {"processed_seeds": [{"seed_id": "abarth__500__2008__2016__il"}, {"seed_id": "abarth__500__2024__2026__il"}]}
    variants = [{"variant_id": "v1", "make": "Abarth", "model": "500", "year_start": 2014, "year_end": 2016}]
    out = batch_runner.normalize_batch_state_for_resume(dirty, ordered, variants=variants, market="IL")
    assert out["processed_seed_ids"] == ["abarth__500__2008__2026__il"]
    assert out["processed_seeds"][0]["seed_id"] == "abarth__500__2008__2026__il"

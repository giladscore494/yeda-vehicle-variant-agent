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

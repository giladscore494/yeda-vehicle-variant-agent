from pathlib import Path

from agent import batch_runner


def _variant(idx: int, status: str = "verified", make: str = "Audi", model: str = "Q7"):
    return {
        "variant_id": f"v-{idx}",
        "make": make,
        "model": model,
        "market": "IL",
        "year_start": 2000 + (idx % 20),
        "year_end": 2026,
        "generation": f"g{idx}",
        "body_type": {"value": "SUV", "status": status, "sources_count": 2 if status == "verified" else 1, "source_ids": ["s1", "s2"]},
        "seats": {"value": 7, "status": "partial", "sources_count": 1, "source_ids": ["s1"]},
        "engine": {"value": f"eng{idx}", "status": status, "sources_count": 2 if status == "verified" else 1, "source_ids": ["s1", "s2"]},
        "transmission": {"value": "AT", "status": "partial", "sources_count": 1, "source_ids": ["s1"]},
        "fuel_type": {"value": "Diesel", "status": "partial", "sources_count": 1, "source_ids": ["s1"]},
        "drivetrain": {"value": "AWD", "status": "partial", "sources_count": 1, "source_ids": ["s1"]},
        "trim": {"value": f"trim-{idx}", "status": status, "sources_count": 1, "source_ids": ["s1"]},
        "verification_status": status,
        "classification": status,
    }


def _setup_paths(monkeypatch):
    root = Path("/repo")
    output = root / "data/output"
    paths = {
        "vehicle_variants_verified": output / "vehicle_variants_verified.json",
        "vehicle_variants_partial": output / "vehicle_variants_partial.json",
        "vehicle_conflicts": output / "vehicle_conflicts.json",
        "vehicle_sources": output / "vehicle_sources.json",
        "unresolved_models": output / "unresolved_models.json",
        "run_history": output / "run_history.json",
    }
    monkeypatch.setattr(batch_runner, "project_root", lambda: root)
    monkeypatch.setattr(batch_runner, "get_output_paths", lambda: paths)
    return root, paths


def _setup_storage(monkeypatch, object_store: dict, list_store: dict):
    saved = {}

    def _load_obj(path):
        return object_store.get(str(path), {})

    def _load_list(path):
        return list_store.get(str(path), [])

    def _save(path, data):
        saved[str(path)] = data

    monkeypatch.setattr(batch_runner, "load_json_object", _load_obj)
    monkeypatch.setattr(batch_runner, "load_json_list", _load_list)
    monkeypatch.setattr(batch_runner, "save_json", _save)
    return saved


def test_resume_export_never_shrinks_imported_dataset(monkeypatch):
    root, paths = _setup_paths(monkeypatch)
    imported = [_variant(i, "verified") for i in range(129)]
    verified = [_variant(1000, "verified")]
    object_store = {
        str(root / "data/output/imported_accumulated_dataset.json"): {"variants": imported},
        str(root / "data/output/combined_vehicle_variants_final_clean.json"): {},
        str(root / "data/output/combined_vehicle_variants_final.json"): {},
        str(root / "data/output/latest_batch_result.json"): {"results": []},
    }
    list_store = {
        str(paths["vehicle_variants_verified"]): verified,
        str(paths["vehicle_variants_partial"]): [],
        str(paths["vehicle_sources"]): [],
        str(paths["run_history"]): [],
        str(paths["vehicle_conflicts"]): [],
        str(paths["unresolved_models"]): [],
    }
    _setup_storage(monkeypatch, object_store, list_store)
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": {"processed_seed_ids": []})
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": [])
    pkg = batch_runner.build_resume_package()
    assert len(pkg["accumulated_clean_export"]["variants"]) >= 129


def test_resume_export_merges_imported_plus_new(monkeypatch):
    root, paths = _setup_paths(monkeypatch)
    imported = [_variant(i, "verified") for i in range(129)]
    verified_new = [_variant(2000 + i, "verified", model="Q8") for i in range(20)]
    object_store = {
        str(root / "data/output/imported_accumulated_dataset.json"): {"variants": imported},
        str(root / "data/output/combined_vehicle_variants_final_clean.json"): {},
        str(root / "data/output/combined_vehicle_variants_final.json"): {},
        str(root / "data/output/latest_batch_result.json"): {"results": []},
    }
    list_store = {
        str(paths["vehicle_variants_verified"]): verified_new,
        str(paths["vehicle_variants_partial"]): [],
        str(paths["vehicle_sources"]): [],
        str(paths["run_history"]): [],
        str(paths["vehicle_conflicts"]): [],
        str(paths["unresolved_models"]): [],
    }
    _setup_storage(monkeypatch, object_store, list_store)
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": {"processed_seed_ids": []})
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": [])
    pkg = batch_runner.build_resume_package()
    assert len(pkg["accumulated_clean_export"]["variants"]) >= 149


def test_import_resume_package_merges_not_overwrites(monkeypatch):
    root, paths = _setup_paths(monkeypatch)
    existing = [_variant(i, "verified") for i in range(129)]
    uploaded = [_variant(i, "verified") for i in range(94)]
    object_store = {
        str(root / "data/output/imported_accumulated_dataset.json"): {"variants": existing},
    }
    list_store = {
        str(paths["vehicle_variants_verified"]): [],
        str(paths["vehicle_variants_partial"]): [],
        str(paths["vehicle_sources"]): [],
        str(paths["run_history"]): [],
        str(paths["vehicle_conflicts"]): [],
        str(paths["unresolved_models"]): [],
    }
    saved = _setup_storage(monkeypatch, object_store, list_store)
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": {"processed_seed_ids": [], "failed_seed_ids": []})
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": [])
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(batch_runner, "normalize_batch_state_for_resume", lambda *args, **kwargs: {"processed_seed_ids": []})
    monkeypatch.setattr(batch_runner, "_batch_state_path", lambda: "batch_state.json")
    monkeypatch.setattr(batch_runner, "audit_coverage_until_last_completed", lambda *args, **kwargs: {})
    pkg = {"schema_version": "resume_package_v1", "accumulated_clean_export": {"variants": uploaded}, "batch_state": {"processed_seed_ids": []}}
    batch_runner.import_progress_json(pkg, overwrite=False)
    imported_saved = saved[str(root / "data/output/imported_accumulated_dataset.json")]
    assert len(imported_saved["variants"]) >= 129


def test_import_resume_package_overwrite_requires_explicit_true(monkeypatch):
    root, paths = _setup_paths(monkeypatch)
    existing = [_variant(i, "verified") for i in range(129)]
    uploaded = [_variant(i, "verified") for i in range(94)]
    object_store = {
        str(root / "data/output/imported_accumulated_dataset.json"): {"variants": existing},
    }
    list_store = {
        str(paths["vehicle_variants_verified"]): [],
        str(paths["vehicle_variants_partial"]): [],
        str(paths["vehicle_sources"]): [],
        str(paths["run_history"]): [],
        str(paths["vehicle_conflicts"]): [],
        str(paths["unresolved_models"]): [],
    }
    saved = _setup_storage(monkeypatch, object_store, list_store)
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": {"processed_seed_ids": [], "failed_seed_ids": []})
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": [])
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(batch_runner, "normalize_batch_state_for_resume", lambda *args, **kwargs: {"processed_seed_ids": []})
    monkeypatch.setattr(batch_runner, "_batch_state_path", lambda: "batch_state.json")
    monkeypatch.setattr(batch_runner, "audit_coverage_until_last_completed", lambda *args, **kwargs: {})
    pkg = {"schema_version": "resume_package_v1", "accumulated_clean_export": {"variants": uploaded}, "batch_state": {"processed_seed_ids": []}}
    keep_result = batch_runner.import_progress_json(pkg, overwrite=False)
    assert any("prevent shrink" in w.lower() for w in keep_result.get("warnings", []))
    overwrite_result = batch_runner.import_progress_json(pkg, overwrite=True)
    imported_saved = saved[str(root / "data/output/imported_accumulated_dataset.json")]
    assert len(imported_saved["variants"]) == 94
    assert any("destructive overwrite" in w.lower() for w in overwrite_result.get("warnings", []))


def test_latest_batch_raw_candidates_not_counted_as_final_variants(monkeypatch):
    root, paths = _setup_paths(monkeypatch)
    object_store = {
        str(root / "data/output/imported_accumulated_dataset.json"): {},
        str(root / "data/output/combined_vehicle_variants_final_clean.json"): {},
        str(root / "data/output/combined_vehicle_variants_final.json"): {},
        str(root / "data/output/latest_batch_result.json"): {
            "results": [
                {
                    "result": {
                        "trace": {
                            "discovery_parsed_json_debug": {
                                "candidate_variants": [{"make": "Audi", "model": "Q7", "year_start": 2005, "year_end": 2026, "market": "IL"}]
                            }
                        }
                    }
                }
            ]
        },
    }
    list_store = {
        str(paths["vehicle_variants_verified"]): [],
        str(paths["vehicle_variants_partial"]): [],
        str(paths["vehicle_sources"]): [],
        str(paths["run_history"]): [],
        str(paths["vehicle_conflicts"]): [],
        str(paths["unresolved_models"]): [],
    }
    _setup_storage(monkeypatch, object_store, list_store)
    loaded = batch_runner.load_all_accumulated_variants()
    assert loaded["inputs_loaded"]["latest_batch_full_variants"] == 0
    assert loaded["verified"] == []
    assert loaded["partial"] == []


def test_verified_preferred_over_partial(monkeypatch):
    root, paths = _setup_paths(monkeypatch)
    base = _variant(1, "verified")
    duplicate_partial = dict(base)
    duplicate_partial["verification_status"] = "partial"
    duplicate_partial["classification"] = "partial"
    object_store = {
        str(root / "data/output/imported_accumulated_dataset.json"): {"variants": []},
        str(root / "data/output/combined_vehicle_variants_final_clean.json"): {},
        str(root / "data/output/combined_vehicle_variants_final.json"): {},
        str(root / "data/output/latest_batch_result.json"): {"results": []},
    }
    list_store = {
        str(paths["vehicle_variants_verified"]): [base],
        str(paths["vehicle_variants_partial"]): [duplicate_partial],
        str(paths["vehicle_sources"]): [],
        str(paths["run_history"]): [],
        str(paths["vehicle_conflicts"]): [],
        str(paths["unresolved_models"]): [],
    }
    _setup_storage(monkeypatch, object_store, list_store)
    payload = batch_runner.build_final_export()
    variants = [v for v in payload.get("variants", []) if v.get("variant_id") == base["variant_id"]]
    assert len(variants) == 1
    assert variants[0].get("verification_status") == "verified"


def test_resume_package_after_audi_q7(monkeypatch):
    root, paths = _setup_paths(monkeypatch)
    imported = [_variant(i, "verified") for i in range(129)]
    new_variants = [_variant(3000 + i, "verified", model="Q7") for i in range(5)]
    object_store = {
        str(root / "data/output/imported_accumulated_dataset.json"): {"variants": imported},
        str(root / "data/output/combined_vehicle_variants_final_clean.json"): {},
        str(root / "data/output/combined_vehicle_variants_final.json"): {},
        str(root / "data/output/latest_batch_result.json"): {"results": []},
    }
    list_store = {
        str(paths["vehicle_variants_verified"]): new_variants,
        str(paths["vehicle_variants_partial"]): [],
        str(paths["vehicle_sources"]): [],
        str(paths["run_history"]): [],
        str(paths["vehicle_conflicts"]): [],
        str(paths["unresolved_models"]): [],
    }
    _setup_storage(monkeypatch, object_store, list_store)
    state = {
        "processed_seed_ids": [f"s{i}" for i in range(53)],
        "last_completed_seed_id": "audi__q7__2005__2026__il",
        "next_seed_id": "audi__q8__2018__2026__il",
    }
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": state)
    monkeypatch.setattr(batch_runner, "normalize_batch_state_for_resume", lambda batch_state, ordered_seeds, variants=None, market="IL": batch_state)
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": [])
    pkg = batch_runner.build_resume_package()
    assert pkg["batch_state"]["last_completed_seed_id"] == "audi__q7__2005__2026__il"
    assert pkg["batch_state"]["next_seed_id"] == "audi__q8__2018__2026__il"
    assert len(pkg["accumulated_clean_export"]["variants"]) > 129


def test_resume_package_shrink_guard_blocks(monkeypatch):
    monkeypatch.setattr(batch_runner, "get_output_paths", lambda: {"run_history": "rh", "vehicle_variants_verified": "vv", "vehicle_variants_partial": "vp", "vehicle_sources": "vs", "unresolved_models": "um", "vehicle_conflicts": "vc"})
    monkeypatch.setattr(batch_runner, "load_json_list", lambda p: [])
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": {"processed_seed_ids": []})
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": [])
    monkeypatch.setattr(
        batch_runner,
        "build_final_export",
        lambda **kwargs: {
            "variants": [_variant(1)],
            "audit": {"accumulation_counts": {"shrink_guard_previous_count": 129, "shrink_guard_new_count": 94}},
        },
    )
    try:
        batch_runner.build_resume_package()
        assert False, "Expected shrink guard to raise"
    except ValueError as exc:
        assert "shrink detected" in str(exc).lower()

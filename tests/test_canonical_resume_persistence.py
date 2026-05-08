from pathlib import Path

from agent import batch_runner


def _variant(idx: int, status: str = "verified"):
    return {
        "variant_id": f"v-{idx}",
        "make": "Audi",
        "model": "Q7",
        "market": "IL",
        "year_start": 2005,
        "year_end": 2026,
        "generation": "g1",
        "body_type": {"value": "SUV", "status": status, "sources_count": 2, "source_ids": ["s1"]},
        "seats": {"value": 7, "status": "partial", "sources_count": 1, "source_ids": ["s1"]},
        "engine": {"value": f"e{idx}", "status": status, "sources_count": 2, "source_ids": ["s1"]},
        "transmission": {"value": "AT", "status": "partial", "sources_count": 1, "source_ids": ["s1"]},
        "fuel_type": {"value": "Diesel", "status": "partial", "sources_count": 1, "source_ids": ["s1"]},
        "drivetrain": {"value": "AWD", "status": "partial", "sources_count": 1, "source_ids": ["s1"]},
        "trim": {"value": f"trim-{idx}", "status": status, "sources_count": 1, "source_ids": ["s1"]},
        "verification_status": status,
        "classification": status,
    }


def test_validate_canonical_resume_package_update_blocks_shrink(monkeypatch):
    monkeypatch.setattr(
        batch_runner,
        "get_ordered_seed_list",
        lambda market="IL": [
            {"seed_id": "a", "make": "A", "model": "M", "year_start": 1, "year_end": 1, "market": "IL"},
            {"seed_id": "b", "make": "B", "model": "M", "year_start": 1, "year_end": 1, "market": "IL"},
        ],
    )
    prev = {"accumulated_clean_export": {"variants": [_variant(1), _variant(2)]}, "batch_state": {"processed_seed_ids": ["a"], "last_completed_seed_id": "a"}}
    new = {"accumulated_clean_export": {"variants": [_variant(1)], "quality_gate": {"passed": True}}, "batch_state": {"processed_seed_ids": ["a"], "last_completed_seed_id": "a", "next_seed_id": "a"}}
    issues = batch_runner.validate_canonical_resume_package_update(new, prev, market="IL")
    assert "candidate_variant_count < previous_variant_count" in issues
    assert "candidate_next_seed_id is already processed" in issues


def test_import_resume_package_saves_local_canonical(monkeypatch):
    root = Path("/repo")
    output = root / "data/output"
    canonical_path = root / "data/canonical/resume_package_canonical.json"
    paths = {
        "vehicle_variants_verified": output / "vehicle_variants_verified.json",
        "vehicle_variants_partial": output / "vehicle_variants_partial.json",
        "vehicle_conflicts": output / "vehicle_conflicts.json",
        "vehicle_sources": output / "vehicle_sources.json",
        "unresolved_models": output / "unresolved_models.json",
        "run_history": output / "run_history.json",
    }
    store_obj = {str(root / "data/output/imported_accumulated_dataset.json"): {"variants": [_variant(1)]}}
    store_list = {str(v): [] for v in paths.values()}
    saved = {}

    monkeypatch.setattr(batch_runner, "project_root", lambda: root)
    monkeypatch.setattr(batch_runner, "get_output_paths", lambda: paths)
    monkeypatch.setattr(batch_runner, "get_github_config", lambda: {"canonical_path": "data/canonical/resume_package_canonical.json", "backup_path": "data/canonical/resume_package_backup_previous.json"})
    monkeypatch.setattr(batch_runner, "load_json_object", lambda p: store_obj.get(str(p), {}))
    monkeypatch.setattr(batch_runner, "load_json_list", lambda p: store_list.get(str(p), []))
    monkeypatch.setattr(batch_runner, "save_json", lambda p, d: saved.__setitem__(str(p), d))
    monkeypatch.setattr(
        batch_runner,
        "get_ordered_seed_list",
        lambda market="IL": [{"seed_id": "audi__q7__2005__2026__il", "make": "Audi", "model": "Q7", "year_start": 2005, "year_end": 2026, "market": "IL"}],
    )
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": {"processed_seed_ids": []})
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(batch_runner, "_batch_state_path", lambda: "batch_state.json")
    monkeypatch.setattr(batch_runner, "audit_coverage_until_last_completed", lambda *args, **kwargs: {})

    pkg = {
        "schema_version": "resume_package_v1",
        "batch_state": {"processed_seed_ids": ["audi__q7__2005__2026__il"]},
        "variants": [_variant(2)],
    }
    out = batch_runner.import_progress_json(pkg, overwrite=False)
    assert out["file_type"] == "resume_package"
    assert str(canonical_path) in saved


def test_persist_canonical_blocks_invalid_update(monkeypatch):
    monkeypatch.setattr(batch_runner, "build_resume_package", lambda: {"accumulated_clean_export": {"variants": [_variant(1)]}, "batch_state": {"processed_seed_ids": ["a"]}})
    monkeypatch.setattr(batch_runner, "load_local_canonical_resume_package", lambda: {"accumulated_clean_export": {"variants": [_variant(1), _variant(2)]}, "batch_state": {"processed_seed_ids": ["a", "b"]}})
    monkeypatch.setattr(batch_runner, "fetch_file_from_github", lambda *args, **kwargs: None)
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": [{"seed_id": "a", "make": "A", "model": "M", "year_start": 1, "year_end": 1, "market": "IL"}])
    called = {"save": 0}
    monkeypatch.setattr(batch_runner, "save_local_canonical_resume_package", lambda p: called.__setitem__("save", called["save"] + 1))

    result = batch_runner.persist_canonical_resume_package(push_to_github=False)
    assert result["ok"] is False
    assert called["save"] == 0


def test_manual_push_uses_local_canonical_not_rebuild(monkeypatch):
    local_package = {
        "schema_version": "resume_package_v1",
        "variants": [_variant(i) for i in range(263)],
        "batch_state": {"processed_seed_ids": [f"s-{i}" for i in range(59)], "next_seed_id": "audi__rs6__2008__2026__il"},
    }
    pushed = {"count": 0}

    monkeypatch.setattr(batch_runner, "load_local_canonical_resume_package", lambda: local_package)
    monkeypatch.setattr(batch_runner, "build_final_export", lambda: {"variants": [_variant(i) for i in range(116)]})
    monkeypatch.setattr(batch_runner, "fetch_file_from_github", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        batch_runner,
        "push_canonical_resume_package",
        lambda package, previous_package=None, batch_id=None: pushed.update({"count": len(package.get("variants", []))}) or {"ok": True, "canonical": {"commit_sha": "abc"}},
    )
    monkeypatch.setattr(batch_runner, "save_local_canonical_resume_package", lambda package: None)

    result = batch_runner.push_local_canonical_to_github()
    assert result["ok"] is True
    assert pushed["count"] == 263

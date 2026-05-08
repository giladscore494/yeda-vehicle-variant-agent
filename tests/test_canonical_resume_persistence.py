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


def _make_batch_env(monkeypatch, seeds, initial_state, persist_side_effect):
    """Shared setup for run_next_batch canonical-persistence tests."""
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(batch_runner, "load_local_canonical_resume_package", lambda: None)
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": dict(initial_state))
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(batch_runner, "_save_state", lambda state: None)
    monkeypatch.setattr(batch_runner, "_refresh_coverage", lambda state, ordered: None)
    monkeypatch.setattr(batch_runner, "save_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(batch_runner, "evaluate_continue_guard", lambda market="IL": {"passed": True, "issues": [], "coverage_audit": {"holes_count": 0}})
    monkeypatch.setattr(batch_runner, "run_single_model", lambda make, model, year_start, year_end, market, **kw: {"status": "completed", "variants_created": 1})
    monkeypatch.setattr(batch_runner, "persist_canonical_resume_package", persist_side_effect)


def test_run_next_batch_auto_push_false_still_persists_local(monkeypatch):
    """auto_push_canonical=False must still trigger local canonical persistence."""
    seeds = [
        {"seed_id": "audi__rs5__2010__2026__il", "make": "Audi", "model": "RS5", "year_start": 2010, "year_end": 2026, "market": "IL"},
        {"seed_id": "audi__rs6__2008__2026__il", "make": "Audi", "model": "RS6", "year_start": 2008, "year_end": 2026, "market": "IL"},
    ]
    initial_state = {
        "market": "IL", "processed_seed_ids": ["audi__rs5__2010__2026__il"],
        "failed_seed_ids": [], "failed_details": [], "in_progress_seed_id": None,
        "last_completed_seed_id": "audi__rs5__2010__2026__il",
    }
    persist_calls = []

    def _fake_persist(batch_id=None, push_to_github=False, market="IL"):
        persist_calls.append({"push_to_github": push_to_github})
        return {"ok": True, "issues": [], "validate_result": {}, "package": {}, "push_result": None}

    _make_batch_env(monkeypatch, seeds, initial_state, _fake_persist)
    result = batch_runner.run_next_batch(limit=1, market="IL", resume=False, auto_push_canonical=False)

    assert result["status"] == "completed"
    assert len(persist_calls) == 1, "persist_canonical_resume_package must be called even when auto_push_canonical=False"
    assert persist_calls[0]["push_to_github"] is False
    assert result["canonical_persist"] is not None


def test_run_next_batch_auto_push_true_calls_github_push(monkeypatch):
    """auto_push_canonical=True must trigger persist with push_to_github=True."""
    seeds = [
        {"seed_id": "audi__rs6__2008__2026__il", "make": "Audi", "model": "RS6", "year_start": 2008, "year_end": 2026, "market": "IL"},
    ]
    initial_state = {
        "market": "IL", "processed_seed_ids": [],
        "failed_seed_ids": [], "failed_details": [], "in_progress_seed_id": None,
        "last_completed_seed_id": None,
    }
    persist_calls = []

    def _fake_persist(batch_id=None, push_to_github=False, market="IL"):
        persist_calls.append({"push_to_github": push_to_github})
        return {"ok": True, "issues": [], "validate_result": {}, "package": {}, "push_result": {"ok": True}}

    _make_batch_env(monkeypatch, seeds, initial_state, _fake_persist)
    result = batch_runner.run_next_batch(limit=1, market="IL", resume=False, auto_push_canonical=True)

    assert result["status"] == "completed"
    assert len(persist_calls) == 1
    assert persist_calls[0]["push_to_github"] is True
    assert result["canonical_persist"]["push_result"]["ok"] is True


def test_run_next_batch_no_results_does_not_call_persist(monkeypatch):
    """When queue is empty, persist_canonical_resume_package must NOT be called."""
    seeds = [
        {"seed_id": "audi__rs5__2010__2026__il", "make": "Audi", "model": "RS5", "year_start": 2010, "year_end": 2026, "market": "IL"},
    ]
    # All seeds already processed — queue will be empty
    initial_state = {
        "market": "IL", "processed_seed_ids": ["audi__rs5__2010__2026__il"],
        "failed_seed_ids": [], "failed_details": [], "in_progress_seed_id": None,
        "last_completed_seed_id": "audi__rs5__2010__2026__il",
    }
    persist_calls = []

    def _fake_persist(**kwargs):
        persist_calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(batch_runner, "load_local_canonical_resume_package", lambda: None)
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": dict(initial_state))
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(batch_runner, "_save_state", lambda state: None)
    monkeypatch.setattr(batch_runner, "_refresh_coverage", lambda state, ordered: None)
    monkeypatch.setattr(batch_runner, "save_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(batch_runner, "evaluate_continue_guard", lambda market="IL": {"passed": True, "issues": [], "coverage_audit": {"holes_count": 0}})
    monkeypatch.setattr(batch_runner, "persist_canonical_resume_package", _fake_persist)

    result = batch_runner.run_next_batch(limit=1, market="IL", resume=True, auto_push_canonical=False)
    assert result["status"] == "completed_all"
    assert len(persist_calls) == 0


def test_canonical_advances_next_seed_id_after_rs5(monkeypatch):
    """Regression: after processing RS6, next_seed_id must advance beyond RS6."""
    seeds = [
        {"seed_id": "audi__rs5__2010__2026__il", "make": "Audi", "model": "RS5", "year_start": 2010, "year_end": 2026, "market": "IL"},
        {"seed_id": "audi__rs6__2008__2026__il", "make": "Audi", "model": "RS6", "year_start": 2008, "year_end": 2026, "market": "IL"},
        {"seed_id": "audi__rs7__2014__2026__il", "make": "Audi", "model": "RS7", "year_start": 2014, "year_end": 2026, "market": "IL"},
    ]
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})

    # Previous canonical: stopped after RS5, next is RS6
    previous = {
        "schema_version": "resume_package_v1",
        "accumulated_clean_export": {"variants": [_variant(i) for i in range(5)]},
        "batch_state": {
            "processed_seed_ids": ["audi__rs5__2010__2026__il"],
            "last_completed_seed_id": "audi__rs5__2010__2026__il",
            "next_seed_id": "audi__rs6__2008__2026__il",
        },
    }
    # After processing RS6, simulate a new batch state with RS6 processed
    new_batch_state = {
        "processed_seed_ids": ["audi__rs5__2010__2026__il", "audi__rs6__2008__2026__il"],
        "last_completed_seed_id": "audi__rs6__2008__2026__il",
        "next_seed_id": "audi__rs7__2014__2026__il",
    }
    new_variants = [_variant(i) for i in range(7)]

    candidate = batch_runner.build_canonical_candidate(
        previous,
        new_variants,
        new_batch_state=new_batch_state,
        source="merged_candidate",
    )
    bs = candidate.get("batch_state") or {}
    assert bs.get("last_completed_seed_id") == "audi__rs6__2008__2026__il"
    assert bs.get("next_seed_id") == "audi__rs7__2014__2026__il"
    assert bs.get("next_seed_id") not in (bs.get("processed_seed_ids") or []), \
        "next_seed_id must not already be in processed_seed_ids"

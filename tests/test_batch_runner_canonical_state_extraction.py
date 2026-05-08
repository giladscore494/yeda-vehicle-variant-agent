from agent import batch_runner


def _ordered_993() -> list[dict]:
    ordered = []
    for i in range(993):
        ordered.append(
            {
                "seed_id": f"seed__model{i}__2000__2026__il",
                "make": "Seed",
                "model": f"Model{i}",
                "year_start": 2000,
                "year_end": 2026,
                "market": "IL",
            }
        )
    ordered[0]["seed_id"] = "abarth__124_spider__2016__2020__il"
    ordered[58]["seed_id"] = "audi__rs5__2010__2026__il"
    ordered[59]["seed_id"] = "audi__rs6__2008__2026__il"
    return ordered


def _canonical_package(processed_ids: list[str] | None = None, with_processed_ids: bool = True) -> dict:
    ordered = _ordered_993()
    processed = processed_ids if processed_ids is not None else [s["seed_id"] for s in ordered[:59]]
    batch_state = {
        "last_completed_seed_id": "audi__rs5__2010__2026__il",
        "next_seed_id": "audi__rs6__2008__2026__il",
    }
    if with_processed_ids:
        batch_state["processed_seed_ids"] = list(processed)
    return {
        "schema_version": "resume_package_v1",
        "batch_state": batch_state,
        "accumulated_clean_export": {"variants": [{"variant_id": f"v-{i}", "classification": "verified"} for i in range(273)]},
        "verified_variants": [{"variant_id": f"ver-{i}", "classification": "verified"} for i in range(100)],
        "partial_variants": [{"variant_id": f"par-{i}", "classification": "partial"} for i in range(100)],
    }


def test_extract_canonical_batch_state_from_github_canonical(monkeypatch):
    ordered = _ordered_993()
    monkeypatch.setattr(batch_runner, "_refresh_coverage", lambda state, ordered_seeds: state.__setitem__("coverage_by_make", {}))
    package = _canonical_package()
    extracted = batch_runner.extract_canonical_batch_state(package, ordered, market="IL")
    assert len(extracted["processed_seed_ids"]) == 59
    assert extracted["last_completed_seed_id"] == "audi__rs5__2010__2026__il"
    assert extracted["next_seed_id"] == "audi__rs6__2008__2026__il"


def test_batch_runner_progress_uses_canonical_state(monkeypatch):
    ordered = _ordered_993()
    package = _canonical_package()
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(batch_runner, "load_local_canonical_resume_package", lambda: package)
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": {"processed_seed_ids": [], "next_seed_id": ordered[0]["seed_id"]})
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(batch_runner, "_refresh_coverage", lambda state, ordered_seeds: state.__setitem__("coverage_by_make", {}))
    monkeypatch.setattr(batch_runner, "_save_state", lambda state: None)
    progress = batch_runner.get_batch_progress(market="IL")
    assert progress["processed"] == 59
    assert progress["total_seeds"] == 993


def test_continue_guard_uses_canonical_processed_ids(monkeypatch):
    ordered = _ordered_993()
    package = _canonical_package()
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(batch_runner, "load_local_canonical_resume_package", lambda: package)
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": {"processed_seed_ids": []})
    monkeypatch.setattr(batch_runner, "fetch_file_from_github", lambda *args, **kwargs: package)
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(batch_runner, "_refresh_coverage", lambda state, ordered_seeds: state.__setitem__("coverage_by_make", {}))
    monkeypatch.setattr(batch_runner, "_save_state", lambda state: None)
    guard = batch_runner.evaluate_continue_guard(market="IL")
    assert guard["passed"] is True
    assert guard["issues"] == []
    assert guard["processed_seed_count"] == 59


def test_next_seed_not_reset_to_abarth(monkeypatch):
    ordered = _ordered_993()
    package = _canonical_package()
    called = {}
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(batch_runner, "load_local_canonical_resume_package", lambda: package)
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": {"processed_seed_ids": [], "next_seed_id": ordered[0]["seed_id"]})
    monkeypatch.setattr(batch_runner, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(batch_runner, "_refresh_coverage", lambda state, ordered_seeds: state.__setitem__("coverage_by_make", {}))
    monkeypatch.setattr(batch_runner, "_save_state", lambda state: None)
    monkeypatch.setattr(batch_runner, "save_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(batch_runner, "evaluate_continue_guard", lambda market="IL": {"passed": True, "issues": [], "coverage_audit": {"holes_count": 0}})
    def _fake_run(make, model, year_start, year_end, market, **kwargs):
        called["seed_id"] = batch_runner.build_seed_id(make, model, year_start, year_end, market)
        return {"status": "completed"}

    monkeypatch.setattr(batch_runner, "run_single_model", _fake_run)
    out = batch_runner.run_next_batch(limit=1, market="IL", resume=True)
    assert out["status"] == "completed"
    assert called["seed_id"] == "audi__rs6__2008__2026__il"


def test_variant_count_no_double_count():
    package = _canonical_package()
    package["final_export"] = {"variants": [{"variant_id": f"fe-{i}"} for i in range(50)]}
    package["variants"] = [{"variant_id": f"root-{i}"} for i in range(50)]
    assert batch_runner.canonical_variant_count(package) == 273


def test_reconstruct_from_last_completed_fallback(monkeypatch):
    ordered = _ordered_993()
    monkeypatch.setattr(batch_runner, "_refresh_coverage", lambda state, ordered_seeds: state.__setitem__("coverage_by_make", {}))
    package = _canonical_package(with_processed_ids=False)
    extracted = batch_runner.extract_canonical_batch_state(package, ordered, market="IL")
    assert len(extracted["processed_seed_ids"]) == 59
    assert extracted["last_completed_seed_id"] == "audi__rs5__2010__2026__il"
    assert extracted["next_seed_id"] == "audi__rs6__2008__2026__il"

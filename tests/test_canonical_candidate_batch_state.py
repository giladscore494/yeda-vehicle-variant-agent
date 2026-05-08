from agent import batch_runner


def _seed(seed_id: str) -> dict:
    parts = seed_id.split("__")
    return {
        "seed_id": seed_id,
        "make": parts[0].replace("_", " ").title(),
        "model": parts[1].replace("_", " ").title(),
        "year_start": int(parts[2]),
        "year_end": int(parts[3]),
        "market": parts[4].upper(),
    }


def _ordered_seeds() -> list[dict]:
    seeds = [_seed("abarth__124_spider__2016__2020__il")]
    seeds.extend([_seed(f"seedmake__seedmodel{i}__2000__2026__il") for i in range(1, 58)])
    seeds.append(_seed("audi__rs5__2010__2026__il"))
    seeds.append(_seed("audi__rs6__2008__2026__il"))
    seeds.append(_seed("audi__rs7__2013__2026__il"))
    return seeds


def _variant(idx: int) -> dict:
    return {
        "variant_id": f"v-{idx}",
        "make": "Audi",
        "model": "RS",
        "market": "IL",
        "year_start": 2010,
        "year_end": 2026,
        "verification_status": "verified",
        "classification": "verified",
    }


def _previous_package(variant_count: int = 263) -> dict:
    ordered = _ordered_seeds()
    processed = [s["seed_id"] for s in ordered[:59]]
    return {
        "schema_version": "resume_package_v1",
        "accumulated_clean_export": {"variants": [_variant(i) for i in range(variant_count)]},
        "batch_state": {
            "processed_seed_ids": processed,
            "last_completed_seed_id": "audi__rs5__2010__2026__il",
            "next_seed_id": "audi__rs6__2008__2026__il",
        },
    }


def test_merged_candidate_preserves_previous_batch_state(monkeypatch):
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": _ordered_seeds())
    previous = _previous_package(variant_count=263)
    merged_variants = [_variant(i) for i in range(273)]

    candidate = batch_runner.build_canonical_candidate(previous, merged_variants, new_batch_state=None)

    assert len((candidate.get("accumulated_clean_export") or {}).get("variants", [])) == 273
    assert len((candidate.get("batch_state") or {}).get("processed_seed_ids", [])) == 59
    assert candidate["batch_state"]["next_seed_id"] == "audi__rs6__2008__2026__il"


def test_merged_candidate_never_resets_to_first_seed(monkeypatch):
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": _ordered_seeds())
    previous = _previous_package(variant_count=263)
    merged_variants = [_variant(i) for i in range(273)]

    candidate = batch_runner.build_canonical_candidate(previous, merged_variants, new_batch_state=None)

    assert candidate["batch_state"]["next_seed_id"] != "abarth__124_spider__2016__2020__il"
    assert candidate["batch_state"]["next_seed_id"] == "audi__rs6__2008__2026__il"


def test_valid_new_batch_state_can_advance(monkeypatch):
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": _ordered_seeds())
    previous = _previous_package(variant_count=263)
    merged_variants = [_variant(i) for i in range(273)]
    new_state = {
        "processed_seed_ids": [s["seed_id"] for s in _ordered_seeds()[:60]],
        "last_completed_seed_id": "audi__rs6__2008__2026__il",
        "next_seed_id": "audi__rs7__2013__2026__il",
    }

    candidate = batch_runner.build_canonical_candidate(previous, merged_variants, new_batch_state=new_state)

    assert len(candidate["batch_state"]["processed_seed_ids"]) == 60
    assert candidate["batch_state"]["last_completed_seed_id"] == "audi__rs6__2008__2026__il"
    assert candidate["batch_state"]["next_seed_id"] == "audi__rs7__2013__2026__il"


def test_invalid_new_batch_state_rejected(monkeypatch):
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": _ordered_seeds())
    previous = _previous_package(variant_count=263)
    merged_variants = [_variant(i) for i in range(273)]
    invalid_new_state = {
        "processed_seed_ids": [],
        "last_completed_seed_id": None,
        "next_seed_id": "abarth__124_spider__2016__2020__il",
    }

    candidate = batch_runner.build_canonical_candidate(previous, merged_variants, new_batch_state=invalid_new_state)

    assert len(candidate["batch_state"]["processed_seed_ids"]) == 59
    assert candidate["batch_state"]["last_completed_seed_id"] == "audi__rs5__2010__2026__il"
    assert candidate["batch_state"]["next_seed_id"] == "audi__rs6__2008__2026__il"


def test_sync_status_pending_push(monkeypatch):
    previous = _previous_package(variant_count=263)
    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": _ordered_seeds())
    monkeypatch.setattr(batch_runner, "load_local_canonical_resume_package", lambda: previous)
    monkeypatch.setattr(batch_runner, "fetch_file_from_github", lambda *args, **kwargs: _previous_package(variant_count=263))
    monkeypatch.setattr(batch_runner, "load_imported_accumulated_variants", lambda: [])
    monkeypatch.setattr(
        batch_runner,
        "build_final_export",
        lambda: {
            "variants": [_variant(i) for i in range(273)],
            "quality_gate": {"passed": True},
            "audit": {"accumulation_counts": {"final_merged_variants": 273, "latest_batch_full_variants": 0}},
        },
    )

    report = batch_runner.canonical_integrity_report()

    assert report["local_canonical_count"] == 263
    assert report["github_canonical_count"] == 263
    assert report["final_merged_count"] == 273
    assert report["sync_status"] == "pending_push"


def test_push_merged_final_export_as_canonical(monkeypatch):
    previous = _previous_package(variant_count=263)
    pushed = {}

    monkeypatch.setattr(batch_runner, "get_ordered_seed_list", lambda market="IL": _ordered_seeds())
    monkeypatch.setattr(batch_runner, "load_local_canonical_resume_package", lambda: previous)
    monkeypatch.setattr(batch_runner, "fetch_file_from_github", lambda *args, **kwargs: _previous_package(variant_count=263))
    monkeypatch.setattr(
        batch_runner,
        "build_final_export",
        lambda: {
            "variants": [_variant(i) for i in range(273)],
            "quality_gate": {"passed": True},
            "audit": {"accumulation_counts": {"final_merged_variants": 273, "latest_batch_full_variants": 0}},
        },
    )
    monkeypatch.setattr(batch_runner, "load_batch_state", lambda market="IL": {"processed_seed_ids": [], "next_seed_id": "abarth__124_spider__2016__2020__il"})
    monkeypatch.setattr(batch_runner, "save_local_canonical_backup", lambda package: None)
    monkeypatch.setattr(batch_runner, "save_local_canonical_resume_package", lambda package: pushed.setdefault("saved", package))
    def _push(package, previous_package=None, batch_id=None):
        pushed["package"] = package
        return {"ok": True, "canonical": {"commit_sha": "abc"}}

    monkeypatch.setattr(batch_runner, "push_canonical_resume_package", _push)

    result = batch_runner.persist_canonical_resume_package(push_to_github=True)

    assert result["ok"] is True
    assert result["validate_result"]["passed"] is True
    assert len(((pushed.get("package") or {}).get("accumulated_clean_export") or {}).get("variants", [])) == 273
    assert len(((pushed.get("package") or {}).get("batch_state") or {}).get("processed_seed_ids", [])) == 59
    assert (pushed.get("package") or {}).get("batch_state", {}).get("last_completed_seed_id") == "audi__rs5__2010__2026__il"
    assert (pushed.get("package") or {}).get("batch_state", {}).get("next_seed_id") == "audi__rs6__2008__2026__il"

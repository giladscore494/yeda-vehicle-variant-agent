import json
from pathlib import Path

import agent.batch_runner as br
from core.variant_id import generate_variant_id


def _seed(seed_id="s1", make="Honda", model="Civic", ys=2017, ye=2026, market="IL"):
    return {"seed_id": seed_id, "make": make, "model": model, "year_start": ys, "year_end": ye, "market": market}


def _minimal_outputs():
    return {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []}


def test_evaluate_continue_guard_merges_failed_details_dicts_without_crashing(monkeypatch, tmp_path):
    seeds = [_seed("s1")]
    canonical = {
        "batch_state": {
            "processed_seed_ids": [],
            "failed_seed_ids": [],
            "failed_details": [{"seed_id": "s1", "reason": "canonical", "status": "failed_after_retries"}],
            "next_seed_id": "s1",
        },
        "accumulated_clean_export": {"variants": [{"variant_id": "v1", "make": "Honda"}]},
    }
    local_state = {
        "processed_seed_ids": [],
        "failed_seed_ids": [],
        "failed_details": [{"seed_id": "s2", "reason": "local", "status": "failed_after_retries"}],
    }
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: canonical)
    monkeypatch.setattr(br, "load_batch_state", lambda market="IL": local_state)
    monkeypatch.setattr(br, "_load_outputs", lambda: _minimal_outputs())
    monkeypatch.setattr(br, "fetch_file_from_github", lambda *a, **k: {})
    monkeypatch.setattr(br, "get_github_config", lambda: {"canonical_path": ""})
    monkeypatch.setattr(br, "_batch_state_path", lambda: tmp_path / "batch_state.json")
    monkeypatch.setattr(br, "_save_state", lambda s: None)

    out = br.evaluate_continue_guard(market="IL")
    assert isinstance(out, dict)
    assert "issues" in out


def test_persist_batch_state_into_canonical_merges_list_of_dicts(monkeypatch):
    canonical = {
        "batch_state": {
            "failed_details": [{"seed_id": "s1", "reason": "r1", "status": "failed_after_retries"}],
            "failed_seed_ids": ["s1"],
        },
        "accumulated_clean_export": {"variants": []},
    }
    saved = {}
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: canonical)
    monkeypatch.setattr(br, "save_local_canonical_resume_package", lambda pkg: saved.setdefault("pkg", pkg))
    br._persist_batch_state_into_canonical(
        {
            "failed_details": [
                {"seed_id": "s1", "reason": "r1", "status": "failed_after_retries"},
                {"seed_id": "s2", "reason": "r2", "status": "failed_after_retries"},
            ],
            "failed_seed_ids": ["s1", "s2"],
        },
        market="IL",
    )
    details = (saved["pkg"]["batch_state"].get("failed_details") or [])
    assert len(details) == 2
    assert {d.get("seed_id") for d in details} == {"s1", "s2"}


def test_get_batch_progress_does_not_reset_seed_accounting_attempts(monkeypatch):
    ordered = [_seed("s1")]
    canonical = {
        "batch_state": {"processed_seed_ids": [], "next_seed_id": "s1"},
        "accumulated_clean_export": {"variants": [{"variant_id": "v1"}]},
    }
    local = {"processed_seed_ids": [], "seed_accounting": {"s1": {"attempts": 3}}}
    captured = {}
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: canonical)
    monkeypatch.setattr(br, "load_batch_state", lambda market="IL": local)
    monkeypatch.setattr(br, "_load_outputs", lambda: _minimal_outputs())
    monkeypatch.setattr(br, "_save_state", lambda s: (_ for _ in ()).throw(AssertionError("must not save in get_batch_progress")))
    monkeypatch.setattr(br, "_refresh_coverage", lambda state, ordered_seeds: captured.setdefault("state", state) or state.__setitem__("coverage_by_make", {}))

    out = br.get_batch_progress(market="IL")
    assert isinstance(out, dict)
    assert captured["state"]["seed_accounting"]["s1"]["attempts"] == 3


def test_evaluate_continue_guard_real_canonical_no_crash(monkeypatch):
    canonical_path = Path("data/canonical/resume_package_canonical.json")
    if not canonical_path.exists():
        return
    package = json.loads(canonical_path.read_text())
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: package)
    monkeypatch.setattr(br, "fetch_file_from_github", lambda *a, **k: {})
    monkeypatch.setattr(br, "_save_state", lambda s: None)

    out = br.evaluate_continue_guard(market="IL")
    assert isinstance(out, dict)
    assert "repair_required" in out


def test_guard_reports_false_processed_seeds_on_real_canonical(monkeypatch):
    canonical_path = Path("data/canonical/resume_package_canonical.json")
    if not canonical_path.exists():
        return
    package = json.loads(canonical_path.read_text())
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: package)
    monkeypatch.setattr(br, "fetch_file_from_github", lambda *a, **k: {})
    monkeypatch.setattr(br, "_save_state", lambda s: None)

    out = br.evaluate_continue_guard(market="IL")
    assert out["repair_required"] is True
    assert int(out["false_processed_seed_count"]) > 0
    assert out["passed"] is False
    assert any("false_processed_zero_variant_seeds_found" in issue for issue in (out.get("issues") or []))


def test_variants_added_to_canonical_uses_actual_canonical_delta(monkeypatch):
    seed = _seed("s1")
    state = {"market": "IL", "processed_seed_ids": ["s1"], "failed_seed_ids": [], "failed_details": [], "seed_accounting": {}}
    before = {"accumulated_clean_export": {"variants": [{"variant_id": "v1"}]}, "batch_state": {}}
    after = {"accumulated_clean_export": {"variants": [{"variant_id": "v1"}, {"variant_id": "v2"}, {"variant_id": "v3"}]}, "batch_state": {}}
    calls = {"n": 0}

    def _load_local():
        calls["n"] += 1
        return before if calls["n"] == 1 else after

    monkeypatch.setattr(br, "load_local_canonical_resume_package", _load_local)
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_refresh_coverage", lambda s, o: None)
    monkeypatch.setattr(br, "build_final_export", lambda: {"variants": [{"variant_id": "v1"}, {"variant_id": "v2"}, {"variant_id": "v3"}]})
    monkeypatch.setattr(br, "persist_canonical_after_seed", lambda **k: {"ok": True})
    monkeypatch.setattr(
        br,
        "process_seed_with_variant_retry",
        lambda *a, **k: {
            "status": "completed",
            "variants_created": 2,
            "trace": {"discovery_parsed_json_debug": {"candidate_variants": []}},
            "accounting": {
                "seed_id": "s1",
                "attempts": 1,
                "valid_variants_built": 2,
                "variants_added_to_canonical": 99,
                "variants_deduped_or_merged": 0,
                "dedupe_proof": [],
                "no_variants_reason": None,
                "marked_processed": True,
                "status": "processed_added",
            },
        },
    )

    results, _, _ = br._process_seeds([seed], state, [seed], limit=1, market="IL")
    acc = results[0]["result"]["accounting"]
    assert acc["variants_added_to_canonical"] == 2


def test_dedupe_proof_recorded_when_variant_matches_existing(monkeypatch):
    seed = _seed("s1", make="Honda", model="Civic", ys=2017, ye=2026)
    state = {"market": "IL", "processed_seed_ids": ["s1"], "failed_seed_ids": [], "failed_details": [], "seed_accounting": {}}
    matched_vid = generate_variant_id("Honda", "Civic", 2017, 2026, "IL", "", "2.0", "AT", "sedan", "petrol")
    canonical_pkg = {"accumulated_clean_export": {"variants": [{"variant_id": matched_vid}]}, "batch_state": {}}

    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: canonical_pkg)
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_refresh_coverage", lambda s, o: None)
    monkeypatch.setattr(br, "build_final_export", lambda: {"variants": [{"variant_id": matched_vid}]})
    monkeypatch.setattr(br, "persist_canonical_after_seed", lambda **k: {"ok": True})
    monkeypatch.setattr(
        br,
        "process_seed_with_variant_retry",
        lambda *a, **k: {
            "status": "completed",
            "variants_created": 1,
            "trace": {
                "discovery_parsed_json_debug": {
                    "candidate_variants": [
                        {
                            "year_start": {"value": 2017},
                            "year_end": {"value": 2026},
                            "generation": {"value": ""},
                            "engine": {"value": "2.0"},
                            "transmission": {"value": "AT"},
                            "body_type": {"value": "sedan"},
                            "fuel_type": {"value": "petrol"},
                        }
                    ]
                }
            },
            "accounting": {
                "seed_id": "s1",
                "attempts": 1,
                "valid_variants_built": 1,
                "variants_added_to_canonical": 1,
                "variants_deduped_or_merged": 0,
                "dedupe_proof": [],
                "no_variants_reason": None,
                "marked_processed": True,
                "status": "processed_added",
            },
        },
    )

    br._process_seeds([seed], state, [seed], limit=1, market="IL")
    acc = state["seed_accounting"]["s1"]
    assert int(acc["variants_added_to_canonical"]) == 0
    assert int(acc["variants_deduped_or_merged"]) > 0
    assert state["dedupe_proof_by_seed"]["s1"]["matched_variant_ids"] == [matched_vid]


def test_repair_logic_shared_between_button_and_next_batch(monkeypatch):
    seed = _seed("s1")
    calls = {"n": 0}
    original = br._apply_false_processed_repair_to_batch_state

    def _wrapped(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(br, "_apply_false_processed_repair_to_batch_state", _wrapped)
    _ = br.repair_false_processed_seeds(
        {"batch_state": {"processed_seed_ids": ["s1"]}, "accumulated_clean_export": {"variants": []}},
        ordered_seeds=[seed],
        market="IL",
    )

    monkeypatch.setattr(
        br,
        "evaluate_continue_guard",
        lambda market="IL": {
            "passed": True,
            "issues": [],
            "coverage_audit": {"holes_count": 0},
            "repair_required": True,
            "false_processed_seed_count": 1,
            "false_processed_seeds": [{"seed_id": "s1"}],
        },
    )
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": [seed])
    monkeypatch.setattr(br, "load_batch_state", lambda market="IL": {"market": "IL", "processed_seed_ids": ["s1"], "failed_seed_ids": [], "failed_details": [], "seed_accounting": {}})
    monkeypatch.setattr(br, "_load_outputs", lambda: _minimal_outputs())
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_refresh_coverage", lambda s, o: None)
    monkeypatch.setattr(br, "save_json", lambda *a, **k: None)
    monkeypatch.setattr(br, "run_single_model", lambda *a, **k: {"status": "completed", "variants_created": 0, "verified_count": 0, "partial_count": 0, "trace": {"candidate_variants_count": 0, "discovery_parsed_json_debug": {}}})
    monkeypatch.setattr(br, "persist_canonical_resume_package", lambda **k: {"ok": True})
    monkeypatch.setattr(br, "_persist_batch_state_into_canonical", lambda *a, **k: None)
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: {"accumulated_clean_export": {"variants": []}, "batch_state": {}})
    br.run_next_batch(limit=1, market="IL")

    assert calls["n"] >= 2

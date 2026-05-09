import agent.batch_runner as br


def _seed(seed_id, make="Honda", model="Civic", ys=2017, ye=2026, market="IL"):
    return {"seed_id": seed_id, "make": make, "model": model, "year_start": ys, "year_end": ye, "market": market}


def test_find_processed_zero_variant_seeds_generic(monkeypatch):
    pkg={"batch_state":{"processed_seed_ids":["s1"]},"accumulated_clean_export":{"variants":[]}}
    monkeypatch.setattr(br,"get_ordered_seed_list",lambda market="IL":[_seed("s1")])
    out=br.find_processed_zero_variant_seeds(pkg)
    assert [r["seed_id"] for r in out]==["s1"]


def test_find_processed_zero_variant_seeds_does_not_flag_seed_with_variant(monkeypatch):
    pkg={"batch_state":{"processed_seed_ids":["s1"]},"accumulated_clean_export":{"variants":[{"seed_id":"s1","make":"Honda","model":"Civic","market":"IL","year_start":2018,"year_end":2020}]}}
    monkeypatch.setattr(br,"get_ordered_seed_list",lambda market="IL":[_seed("s1")])
    assert br.find_processed_zero_variant_seeds(pkg)==[]


def test_find_processed_zero_variant_seeds_does_not_flag_deduped_seed(monkeypatch):
    pkg={"batch_state":{"processed_seed_ids":["s1"],"dedupe_proof_by_seed":{"s1":{"matched_variant_ids":["v1"]}}},"accumulated_clean_export":{"variants":[]}}
    monkeypatch.setattr(br,"get_ordered_seed_list",lambda market="IL":[_seed("s1")])
    assert br.find_processed_zero_variant_seeds(pkg)==[]


def test_find_processed_zero_variant_seeds_does_not_flag_no_variants_reason(monkeypatch):
    pkg={"batch_state":{"processed_seed_ids":["s1"],"no_variants_by_seed":{"s1":{"reason":"no_reliable_sources_found"}}},"accumulated_clean_export":{"variants":[]}}
    monkeypatch.setattr(br,"get_ordered_seed_list",lambda market="IL":[_seed("s1")])
    assert br.find_processed_zero_variant_seeds(pkg)==[]


def test_seed_not_marked_processed_when_zero_variants_no_reason(monkeypatch):
    state={"processed_seed_ids":[],"failed_seed_ids":[]}
    monkeypatch.setattr(br,"run_single_model",lambda *a,**k:{"status":"completed","variants_created":0,"verified_count":0,"partial_count":0,"trace":{"candidate_variants_count":0,"discovery_parsed_json_debug":{}}})
    r=br.process_seed_with_variant_retry(_seed("s1"),state=state,max_attempts=2)
    assert "s1" not in state["processed_seed_ids"]
    assert "s1" in state["needs_retry_seed_ids"]
    assert r["status"]=="failed_after_retries"


def test_seed_marked_processed_when_variant_added(monkeypatch):
    state={"processed_seed_ids":[],"failed_seed_ids":[]}
    monkeypatch.setattr(br,"run_single_model",lambda *a,**k:{"status":"completed","variants_created":1,"verified_count":1,"partial_count":0,"trace":{"candidate_variants_count":1,"discovery_parsed_json_debug":{}}})
    br.process_seed_with_variant_retry(_seed("s2"),state=state,max_attempts=1)
    assert "s2" in state["processed_seed_ids"]


def test_retry_loop_stops_at_max_attempts(monkeypatch):
    state={"processed_seed_ids":[],"failed_seed_ids":[]}
    calls={"n":0}
    def _run(*a,**k):
        calls["n"]+=1
        return {"status":"completed","variants_created":0,"verified_count":0,"partial_count":0,"trace":{"candidate_variants_count":0,"discovery_parsed_json_debug":{}}}
    monkeypatch.setattr(br,"run_single_model",_run)
    br.process_seed_with_variant_retry(_seed("s3"),state=state,max_attempts=9)
    assert calls["n"]==5


def test_repair_zero_variant_processed_seeds_detects_honda_hyundai(monkeypatch):
    seeds=[_seed("honda__civic__2017__2026__il",model="Civic"),_seed("hyundai__kona__2017__2026__il",make="Hyundai",model="Kona")]
    pkg={"batch_state":{"processed_seed_ids":[s["seed_id"] for s in seeds]},"accumulated_clean_export":{"variants":[]}}
    out=br.find_processed_zero_variant_seeds(pkg,ordered_seeds=seeds)
    assert len(out)==2

def test_normalize_does_not_require_seed_accounting():
    ordered=[{"seed_id":"s1","make":"A","model":"M","year_start":2010,"year_end":2012,"market":"IL"},{"seed_id":"s2","make":"A","model":"N","year_start":2013,"year_end":2015,"market":"IL"}]
    out=br.normalize_batch_state_for_resume({"processed_seed_ids":["s1"]},ordered,market="IL")
    assert out["processed_seed_ids"]==["s1"]


def test_zero_variant_guard_only_applies_on_new_seed_completion(monkeypatch):
    ordered=[{"seed_id":"s1","make":"A","model":"M","year_start":2010,"year_end":2012,"market":"IL"}]
    out=br.normalize_batch_state_for_resume({"processed_seed_ids":["s1"]},ordered,market="IL")
    assert out["processed_seed_ids"]==["s1"]
    state={"processed_seed_ids":[],"failed_seed_ids":[]}
    monkeypatch.setattr(br,"run_single_model",lambda *a,**k:{"variants_created":0,"verified_count":0,"partial_count":0,"trace":{"candidate_variants_count":0}})
    res=br.process_seed_with_variant_retry(dict(ordered[0]),state=state,max_attempts=1)
    assert res["status"]=="failed_after_retries"
    assert "s1" not in state["processed_seed_ids"]


def test_strict_audit_reports_but_does_not_mutate_by_default():
    ordered=[{"seed_id":"s1","make":"A","model":"M","year_start":2010,"year_end":2012,"market":"IL"}]
    base={"processed_seed_ids":["s1"]}
    relaxed=br.normalize_batch_state_for_resume(base,ordered,variants=[],market="IL")
    strict=br.normalize_batch_state_for_resume(base,ordered,variants=[],market="IL",strict_zero_variant_audit=True)
    assert relaxed["processed_seed_ids"]==["s1"]
    assert strict["processed_seed_ids"]==["s1"]
    assert strict["false_processed_seed_ids"]


# ---------------------------------------------------------------------------
# Regression: Honda 16/16 processed with 0 variants must be flagged
# ---------------------------------------------------------------------------

def _make_honda_seeds(n=16):
    """Create n Honda seeds with distinct seed_ids to simulate 16/16 processed."""
    models = [
        "Accord", "Civic", "Civic Type R", "CR-V", "CR-Z", "Fit", "HR-V",
        "Insight", "Jazz", "Legend", "NSX", "Odyssey", "Passport", "Pilot",
        "Ridgeline", "Stream",
    ]
    seeds = []
    for i, m in enumerate(models[:n]):
        sid = f"honda__{m.lower().replace(' ', '_').replace('-', '_')}__2017__2026__il"
        seeds.append({"seed_id": sid, "make": "Honda", "model": m, "year_start": 2017, "year_end": 2026, "market": "IL"})
    return seeds


def test_honda_16_of_16_processed_zero_variants_flagged_as_false_processed():
    """Regression: Honda processed 16/16 with 0 variants must be flagged as false_processed_zero_variant_seed."""
    honda_seeds = _make_honda_seeds(16)
    pkg = {
        "batch_state": {"processed_seed_ids": [s["seed_id"] for s in honda_seeds]},
        "accumulated_clean_export": {"variants": []},
    }
    flagged = br.find_processed_zero_variant_seeds(pkg, ordered_seeds=honda_seeds)
    assert len(flagged) == 16, f"Expected 16 false-processed seeds, got {len(flagged)}: {[r['seed_id'] for r in flagged]}"
    assert all(r["matched_variants_count"] == 0 for r in flagged)
    assert all(r["make"] == "Honda" for r in flagged)
    assert all(r["repair_status"] == "needs_retry" for r in flagged)


def test_evaluate_continue_guard_blocked_by_false_processed_seeds(monkeypatch, tmp_path):
    """evaluate_continue_guard must report repair_required=True when false-processed seeds exist.

    When the canonical has ZERO total variants (canonical_variants_count == 0) the guard also
    fails for 'variants_found == 0', so passed=False is guaranteed even without the zero-variant
    issue being promoted to the issues list (which only happens when variants carry make fields).
    The key contract is that repair_required=True and false_processed_seed_count=N are always
    present so run_next_batch can block the forward scan.
    """
    honda_seeds = _make_honda_seeds(16)
    # canonical has Honda 16/16 processed, 0 variants
    fake_canonical = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "processed_seed_ids": [s["seed_id"] for s in honda_seeds],
            "last_completed_seed_id": honda_seeds[-1]["seed_id"],
            "next_seed_id": None,
        },
        "accumulated_clean_export": {"variants": []},
    }
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: fake_canonical)
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": honda_seeds)
    monkeypatch.setattr(br, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(br, "fetch_file_from_github", lambda path: None)
    monkeypatch.setattr(br, "get_github_config", lambda: {"canonical_path": "", "token": "", "repo": "", "branch": "", "backup_path": ""})
    monkeypatch.setattr(br, "_batch_state_path", lambda: tmp_path / "batch_state.json")
    monkeypatch.setattr(br, "_save_state", lambda s: None)

    guard = br.evaluate_continue_guard(market="IL")
    # repair_required must always be set when false-processed seeds exist
    assert guard["repair_required"] is True
    assert guard["false_processed_seed_count"] == 16
    assert len(guard["false_processed_seeds"]) == 16
    # passed is False — due to 'variants_found == 0' (canonical is empty) even without
    # the zero-variant issue being in issues (no make-field variants to trigger blocking promotion)
    assert guard["passed"] is False


def test_run_next_batch_blocked_by_repair_required(monkeypatch, tmp_path):
    """run_next_batch must return status='blocked' when evaluate_continue_guard reports repair_required."""
    honda_seeds = _make_honda_seeds(4)
    fake_guard = {
        "passed": True,  # deliberately set passed=True to isolate the repair_required check
        "issues": [],
        "coverage_audit": {"holes_count": 0},
        "repair_required": True,
        "false_processed_seed_count": 4,
        "false_processed_seeds": [{"seed_id": s["seed_id"]} for s in honda_seeds],
    }
    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": fake_guard)
    result = br.run_next_batch(limit=1, market="IL")
    assert result["status"] == "blocked"
    assert result.get("repair_required") is True
    assert result.get("false_processed_seed_count") == 4


def test_repair_false_processed_seeds_removes_from_processed():
    """repair_false_processed_seeds must move seeds from processed_seed_ids to needs_retry_seed_ids."""
    seeds = [
        _seed("honda__accord__1990__2024__il", model="Accord"),
        _seed("honda__civic__1990__2026__il", model="Civic"),
        _seed("honda__cr_v__1997__2026__il", model="CR-V"),
    ]
    pkg = {
        "batch_state": {"processed_seed_ids": [s["seed_id"] for s in seeds]},
        "accumulated_clean_export": {"variants": []},
    }
    result = br.repair_false_processed_seeds(pkg, ordered_seeds=seeds)
    assert result["ok"] is True
    assert result["repaired_count"] == 3
    repaired_bs = result["package"]["batch_state"]
    assert repaired_bs["processed_seed_ids"] == []
    for sid in [s["seed_id"] for s in seeds]:
        assert sid in repaired_bs["needs_retry_seed_ids"]
        assert sid in repaired_bs["false_processed_seed_ids"]


def test_repair_false_processed_seeds_skips_seeds_with_variants():
    """repair_false_processed_seeds must not remove a seed that actually has variants."""
    seeds = [
        _seed("honda__accord__1990__2024__il", model="Accord"),
        _seed("honda__civic__1990__2026__il", model="Civic"),
    ]
    pkg = {
        "batch_state": {"processed_seed_ids": [s["seed_id"] for s in seeds]},
        "accumulated_clean_export": {
            "variants": [
                # Civic has a variant — should NOT be repaired
                {"seed_id": "honda__civic__1990__2026__il", "make": "Honda", "model": "Civic", "market": "IL", "year_start": 2020, "year_end": 2026}
            ]
        },
    }
    result = br.repair_false_processed_seeds(pkg, ordered_seeds=seeds)
    assert result["ok"] is True
    assert result["repaired_count"] == 1  # only Accord
    repaired_bs = result["package"]["batch_state"]
    # Accord removed; Civic kept
    assert "honda__accord__1990__2024__il" not in repaired_bs["processed_seed_ids"]
    assert "honda__civic__1990__2026__il" in repaired_bs["processed_seed_ids"]


def test_repair_false_processed_seeds_no_false_processed():
    """repair_false_processed_seeds returns ok with repaired_count=0 when all seeds are genuinely processed."""
    seeds = [_seed("honda__civic__1990__2026__il", model="Civic")]
    pkg = {
        "batch_state": {"processed_seed_ids": ["honda__civic__1990__2026__il"]},
        "accumulated_clean_export": {
            "variants": [
                {"seed_id": "honda__civic__1990__2026__il", "make": "Honda", "model": "Civic", "market": "IL", "year_start": 2020, "year_end": 2026}
            ]
        },
    }
    result = br.repair_false_processed_seeds(pkg, ordered_seeds=seeds)
    assert result["ok"] is True
    assert result["repaired_count"] == 0
    assert result["package"]["batch_state"]["processed_seed_ids"] == ["honda__civic__1990__2026__il"]


def test_evaluate_continue_guard_issues_include_zero_variant_msg_when_variants_have_make(monkeypatch, tmp_path):
    """evaluate_continue_guard must add false_processed message to issues when canonical has make-field variants.

    Hyundai is processed but has 0 matching variants; another make (Toyota) has variants.
    Since the canonical carries variants WITH make fields, the audit is promoted to a blocking issue.
    """
    hyundai_seed = _seed("hyundai__kona__2017__2026__il", make="Hyundai", model="Kona")
    toyota_seed = _seed("toyota__corolla__2017__2026__il", make="Toyota", model="Corolla")
    seeds = [hyundai_seed, toyota_seed]
    # Toyota variant exists, Hyundai has 0 variants → Hyundai should be false-processed
    fake_canonical = {
        "schema_version": "resume_package_v1",
        "batch_state": {
            "processed_seed_ids": [hyundai_seed["seed_id"], toyota_seed["seed_id"]],
            "last_completed_seed_id": toyota_seed["seed_id"],
            "next_seed_id": None,
        },
        "accumulated_clean_export": {
            "variants": [
                {"seed_id": toyota_seed["seed_id"], "make": "Toyota", "model": "Corolla", "market": "IL", "year_start": 2017, "year_end": 2026, "variant_id": "toyota-corolla-v1"},
            ]
        },
    }
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: fake_canonical)
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": seeds)
    monkeypatch.setattr(br, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(br, "fetch_file_from_github", lambda path: None)
    monkeypatch.setattr(br, "get_github_config", lambda: {"canonical_path": "", "token": "", "repo": "", "branch": "", "backup_path": ""})
    monkeypatch.setattr(br, "_batch_state_path", lambda: tmp_path / "batch_state.json")
    monkeypatch.setattr(br, "_save_state", lambda s: None)

    guard = br.evaluate_continue_guard(market="IL")
    # The canonical has Toyota variants with make fields → audit promoted to issues
    assert guard["repair_required"] is True
    assert guard["false_processed_seed_count"] == 1
    assert guard["false_processed_seeds"][0]["seed_id"] == hyundai_seed["seed_id"]
    assert guard["passed"] is False
    assert any("false_processed_zero_variant_seeds_found" in issue for issue in guard["issues"])


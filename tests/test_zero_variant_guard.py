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

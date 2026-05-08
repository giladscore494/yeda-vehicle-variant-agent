from core.final_export_builder import build_clean_final_export, rebuild_variant_status, repair_field_source_ids, evaluate_final_export_quality, assert_no_mock_in_final_export
from agent.runner import _field_to_verified


def _field(v=None, st="partial", sc=1, ids=None):
    return {"value": v, "status": st, "sources_count": sc, "source_ids": ids or (["src_1"] if sc else []), "used_in_compare": st in {"verified","partial"} and sc>0}


def _variant(trim="Base", status="partial"):
    return {"variant_id": f"v-{trim}", "make":"Aiways","model":"U5","market":"IL","year_start":2020,"year_end":2023,
    "generation":_field("Gen1","partial",1),"body_type":_field("SUV","verified",2,["src_1","src_2"]),"seats":_field(5,"partial",1),"engine":_field("EV","verified",2,["src_1","src_3"]),"transmission":_field("AT","partial",1),"fuel_type":_field("Electric","partial",1),"drivetrain":_field("FWD","partial",1),"trim":_field(trim,status,1)}


def test_mock_removed_and_no_mock_in_export():
    bad = _variant(); bad["source_ids"]=["source_mock_kia_sportage"]; bad["notes"]= ["mock mode"]
    good = _variant("Premium")
    out = build_clean_final_export([good], [bad], sources=[{"source_id": "source_mock_kia_sportage", "source_type": "mock"}, {"source_id": "src_real"}])
    assert out["counts"]["mock_removed"] == 1
    assert out["counts"]["mock_sources_removed"] == 1
    assert all("source_mock_" not in str(v) for v in out["variants"])
    assert all("source_mock_" not in str(s) for s in out["sources"])


def test_rebuild_status_verified_and_partial_and_compare_flag():
    v = _variant(); st, cf = rebuild_variant_status(v)
    assert (st, cf) == ("verified", "high")
    weak = _variant(); weak["engine"]["sources_count"] = 0; weak["engine"]["status"] = "unverified"; weak["engine"]["used_in_compare"] = True
    st2, _ = rebuild_variant_status(weak)
    assert weak["engine"]["used_in_compare"] is False
    assert st2 in {"partial", "unresolved"}


def test_repair_field_source_ids_from_field_sources_and_runner_preserves():
    v = _variant(); v["engine"] = {"value":"EV","sources_count":2,"source_ids":[]}; v["field_sources"]={"engine":["src_1","src_2"]}
    fixed = repair_field_source_ids(v)
    assert fixed["engine"]["source_ids"] == ["src_1", "src_2"]
    fd = _field_to_verified({"value":"EV","sources_count":2}, {"field_sources":{"engine":["src_1","src_2"]}}, "engine")
    assert fd["source_ids"] == ["src_1", "src_2"]


def test_trim_merge_and_verified_wins_and_counts_match():
    verified = _variant("Standard", "verified")
    partial = _variant("Premium", "partial")
    partial["variant_id"] = verified["variant_id"]
    out = build_clean_final_export([verified], [partial])
    assert out["counts"]["total_variants"] == len(out["variants"])
    assert len(out["variants"]) == 1
    vals = {t["value"] for t in out["variants"][0].get("trim_options", [])}
    assert {"Standard", "Premium"}.issubset(vals)
    assert out["variants"][0]["verification_status"] == "verified"


def test_quality_grade_fail_when_mock_present():
    out = {"variants":[{"make":"A","model":"B","year_start":2020,"year_end":2021,"notes":["mock mode"]}], "counts":{"total_variants":1,"variants_with_empty_source_ids":1,"unresolved":0}, "audit":{"source_id_coverage_ratio":0.1,"verified_ratio":0.0,"trim_merge_enabled":True}}
    q = evaluate_final_export_quality(out)
    assert q["grade"] == "FAIL"


def test_quality_grades_and_assert_no_mock():
    clean = build_clean_final_export([_variant()], [])
    assert clean["quality_gate"]["grade"] in {"A", "B", "C", "D"}
    assert_no_mock_in_final_export(clean)


def test_zero_variants_is_fail():
    out = build_clean_final_export([], [])
    assert out["quality_gate"]["grade"] == "FAIL"
    assert "No variants in final export." in out["quality_gate"]["blocking_issues"]


def test_mock_removed_is_warning_not_blocking():
    bad = _variant(); bad["notes"] = ["mock mode"]
    out = build_clean_final_export([_variant("X", "verified")], [bad], sources=[{"source_id": "source_mock_1", "source_type": "mock"}])
    assert "Mock contaminated records were found and removed." in out["quality_gate"]["warnings"]
    assert "Mock contamination remains after cleanup." not in out["quality_gate"]["blocking_issues"]
    assert out["quality_gate"]["passed"] is True


def test_blocking_issue_always_fails():
    out = build_clean_final_export([], [])
    assert out["quality_gate"]["blocking_issues"]
    assert out["quality_gate"]["passed"] is False
    assert out["quality_gate"]["grade"] == "FAIL"

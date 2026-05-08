from __future__ import annotations

from datetime import datetime, timezone
import copy
import json
from typing import Any

FIELD_NAMES = ["body_type", "seats", "engine", "transmission", "fuel_type", "drivetrain", "generation", "year_start", "year_end", "trim"]
CRITICAL_FIELDS = ["body_type", "seats", "engine", "transmission", "fuel_type", "drivetrain"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_field_obj(v: Any) -> dict:
    return v if isinstance(v, dict) else {"value": v}


def contains_mock_marker(obj: Any) -> bool:
    text = json.dumps(obj, ensure_ascii=False).lower()
    return any(m in text for m in ["source_mock_", "mock mode", "reason\": \"mock\"", "1.6 turbo", "source_mock_kia_sportage"])


def is_mock_contaminated_variant(variant: dict) -> bool:
    if contains_mock_marker(variant):
        return True
    notes = " ".join(variant.get("notes", []) if isinstance(variant.get("notes"), list) else [str(variant.get("notes", ""))]).lower()
    if "mock" in notes:
        return True
    if str(variant.get("reason", "")).strip().lower() == "mock":
        return True
    gen = _as_field_obj(variant.get("generation", {})).get("value")
    model = str(variant.get("model", "")).lower()
    if str(gen or "").upper() == "QL" and "sportage" in json.dumps(variant, ensure_ascii=False).lower() and "sportage" not in model:
        return True
    return False


def repair_field_source_ids(variant: dict) -> dict:
    v = copy.deepcopy(variant)
    field_sources = v.get("field_sources") if isinstance(v.get("field_sources"), dict) else {}
    cand_fs = ((v.get("candidate_raw") or {}).get("field_sources") if isinstance(v.get("candidate_raw"), dict) else {}) or {}
    top_sources = [s for s in (v.get("source_ids") or v.get("source_urls") or []) if isinstance(s, str) and not s.startswith("source_mock_")]

    for name in FIELD_NAMES:
        if name not in v:
            continue
        f = _as_field_obj(v.get(name))
        ids = f.get("source_ids") or f.get("source_urls") or []
        ids = ids if isinstance(ids, list) else []
        ids = [s for s in ids if isinstance(s, str) and not s.startswith("source_mock_")]
        if not ids:
            for bucket in [field_sources.get(name, []), cand_fs.get(name, []), top_sources]:
                if isinstance(bucket, list):
                    ids = [s for s in bucket if isinstance(s, str) and not s.startswith("source_mock_")]
                    if ids:
                        break
        if ids:
            f["source_ids"] = list(dict.fromkeys(ids))
        sc = int(f.get("sources_count", 0) or 0)
        if sc == 0 and f.get("source_ids"):
            f["sources_count"] = len(f["source_ids"])
        v[name] = f
    return v


def rebuild_variant_status(variant: dict) -> tuple[str, str]:
    v = variant
    for n in FIELD_NAMES:
        if n in v:
            f = _as_field_obj(v[n])
            used = str(f.get("status", "unknown")) in {"verified", "partial"} and int(f.get("sources_count", 0) or 0) >= 1
            f["used_in_compare"] = used
            v[n] = f
    if not v.get("make") or not v.get("model") or not v.get("year_start") or not v.get("year_end"):
        return "unresolved", "low"
    if any(str(_as_field_obj(v.get(f, {})).get("status", "")).lower() == "conflict" for f in CRITICAL_FIELDS):
        return "conflict", "low"

    crit = [_as_field_obj(v.get(f, {})) for f in CRITICAL_FIELDS]
    c1 = sum(1 for f in crit if int(f.get("sources_count", 0) or 0) >= 1)
    c2 = sum(1 for f in crit if int(f.get("sources_count", 0) or 0) >= 2)
    nonempty = sum(1 for f in crit if f.get("value") not in (None, ""))
    identity = any(_as_field_obj(v.get(f, {})).get("value") not in (None, "") for f in ["engine", "transmission", "fuel_type", "body_type", "generation"])
    no_bad_compare = all((not _as_field_obj(v.get(f, {})).get("used_in_compare")) or int(_as_field_obj(v.get(f, {})).get("sources_count", 0) or 0) >= 1 for f in FIELD_NAMES if f in v)

    bt = str(_as_field_obj(v.get("body_type", {})).get("status", "")) in {"verified", "partial"}
    st = str(_as_field_obj(v.get("seats", {})).get("status", "")) in {"verified", "partial"}
    ft = str(_as_field_obj(v.get("fuel_type", {})).get("status", "")) in {"verified", "partial"}
    eng_verified = str(_as_field_obj(v.get("engine", {})).get("status", "")) == "verified" or str(_as_field_obj(v.get("transmission", {})).get("status", "")) == "verified"

    if bt and st and ft and eng_verified and c1 >= 4 and c2 >= 2 and no_bad_compare:
        return "verified", "high"
    if identity and nonempty >= 2 and c1 >= 1:
        return "partial", "medium"
    return "unresolved", "low"


def evaluate_final_export_quality(final_export: dict) -> dict:
    blocking, warnings = [], []
    score = 100
    variants = final_export.get("variants", [])
    if len(variants) == 0:
        blocking.append("No variants in final export.")
    if any(is_mock_contaminated_variant(v) for v in variants):
        blocking.append("Mock markers remain in final export.")
    for v in variants:
        for fn in FIELD_NAMES:
            if fn in v:
                f = _as_field_obj(v[fn])
                if f.get("used_in_compare") and int(f.get("sources_count", 0) or 0) == 0:
                    blocking.append("used_in_compare=true field has sources_count=0")
                    break
        if not v.get("make") or not v.get("model") or not v.get("year_start") or not v.get("year_end"):
            blocking.append("variant missing make/model/year_start/year_end")
            break
    if int(final_export.get("counts", {}).get("total_variants", -1)) != len(variants):
        blocking.append("malformed counts not matching variants")
    audit = final_export.get("audit", {})
    if audit.get("source_id_coverage_ratio", 0) < 0.5:
        score -= 15; warnings.append("Low source ID coverage ratio.")
    if audit.get("verified_ratio", 0) < 0.4:
        score -= 10; warnings.append("Low verified ratio.")
    if final_export.get("counts", {}).get("variants_with_empty_source_ids", 0) > max(1, len(variants) * 0.2):
        score -= 10; warnings.append("Too many variants without source_ids.")
    if not audit.get("trim_merge_enabled", True):
        score -= 5; warnings.append("Trim merge disabled.")
    if final_export.get("counts", {}).get("unresolved", 0) > 0:
        score -= 5; warnings.append("Unresolved included in final.")
    if blocking or score < 45:
        grade = "FAIL"
    elif score >= 90:
        grade = "A"
    elif score >= 75:
        grade = "B"
    elif score >= 60:
        grade = "C"
    else:
        grade = "D"
    return {"passed": grade != "FAIL" and not blocking, "score": max(score, 0), "grade": grade, "blocking_issues": list(dict.fromkeys(blocking)), "warnings": warnings}


def assert_no_mock_in_final_export(final_export: dict):
    payload_text = json.dumps(final_export, ensure_ascii=False).lower()
    if "source_mock_" in payload_text:
        raise ValueError("Mock contamination still exists in final export")
    if any(is_mock_contaminated_variant(v) for v in final_export.get("variants", [])):
        raise ValueError("Mock contamination still exists in final export")


def _clean_sources(sources: list[dict] | None) -> tuple[list[dict], int]:
    cleaned = []
    removed = 0
    for src in sources or []:
        if not isinstance(src, dict):
            continue
        text = json.dumps(src, ensure_ascii=False).lower()
        if "source_mock_" in text or src.get("source_type") == "mock" or str(src.get("reason", "")).strip().lower() == "mock" or "mock mode" in text:
            removed += 1
            continue
        cleaned.append(src)
    return cleaned, removed


def _tkey(v: dict) -> str:
    def val(n):
        return str(_as_field_obj(v.get(n, {})).get("value", v.get(n, "")) or "").strip().lower()
    tokens = [v.get("make", ""), v.get("model", ""), v.get("market", ""), val("year_start"), val("year_end"), val("generation"), val("body_type"), val("seats"), val("engine"), val("transmission"), val("fuel_type"), val("drivetrain")]
    return "|".join(str(t).strip().lower() for t in tokens)


def build_clean_final_export(verified_variants, partial_variants, sources=None, conflicts=None, unresolved=None, include_partial=True, include_verified=True, include_conflicts=False, include_unresolved=False, merge_trim_options=True, strict_no_mock=True) -> dict:
    items = []
    if include_partial: items.extend(copy.deepcopy(partial_variants or []))
    if include_verified: items.extend(copy.deepcopy(verified_variants or []))
    mock_removed = 0
    filtered = []
    for v in items:
        if strict_no_mock and is_mock_contaminated_variant(v):
            mock_removed += 1
            continue
        filtered.append(repair_field_source_ids(v))
    by_key = {}
    trim_merged = 0
    rank = {"verified": 3, "partial": 2, "unresolved": 1, "conflict": 0}
    for v in filtered:
        st, conf = rebuild_variant_status(v)
        v["verification_status"] = st; v["confidence"] = conf
        key = _tkey(v) if merge_trim_options else (v.get("variant_id") or _tkey(v))
        if key not in by_key:
            by_key[key] = v; by_key[key]["trim_options"] = []
        else:
            trim_merged += 1
            if rank.get(st, 0) > rank.get(by_key[key].get("verification_status", "unresolved"), 0):
                base = by_key[key]; by_key[key] = v; by_key[key]["trim_options"] = base.get("trim_options", [])
        trim = _as_field_obj(v.get("trim", {}))
        if trim.get("value") not in (None, ""):
            by_key[key].setdefault("trim_options", []).append({"value": trim.get("value"), "source_ids": trim.get("source_ids", []), "status": trim.get("status", "unknown"), "sources_count": int(trim.get("sources_count", 0) or 0)})
    variants = list(by_key.values())
    for v in variants:
        opts = v.get("trim_options", [])
        v["trim_options"] = [dict(t) for t in {json.dumps(o, sort_keys=True): o for o in opts}.values()]

    counts = {"verified": sum(1 for v in variants if v.get("verification_status") == "verified"), "partial": sum(1 for v in variants if v.get("verification_status") == "partial"), "conflict": sum(1 for v in variants if v.get("verification_status") == "conflict"), "unresolved": sum(1 for v in variants if v.get("verification_status") == "unresolved"), "total_variants": len(variants), "mock_removed": mock_removed, "duplicates_removed": max(0, len(filtered)-len(variants)), "trim_merged": trim_merged, "variants_with_empty_source_ids": 0, "variants_with_no_sources": 0}
    fields_checked=gt0=with_ids=0
    for v in variants:
        has_any=False; has_ids=False
        for n in FIELD_NAMES:
            if n in v:
                f = _as_field_obj(v[n]); fields_checked += 1
                if int(f.get("sources_count", 0) or 0) > 0:
                    gt0 += 1; has_any=True
                    if f.get("source_ids"):
                        with_ids += 1; has_ids=True
        if has_any and not has_ids:
            counts["variants_with_empty_source_ids"] += 1
        if not has_any:
            counts["variants_with_no_sources"] += 1
    total = max(1, len(variants))
    audit = {"mock_contamination_found": mock_removed > 0, "source_id_coverage_ratio": with_ids / max(gt0, 1), "verified_ratio": counts["verified"] / total, "partial_ratio": counts["partial"] / total, "fields_checked": fields_checked, "fields_with_sources_count_gt_0": gt0, "fields_with_source_ids": with_ids, "status_rebuilt": True, "trim_merge_enabled": merge_trim_options}
    cleaned_sources, mock_sources_removed = _clean_sources(sources)
    counts["mock_sources_removed"] = mock_sources_removed
    out = {"schema_version": "vehicle_variants_final_v2", "created_at": _now(), "counts": counts, "variants": variants, "sources": cleaned_sources, "conflicts": conflicts if include_conflicts else [], "unresolved": unresolved if include_unresolved else [], "audit": audit}
    out["quality_gate"] = evaluate_final_export_quality(out)
    if strict_no_mock and mock_removed > 0:
        out["quality_gate"].setdefault("blocking_issues", []).append("Mock contaminated variants were removed from final export.")
    assert_no_mock_in_final_export(out)
    return out

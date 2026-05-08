from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Callable

from core.ingest import load_model_seeds
from agent.runner import run_single_model
from storage.json_store import get_output_paths, load_json_list, load_json_object, save_json, project_root
from core.final_export_builder import build_clean_final_export, assert_no_mock_in_final_export

BATCH_STATE_SCHEMA = "batch_state_v1"
RETRYABLE_SCHEMA_ERROR_TOKENS = ["'market'", "missing market", "keyerror: market", "missing required seed field"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_token(value: str) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    return text.replace("/", "-").replace(" ", "_")


def build_seed_id(make: str, model: str, year_start: int, year_end: int, market: str = "IL") -> str:
    return f"{normalize_token(make)}__{normalize_token(model)}__{int(year_start)}__{int(year_end)}__{normalize_token(market)}"


def get_ordered_seed_list(market: str = "IL") -> list[dict]:
    seeds = load_model_seeds()
    ordered = sorted(seeds, key=lambda s: ((s.make or "").lower(), (s.model or "").lower(), int(s.year_start or 0), int(s.year_end or 0)))
    return [{"make": s.make, "model": s.model, "year_start": int(s.year_start or 0), "year_end": int(s.year_end or 0), "market": market, "seed_id": build_seed_id(s.make, s.model, int(s.year_start or 0), int(s.year_end or 0), market)} for s in ordered]


def seed_to_dict(seed: dict) -> dict:
    return {
        "seed_id": seed["seed_id"],
        "make": seed["make"],
        "model": seed["model"],
        "year_start": seed["year_start"],
        "year_end": seed["year_end"],
        "market": seed.get("market", "IL"),
    }


def _batch_state_path():
    return project_root() / "data/output/batch_state.json"


def _empty_coverage_by_make(ordered_seeds: list[dict]) -> dict:
    coverage = {}
    for seed in ordered_seeds:
        make = seed["make"]
        coverage.setdefault(make, {"total": 0, "processed": 0, "verified_variants": 0, "partial_variants": 0, "unresolved": 0, "failed": 0, "completed": False})
        coverage[make]["total"] += 1
    return coverage


def _default_state(market: str, ordered_seeds: list[dict]) -> dict:
    now = _now()
    return {"schema_version": BATCH_STATE_SCHEMA, "market": market, "created_at": now, "updated_at": now, "last_batch_id": None, "total_seeds": len(ordered_seeds), "processed_seed_ids": [], "failed_seed_ids": [], "skipped_seed_ids": [], "in_progress_seed_id": None, "last_completed_seed_id": None, "next_seed_id": ordered_seeds[0]["seed_id"] if ordered_seeds else None, "coverage_by_make": _empty_coverage_by_make(ordered_seeds), "run_history": [], "failed_details": []}


def load_batch_state(market: str = "IL") -> dict:
    ordered = get_ordered_seed_list(market)
    state = load_json_object(_batch_state_path())
    if not state or state.get("schema_version") != BATCH_STATE_SCHEMA or state.get("market") != market:
        state = _default_state(market, ordered)
        save_json(_batch_state_path(), state)
    return state


def _save_state(state: dict):
    state["updated_at"] = _now()
    save_json(_batch_state_path(), state)


def _load_outputs() -> dict:
    p = get_output_paths()
    return {"run_history": load_json_list(p["run_history"]), "unresolved": load_json_list(p["unresolved_models"]), "conflicts": load_json_list(p["vehicle_conflicts"]), "verified": load_json_list(p["vehicle_variants_verified"]), "partial": load_json_list(p["vehicle_variants_partial"]), "sources": load_json_list(p["vehicle_sources"])}


def is_seed_completed(seed_id: str, outputs: dict, batch_state: dict) -> bool:
    if seed_id in (batch_state.get("processed_seed_ids") or []):
        return True
    for row in outputs.get("unresolved", []):
        if row.get("seed_id") == seed_id:
            return True
    for row in outputs.get("conflicts", []):
        if row.get("seed_id") == seed_id:
            return True
    for run in outputs.get("run_history", []):
        if run.get("seed_id") != seed_id:
            continue
        if run.get("status") != "completed":
            continue
        summary = run.get("classification_summary") or {}
        if any(int(summary.get(k, run.get(k, 0)) or 0) > 0 for k in ["verified_count", "partial_count", "conflict_count", "unresolved_count"]):
            return True
        if run.get("variants_created") is not None:
            return True
    return False


def audit_coverage_until_last_completed(ordered_seeds: list[dict], batch_state: dict, outputs: dict) -> dict:
    seed_ids = [s["seed_id"] for s in ordered_seeds]
    last_completed_seed_id = batch_state.get("last_completed_seed_id")
    if last_completed_seed_id not in seed_ids:
        # fallback: furthest processed in canonical order
        completed_set = set(batch_state.get("processed_seed_ids") or [])
        idxs = [i for i, s in enumerate(seed_ids) if s in completed_set]
        last_idx = max(idxs) if idxs else -1
        last_completed_seed_id = seed_ids[last_idx] if last_idx >= 0 else None
    else:
        last_idx = seed_ids.index(last_completed_seed_id)
    if last_completed_seed_id is None:
        last_idx = -1
    missing = []
    for seed in ordered_seeds[: last_idx + 1]:
        if not is_seed_completed(seed["seed_id"], outputs, batch_state):
            missing.append(seed_to_dict(seed))
    return {"last_completed_seed_id": last_completed_seed_id, "last_completed_index": last_idx, "scanned_count": max(last_idx + 1, 0), "missing_seed_ids": [m["seed_id"] for m in missing], "missing_seeds": missing, "holes_count": len(missing), "coverage_complete_until_last_completed": len(missing) == 0}


def _refresh_coverage(state: dict, ordered_seeds: list[dict]):
    coverage = _empty_coverage_by_make(ordered_seeds)
    by_seed = {s["seed_id"]: s for s in ordered_seeds}
    for sid in state.get("processed_seed_ids", []):
        if sid in by_seed:
            coverage[by_seed[sid]["make"]]["processed"] += 1
    for sid in state.get("failed_seed_ids", []):
        if sid in by_seed:
            coverage[by_seed[sid]["make"]]["failed"] += 1
    for run in load_json_list(get_output_paths()["run_history"]):
        make = run.get("make")
        if make in coverage:
            summary = run.get("classification_summary") or {}
            coverage[make]["verified_variants"] += int(summary.get("verified_count", run.get("verified_count", 0)) or 0)
            coverage[make]["partial_variants"] += int(summary.get("partial_count", run.get("partial_count", 0)) or 0)
            coverage[make]["unresolved"] += int(summary.get("unresolved_count", run.get("unresolved_count", 0)) or 0)
    for make, c in coverage.items():
        c["completed"] = c["processed"] >= c["total"] and c["total"] > 0
    state["coverage_by_make"] = coverage


def _process_seeds(seed_queue: list[dict], state: dict, ordered: list[dict], limit: int, force_refresh=False, use_cache=True, progress_callback: Callable | None = None):
    results = []
    for idx, seed in enumerate(seed_queue[:limit], start=1):
        market = seed.get("market") or state.get("market") or "IL"
        seed["market"] = market
        sid = seed["seed_id"]
        state["in_progress_seed_id"] = sid
        _save_state(state)
        if progress_callback:
            progress_callback({"index": idx, "total": min(limit, len(seed_queue)), "seed": seed, "results": list(results)})
        try:
            result = run_single_model(seed["make"], seed["model"], seed["year_start"], seed["year_end"], market=seed["market"], use_cache=use_cache, force_refresh=force_refresh)
        except Exception as exc:
            result = {"status": "error", "error": str(exc)}
        status = result.get("status")
        if status in {"completed", "partial"} and sid not in state["processed_seed_ids"]:
            state["processed_seed_ids"].append(sid)
        if status == "error" and sid not in state["failed_seed_ids"]:
            state["failed_seed_ids"].append(sid)
            state.setdefault("failed_details", []).append({"seed_id": sid, "reason": str(result.get("error", "")), "created_at": _now()})
        if status in {"completed", "partial"}:
            state["last_completed_seed_id"] = sid
        state["in_progress_seed_id"] = None
        results.append({"seed": seed, "result": result})
        _refresh_coverage(state, ordered)
        _save_state(state)
    return results


def run_next_batch(limit=5, market="IL", make_filter=None, force_refresh=False, use_cache=True, resume=True, include_failed=False, progress_callback: Callable | None = None):
    ordered = get_ordered_seed_list(market)
    state = load_batch_state(market) if resume else _default_state(market, ordered)
    outputs = _load_outputs()
    if state.get("in_progress_seed_id"):
        state.setdefault("failed_details", []).append({"seed_id": state["in_progress_seed_id"], "reason": "Previous run interrupted before completion", "created_at": _now()})
        state["in_progress_seed_id"] = None
    candidates = [s for s in ordered if not make_filter or s["make"].lower() == make_filter.lower()]
    coverage = audit_coverage_until_last_completed(candidates, state, outputs)
    holes = [seed_to_dict(s) for s in coverage["missing_seeds"]]
    batch_mode = "fill_coverage_holes" if holes else "resume_forward"
    queue = holes if holes else [seed_to_dict(s) for s in candidates if s["seed_id"] not in state.get("processed_seed_ids", []) and (include_failed or s["seed_id"] not in state.get("failed_seed_ids", []))]
    if not queue:
        _refresh_coverage(state, ordered)
        _save_state(state)
        return {"status": "completed_all", "batch_mode": "completed_all", "processed": 0, "remaining": 0, "holes_detected": bool(holes), "holes_count_before": len(holes), "holes_processed_this_batch": 0, "coverage_audit_after_batch": coverage}
    batch_id = str(uuid.uuid4())
    state["last_batch_id"] = batch_id
    results = _process_seeds(queue, state, ordered, limit, force_refresh, use_cache, progress_callback)
    outputs_after = _load_outputs()
    coverage_after = audit_coverage_until_last_completed(candidates, state, outputs_after)
    remaining = len(queue) - min(limit, len(queue))
    latest_batch_path = project_root() / "data/output/latest_batch_result.json"
    payload = {"batch": {"batch_id": batch_id, "started_at": _now(), "requested_limit": limit, "processed": len(results), "batch_mode": batch_mode}, "results": results}
    save_json(latest_batch_path, payload)
    return {"status": "completed", "batch_id": batch_id, "batch_mode": batch_mode, "processed": len(results), "remaining": max(remaining, 0), "results": results, "holes_detected": bool(holes), "holes_count_before": len(holes), "holes_processed_this_batch": len(results) if holes else 0, "coverage_audit_after_batch": coverage_after}


def repair_coverage_until_clean(limit_per_pass=20, max_passes=10, market="IL"):
    passes = []
    for _ in range(max_passes):
        state = load_batch_state(market)
        ordered = get_ordered_seed_list(market)
        audit = audit_coverage_until_last_completed(ordered, state, _load_outputs())
        if audit["holes_count"] == 0:
            break
        passes.append(run_next_batch(limit=limit_per_pass, market=market, resume=True))
    return {"passes": passes, "final_audit": audit_coverage_until_last_completed(get_ordered_seed_list(market), load_batch_state(market), _load_outputs())}


def get_batch_progress(market="IL") -> dict:
    ordered = get_ordered_seed_list(market)
    state = load_batch_state(market)
    _refresh_coverage(state, ordered)
    audit = audit_coverage_until_last_completed(ordered, state, _load_outputs())
    next_seed = next((seed_to_dict(s) for s in ordered if s["seed_id"] == state.get("next_seed_id")), None)
    total = len(ordered); processed = len(state.get("processed_seed_ids", [])); failed = len(state.get("failed_seed_ids", []))
    coverage_rows = sorted([{"make": m, **c, "remaining": max(c.get("total", 0)-c.get("processed", 0),0)} for m,c in state.get("coverage_by_make", {}).items()], key=lambda r:r["make"].lower())
    return {"total_seeds": total, "processed": processed, "remaining": max(total-processed, 0), "failed": failed, "percent_complete": round((processed/total)*100, 1) if total else 0.0, "current_make": (next_seed or {}).get("make"), "next_seed": next_seed, "coverage_by_make": coverage_rows, "coverage_audit": audit}


def build_final_export(include_partial=True, include_verified=True, include_conflicts=False, include_unresolved=False, merge_trim_options=True, strict_no_mock=True) -> dict:
    p = get_output_paths()
    verified = load_json_list(p["vehicle_variants_verified"]) if include_verified else []
    partial = load_json_list(p["vehicle_variants_partial"]) if include_partial else []
    final_export = build_clean_final_export(
        verified_variants=verified,
        partial_variants=partial,
        sources=load_json_list(p["vehicle_sources"]),
        conflicts=load_json_list(p["vehicle_conflicts"]),
        unresolved=load_json_list(p["unresolved_models"]),
        include_partial=include_partial,
        include_verified=include_verified,
        include_conflicts=include_conflicts,
        include_unresolved=include_unresolved,
        merge_trim_options=merge_trim_options,
        strict_no_mock=strict_no_mock,
    )
    assert_no_mock_in_final_export(final_export)
    return final_export


def build_resume_package() -> dict:
    p = get_output_paths()
    return {"schema_version": "resume_package_v1", "created_at": _now(), "batch_state": load_batch_state(), "run_history": load_json_list(p["run_history"]), "verified_variants": load_json_list(p["vehicle_variants_verified"]), "partial_variants": load_json_list(p["vehicle_variants_partial"]), "sources": load_json_list(p["vehicle_sources"]), "unresolved": load_json_list(p["unresolved_models"]), "conflicts": load_json_list(p["vehicle_conflicts"])}


def rebuild_batch_state_from_outputs(market="IL") -> dict:
    ordered = get_ordered_seed_list(market); state = _default_state(market, ordered)
    outputs = _load_outputs()
    for run in outputs["run_history"]:
        sid = run.get("seed_id") or build_seed_id(run.get("make"), run.get("model"), run.get("year_start") or 0, run.get("year_end") or 0, run.get("market") or market)
        if is_seed_completed(sid, outputs, state) and sid not in state["processed_seed_ids"]:
            state["processed_seed_ids"].append(sid)
        if run.get("status") == "error" and sid not in state["failed_seed_ids"]:
            state["failed_seed_ids"].append(sid)
    remaining = [s for s in ordered if s["seed_id"] not in state["processed_seed_ids"]]
    state["next_seed_id"] = remaining[0]["seed_id"] if remaining else None
    _refresh_coverage(state, ordered); _save_state(state); return state


def cleanup_retryable_schema_errors(market: str = "IL") -> dict:
    state = load_batch_state(market)
    failed_details = state.get("failed_details", [])
    retryable_seed_ids = set()
    kept_failed_details = []
    for detail in failed_details:
        reason = str(detail.get("reason", "")).lower()
        if any(token in reason for token in RETRYABLE_SCHEMA_ERROR_TOKENS):
            retryable_seed_ids.add(detail.get("seed_id"))
            continue
        kept_failed_details.append(detail)
    state["failed_details"] = kept_failed_details
    before_failed = set(state.get("failed_seed_ids", []))
    cleaned_ids = [sid for sid in before_failed if sid in retryable_seed_ids]
    state["failed_seed_ids"] = [sid for sid in state.get("failed_seed_ids", []) if sid not in retryable_seed_ids]
    state["processed_seed_ids"] = [sid for sid in state.get("processed_seed_ids", []) if sid not in retryable_seed_ids]
    if state.get("last_completed_seed_id") in retryable_seed_ids:
        state["last_completed_seed_id"] = None
    _refresh_coverage(state, get_ordered_seed_list(market))
    _save_state(state)
    return {"status": "ok", "cleaned_seed_ids": cleaned_ids, "cleaned_count": len(cleaned_ids)}


def detect_import_file_type(uploaded_json) -> str:
    if isinstance(uploaded_json, list):
        if uploaded_json and isinstance(uploaded_json[0], dict) and "run_id" in uploaded_json[0]:
            return "run_history"
        return "unknown"
    if uploaded_json.get("schema_version") == "resume_package_v1":
        return "resume_package"
    if uploaded_json.get("schema_version") == BATCH_STATE_SCHEMA or "processed_seed_ids" in uploaded_json:
        return "batch_state"
    if "batch" in uploaded_json and "results" in uploaded_json:
        return "latest_batch_result"
    if uploaded_json.get("schema_version") == "vehicle_variants_final_v1" or "variants" in uploaded_json:
        return "final_export"
    return "unknown"


def import_progress_json(uploaded_json: dict | list, overwrite: bool = False, market: str = "IL") -> dict:
    file_type = detect_import_file_type(uploaded_json if isinstance(uploaded_json, dict) else uploaded_json)
    paths = get_output_paths()
    state = load_batch_state(market)
    result = {"import_status": "completed", "file_type": file_type, "processed_added": 0, "variants_verified_added": 0, "variants_partial_added": 0, "run_history_added": 0, "warnings": []}
    if file_type == "batch_state":
        incoming = uploaded_json
        incoming_processed = set(incoming.get("processed_seed_ids", []))
        local_processed = set(state.get("processed_seed_ids", []))
        merged = incoming if overwrite or len(incoming_processed) >= len(local_processed) else state
        if not overwrite:
            merged["processed_seed_ids"] = sorted(local_processed | incoming_processed)
            merged["failed_seed_ids"] = sorted(set(state.get("failed_seed_ids", [])) | set(incoming.get("failed_seed_ids", [])))
        save_json(_batch_state_path(), merged)
        result["processed_added"] = len(set(merged.get("processed_seed_ids", [])) - local_processed)
    elif file_type == "latest_batch_result":
        rows = uploaded_json.get("results", [])
        for item in rows:
            sid = item.get("seed", {}).get("seed_id")
            status = (item.get("result") or {}).get("status")
            if sid and status in {"completed", "partial"} and sid not in state["processed_seed_ids"]:
                state["processed_seed_ids"].append(sid); result["processed_added"] += 1
        _save_state(state)
    elif file_type == "run_history":
        existing = load_json_list(paths["run_history"])
        old_ids = {r.get("run_id") for r in existing}
        merged = existing + [r for r in uploaded_json if r.get("run_id") not in old_ids]
        save_json(paths["run_history"], merged)
        result["run_history_added"] = len(merged) - len(existing)
    elif file_type == "final_export":
        variants = uploaded_json.get("variants", [])
        verified = load_json_list(paths["vehicle_variants_verified"])
        partial = load_json_list(paths["vehicle_variants_partial"])
        vid_verified = {v.get("variant_id") for v in verified}
        vid_partial = {v.get("variant_id") for v in partial}
        for v in variants:
            vid = v.get("variant_id")
            status = str(v.get("classification") or v.get("status") or "").lower()
            if status == "verified" and vid not in vid_verified:
                verified.append(v); vid_verified.add(vid); result["variants_verified_added"] += 1
            elif vid not in vid_partial and vid not in vid_verified:
                partial.append(v); vid_partial.add(vid); result["variants_partial_added"] += 1
        save_json(paths["vehicle_variants_verified"], verified)
        save_json(paths["vehicle_variants_partial"], [v for v in partial if v.get("variant_id") not in vid_verified])
    elif file_type == "resume_package":
        pkg = uploaded_json
        save_json(paths["run_history"], pkg.get("run_history", []))
        save_json(paths["vehicle_variants_verified"], pkg.get("verified_variants", []))
        save_json(paths["vehicle_variants_partial"], pkg.get("partial_variants", []))
        save_json(paths["vehicle_sources"], pkg.get("sources", []))
        save_json(paths["unresolved_models"], pkg.get("unresolved", []))
        save_json(paths["vehicle_conflicts"], pkg.get("conflicts", []))
        save_json(_batch_state_path(), pkg.get("batch_state", state))
    else:
        result["import_status"] = "skipped"
        result["warnings"].append("Unknown import file type")
    rebuild_batch_state_from_outputs(market)
    return result

from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Callable

from core.ingest import load_model_seeds
from agent.runner import run_single_model
from storage.json_store import get_output_paths, load_json_list, load_json_object, save_json, project_root
from core.final_export_builder import build_clean_final_export, assert_no_mock_in_final_export, is_mock_contaminated_variant

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


def seed_to_dict(seed: dict, default_market: str = "IL") -> dict:
    market = seed.get("market") or default_market
    return {
        "seed_id": seed["seed_id"],
        "make": seed["make"],
        "model": seed["model"],
        "year_start": seed["year_start"],
        "year_end": seed["year_end"],
        "market": market,
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


def _is_verified_variant(variant: dict) -> bool:
    status = str(variant.get("verification_status") or variant.get("classification") or variant.get("status") or "").lower()
    return status == "verified"


def _merge_variant_lists(existing: list[dict], incoming: list[dict]) -> list[dict]:
    by_id = {}
    for row in [*(existing or []), *(incoming or [])]:
        if not isinstance(row, dict):
            continue
        vid = row.get("variant_id")
        if not vid:
            continue
        current = by_id.get(vid)
        if current is None:
            by_id[vid] = dict(row)
            continue
        pick_new = _is_verified_variant(row) and not _is_verified_variant(current)
        if pick_new:
            merged = dict(current)
            merged.update(row)
            merged["source_ids"] = sorted(set((current.get("source_ids") or []) + (row.get("source_ids") or [])))
            merged["trim_options"] = current.get("trim_options") or row.get("trim_options")
            by_id[vid] = merged
    return list(by_id.values())


def _split_variants(variants: list[dict]) -> tuple[list[dict], list[dict]]:
    verified = [v for v in variants if isinstance(v, dict) and _is_verified_variant(v)]
    partial = [v for v in variants if isinstance(v, dict) and not _is_verified_variant(v)]
    return verified, partial


def load_all_accumulated_variants() -> dict:
    paths = get_output_paths()
    inputs_loaded = {
        "vehicle_variants_verified": 0,
        "vehicle_variants_partial": 0,
        "combined_clean": 0,
        "combined_old": 0,
        "latest_batch_candidates": 0,
        "resume_package": 0,
    }
    verified = load_json_list(paths["vehicle_variants_verified"])
    partial = load_json_list(paths["vehicle_variants_partial"])
    inputs_loaded["vehicle_variants_verified"] = len(verified)
    inputs_loaded["vehicle_variants_partial"] = len(partial)
    sources = load_json_list(paths["vehicle_sources"])

    combined_clean = load_json_object(project_root() / "data/output/combined_vehicle_variants_final_clean.json")
    if isinstance(combined_clean, dict):
        clean_variants = [v for v in combined_clean.get("variants", []) if isinstance(v, dict)]
        inputs_loaded["combined_clean"] = len(clean_variants)
        v_clean, p_clean = _split_variants(clean_variants)
        verified = _merge_variant_lists(verified, v_clean)
        partial = _merge_variant_lists(partial, p_clean)

    combined_old = load_json_object(project_root() / "data/output/combined_vehicle_variants_final.json")
    if isinstance(combined_old, dict):
        old_variants = [v for v in combined_old.get("variants", []) if isinstance(v, dict)]
        inputs_loaded["combined_old"] = len(old_variants)
        v_old, p_old = _split_variants(old_variants)
        verified = _merge_variant_lists(verified, v_old)
        partial = _merge_variant_lists(partial, p_old)

    imported_dataset = load_json_object(project_root() / "data/output/imported_accumulated_dataset.json")
    if isinstance(imported_dataset, dict):
        imported_variants = [v for v in imported_dataset.get("variants", []) if isinstance(v, dict)]
        if not imported_variants and isinstance(imported_dataset.get("accumulated_clean_export"), dict):
            imported_variants = [
                v for v in imported_dataset["accumulated_clean_export"].get("variants", []) if isinstance(v, dict)
            ]
        inputs_loaded["resume_package"] = len(imported_variants)
        v_imp, p_imp = _split_variants(imported_variants)
        verified = _merge_variant_lists(verified, v_imp)
        partial = _merge_variant_lists(partial, p_imp)

    latest = load_json_object(project_root() / "data/output/latest_batch_result.json")
    rebuilt = []
    for row in latest.get("results", []):
        parsed = (((row.get("result") or {}).get("trace") or {}).get("discovery_parsed_json_debug") or {})
        rebuilt.extend([v for v in parsed.get("candidate_variants", []) if isinstance(v, dict)])
    inputs_loaded["latest_batch_candidates"] = len(rebuilt)
    if rebuilt:
        _, rebuilt_partial = _split_variants(rebuilt)
        partial = _merge_variant_lists(partial, rebuilt_partial)

    run_history = load_json_list(paths["run_history"])
    for run in run_history:
        if not isinstance(run, dict):
            continue
        embedded = [v for v in (run.get("variants") or []) if isinstance(v, dict)]
        if not embedded:
            continue
        ev, ep = _split_variants(embedded)
        verified = _merge_variant_lists(verified, ev)
        partial = _merge_variant_lists(partial, ep)

    verified = [v for v in verified if not is_mock_contaminated_variant(v)]
    verified_ids = {v.get("variant_id") for v in verified if isinstance(v, dict)}
    partial = [v for v in partial if isinstance(v, dict) and v.get("variant_id") not in verified_ids and not is_mock_contaminated_variant(v)]
    return {"verified": verified, "partial": partial, "sources": sources, "inputs_loaded": inputs_loaded}


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
        selected_market = state.get("market")
        seed["market"] = seed.get("market") or selected_market or "IL"
        market = seed["market"]
        if not seed.get("seed_id"):
            seed["seed_id"] = build_seed_id(seed.get("make"), seed.get("model"), seed.get("year_start"), seed.get("year_end"), market)
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
    holes = [seed_to_dict(s, default_market=market) for s in coverage["missing_seeds"]]
    batch_mode = "fill_coverage_holes" if holes else "resume_forward"
    queue = holes if holes else [seed_to_dict(s, default_market=market) for s in candidates if s["seed_id"] not in state.get("processed_seed_ids", []) and (include_failed or s["seed_id"] not in state.get("failed_seed_ids", []))]
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
    payload = {"batch": {"batch_id": batch_id, "started_at": _now(), "requested_limit": limit, "processed": len(results), "batch_mode": batch_mode}, "results": results, "coverage_audit_after_batch": coverage_after}
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
    loaded = load_all_accumulated_variants()
    verified = loaded["verified"] if include_verified else []
    partial = loaded["partial"] if include_partial else []
    final_export = build_clean_final_export(
        verified_variants=verified,
        partial_variants=partial,
        sources=loaded["sources"],
        conflicts=load_json_list(p["vehicle_conflicts"]),
        unresolved=load_json_list(p["unresolved_models"]),
        include_partial=include_partial,
        include_verified=include_verified,
        include_conflicts=include_conflicts,
        include_unresolved=include_unresolved,
        merge_trim_options=merge_trim_options,
        strict_no_mock=strict_no_mock,
    )
    final_export.setdefault("audit", {})["inputs_loaded"] = loaded["inputs_loaded"]
    assert_no_mock_in_final_export(final_export)
    return final_export


def build_resume_package() -> dict:
    p = get_output_paths()
    accumulated_clean_export = build_final_export()
    variants = accumulated_clean_export.get("variants", [])
    makes = {str(v.get("make", "")).strip().lower() for v in variants if isinstance(v, dict) and v.get("make")}
    models = {f"{str(v.get('make','')).strip().lower()}::{str(v.get('model','')).strip().lower()}" for v in variants if isinstance(v, dict) and v.get("make") and v.get("model")}
    return {
        "schema_version": "resume_package_v1",
        "created_at": _now(),
        "batch_state": load_batch_state(),
        "run_history": load_json_list(p["run_history"]),
        "verified_variants": load_json_list(p["vehicle_variants_verified"]),
        "partial_variants": load_json_list(p["vehicle_variants_partial"]),
        "sources": load_json_list(p["vehicle_sources"]),
        "unresolved": load_json_list(p["unresolved_models"]),
        "conflicts": load_json_list(p["vehicle_conflicts"]),
        "accumulated_clean_export": accumulated_clean_export,
        "counts": {"total_variants": len(variants), "makes_count": len(makes), "models_count": len(models)},
    }


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
        if uploaded_json and isinstance(uploaded_json[0], dict) and "variant_id" in uploaded_json[0]:
            return "accumulated_variants"
        return "unknown"
    if uploaded_json.get("schema_version") in {"resume_package_v1", "vehicle_variant_resume_package_v1"}:
        return "resume_package"
    if isinstance(uploaded_json.get("batch_state"), dict) and isinstance(uploaded_json.get("final_export"), dict) and isinstance(uploaded_json.get("final_export", {}).get("variants"), list):
        return "resume_package"
    if uploaded_json.get("schema_version") == BATCH_STATE_SCHEMA or "processed_seed_ids" in uploaded_json:
        return "batch_state"
    if "batch" in uploaded_json and "results" in uploaded_json:
        return "latest_batch_result"
    if uploaded_json.get("schema_version") == "vehicle_variants_final_v1" or "variants" in uploaded_json:
        return "final_export"
    return "unknown"


def _normalize_imported_batch_state(imported_state: dict, market: str = "IL") -> dict:
    ordered = get_ordered_seed_list(market)
    canonical_seed_ids = [s["seed_id"] for s in ordered]
    canonical_set = set(canonical_seed_ids)
    processed_seed_ids = [sid for sid in (imported_state.get("processed_seed_ids") or []) if sid in canonical_set]
    failed_seed_ids = [sid for sid in (imported_state.get("failed_seed_ids") or []) if sid in canonical_set]

    now = _now()
    normalized = {
        "schema_version": BATCH_STATE_SCHEMA,
        "market": imported_state.get("market") or market or "IL",
        "created_at": imported_state.get("created_at") or now,
        "updated_at": now,
        "last_batch_id": imported_state.get("last_batch_id"),
        "total_seeds": len(ordered),
        "processed_seed_ids": processed_seed_ids,
        "processed_seeds": imported_state.get("processed_seeds", len(processed_seed_ids)),
        "failed_seed_ids": failed_seed_ids,
        "skipped_seed_ids": imported_state.get("skipped_seed_ids", []),
        "in_progress_seed_id": None,
        "run_history": imported_state.get("run_history", []),
        "failed_details": imported_state.get("failed_details", []),
    }

    furthest_idx = max([i for i, sid in enumerate(canonical_seed_ids) if sid in set(processed_seed_ids)], default=-1)
    normalized["last_completed_seed_id"] = canonical_seed_ids[furthest_idx] if furthest_idx >= 0 else None
    normalized["next_seed_id"] = next((sid for sid in canonical_seed_ids if sid not in set(processed_seed_ids)), None)
    _refresh_coverage(normalized, ordered)
    return normalized


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
    elif file_type in {"final_export", "accumulated_variants"}:
        variants = uploaded_json if isinstance(uploaded_json, list) else uploaded_json.get("variants", [])
        save_json(project_root() / "data/output/imported_accumulated_dataset.json", {"created_at": _now(), "variants": variants})
        verified = load_json_list(paths["vehicle_variants_verified"])
        partial = load_json_list(paths["vehicle_variants_partial"])
        merged_verified = _merge_variant_lists(verified, [v for v in variants if _is_verified_variant(v)])
        merged_partial = _merge_variant_lists(partial, [v for v in variants if not _is_verified_variant(v)])
        verified_ids = {v.get("variant_id") for v in merged_verified}
        merged_partial = [v for v in merged_partial if v.get("variant_id") not in verified_ids]
        result["variants_verified_added"] = max(0, len(merged_verified) - len(verified))
        result["variants_partial_added"] = max(0, len(merged_partial) - len(partial))
        save_json(paths["vehicle_variants_verified"], merged_verified)
        save_json(paths["vehicle_variants_partial"], merged_partial)
    elif file_type == "resume_package":
        pkg = uploaded_json if isinstance(uploaded_json, dict) else {}
        schema_version = pkg.get("schema_version")
        if schema_version == "vehicle_variant_resume_package_v1" and isinstance(pkg.get("final_export"), dict):
            acc = pkg.get("final_export", {})
            imported_sources = acc.get("sources", []) if isinstance(acc.get("sources", []), list) else []
        else:
            acc = pkg.get("accumulated_clean_export", {}) if isinstance(pkg.get("accumulated_clean_export"), dict) else {}
            imported_sources = pkg.get("sources", []) if isinstance(pkg.get("sources", []), list) else []
        variants = [v for v in (acc.get("variants", []) if isinstance(acc, dict) else []) if isinstance(v, dict)]
        save_json(project_root() / "data/output/imported_accumulated_dataset.json", acc if isinstance(acc, dict) else {"variants": variants})
        verified = load_json_list(paths["vehicle_variants_verified"])
        partial = load_json_list(paths["vehicle_variants_partial"])
        v_new, p_new = _split_variants(variants)
        merged_verified = _merge_variant_lists([] if overwrite else verified, v_new)
        merged_partial = _merge_variant_lists([] if overwrite else partial, p_new)
        verified_ids = {v.get("variant_id") for v in merged_verified}
        merged_partial = [v for v in merged_partial if v.get("variant_id") not in verified_ids]
        save_json(paths["vehicle_variants_verified"], merged_verified)
        save_json(paths["vehicle_variants_partial"], merged_partial)
        if imported_sources:
            save_json(paths["vehicle_sources"], imported_sources if overwrite else (load_json_list(paths["vehicle_sources"]) + imported_sources))
        if schema_version != "vehicle_variant_resume_package_v1":
            if overwrite:
                save_json(paths["run_history"], pkg.get("run_history", []))
            else:
                save_json(paths["run_history"], load_json_list(paths["run_history"]) + pkg.get("run_history", []))
            save_json(paths["unresolved_models"], pkg.get("unresolved", []))
            save_json(paths["vehicle_conflicts"], pkg.get("conflicts", []))
        imported_state = pkg.get("batch_state", state) if isinstance(pkg.get("batch_state"), dict) else state
        normalized_state = _normalize_imported_batch_state(imported_state, market=market)
        save_json(_batch_state_path(), normalized_state)
        result["processed_added"] = max(0, len(set(normalized_state.get("processed_seed_ids", [])) - set(state.get("processed_seed_ids", []))))
        result["variants_verified_added"] = max(0, len(merged_verified) - len(verified))
        result["variants_partial_added"] = max(0, len(merged_partial) - len(partial))
        c = acc.get("counts", {}) if isinstance(acc, dict) else {}
        result["imported_variants"] = len(variants)
        result["imported_makes"] = c.get("makes_count")
        result["imported_models"] = c.get("models_count")
    else:
        result["import_status"] = "skipped"
        result["warnings"].append("Unknown import file type")
    if file_type != "resume_package":
        rebuild_batch_state_from_outputs(market)
    else:
        audit_coverage_until_last_completed(get_ordered_seed_list(market), load_batch_state(market), _load_outputs())
    return result

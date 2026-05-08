from __future__ import annotations

from datetime import datetime, timezone
import copy
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


def _field_value(variant: dict, field_name: str):
    value = variant.get(field_name)
    if isinstance(value, dict):
        return value.get("value", value)
    return value


def _norm_token(value) -> str:
    return str(value if value is not None else "").strip().lower()


def _variant_identity_key(variant: dict) -> str:
    parts = [
        _field_value(variant, "make"),
        _field_value(variant, "model"),
        _field_value(variant, "market"),
        _field_value(variant, "year_start"),
        _field_value(variant, "year_end"),
        _field_value(variant, "generation"),
        _field_value(variant, "body_type"),
        _field_value(variant, "seats"),
        _field_value(variant, "engine"),
        _field_value(variant, "transmission"),
        _field_value(variant, "fuel_type"),
        _field_value(variant, "drivetrain"),
    ]
    return "|".join(_norm_token(p) for p in parts)


def _variant_dedupe_key(variant: dict) -> str | None:
    variant_id = _norm_token(variant.get("variant_id"))
    if variant_id:
        return f"id:{variant_id}"
    key = _variant_identity_key(variant)
    return f"identity:{key}" if key.replace("|", "").strip() else None


def _variant_status_rank(variant: dict) -> int:
    status = _norm_token(variant.get("verification_status") or variant.get("classification") or variant.get("status"))
    if status == "verified":
        return 2
    if status == "partial":
        return 1
    return 0


def _variant_completeness_score(variant: dict) -> int:
    fields = [
        "make",
        "model",
        "market",
        "year_start",
        "year_end",
        "generation",
        "body_type",
        "seats",
        "engine",
        "transmission",
        "fuel_type",
        "drivetrain",
        "trim",
    ]
    return sum(1 for f in fields if _field_value(variant, f) not in (None, ""))


def _unique_strings(items: list) -> list[str]:
    out = []
    seen = set()
    for item in items or []:
        if not isinstance(item, str):
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _trim_option_entries(variant: dict) -> list[dict]:
    opts = []
    for row in variant.get("trim_options") or []:
        if isinstance(row, dict):
            opts.append(copy.deepcopy(row))
    trim = _field_value(variant, "trim")
    trim_field = variant.get("trim")
    if trim not in (None, ""):
        item = {"value": trim}
        if isinstance(trim_field, dict):
            item["source_ids"] = _unique_strings(trim_field.get("source_ids") or trim_field.get("source_urls") or [])
            item["status"] = trim_field.get("status")
            item["sources_count"] = int(trim_field.get("sources_count", 0) or 0)
        opts.append(item)
    return opts


def _merge_trim_options(existing: dict, incoming: dict) -> list[dict]:
    merged = []
    seen = set()
    for row in _trim_option_entries(existing) + _trim_option_entries(incoming):
        key = (
            _norm_token(row.get("value")),
            tuple(sorted(_unique_strings(row.get("source_ids") or []))),
            _norm_token(row.get("status")),
            int(row.get("sources_count", 0) or 0),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged


def _is_real_full_variant(variant: dict) -> bool:
    if not isinstance(variant, dict):
        return False
    if not _norm_token(variant.get("variant_id")):
        return False
    required = ["make", "model", "market", "year_start", "year_end"]
    return all(_field_value(variant, f) not in (None, "") for f in required)


def _merge_variant_pair(current: dict, incoming: dict) -> dict:
    current_rank = _variant_status_rank(current)
    incoming_rank = _variant_status_rank(incoming)
    if incoming_rank > current_rank:
        primary, secondary = incoming, current
    elif incoming_rank < current_rank:
        primary, secondary = current, incoming
    else:
        incoming_score = _variant_completeness_score(incoming)
        current_score = _variant_completeness_score(current)
        primary, secondary = (incoming, current) if incoming_score > current_score else (current, incoming)
    merged = copy.deepcopy(secondary)
    merged.update(copy.deepcopy(primary))
    merged["source_ids"] = _unique_strings((secondary.get("source_ids") or []) + (primary.get("source_ids") or []))
    trims = _merge_trim_options(secondary, primary)
    if trims:
        merged["trim_options"] = trims
    if current_rank == 2 or incoming_rank == 2:
        merged["verification_status"] = "verified"
    elif current_rank == 1 or incoming_rank == 1:
        merged["verification_status"] = merged.get("verification_status") or merged.get("classification") or "partial"
    return merged


def dedupe_variants_stable(variants: list[dict]) -> list[dict]:
    by_key: dict[str, dict] = {}
    for row in variants or []:
        if not isinstance(row, dict):
            continue
        key = _variant_dedupe_key(row)
        if key is None:
            continue
        current = by_key.get(key)
        if current is None:
            by_key[key] = copy.deepcopy(row)
            continue
        by_key[key] = _merge_variant_pair(current, row)
    return list(by_key.values())


def _merge_variant_lists(existing: list[dict], incoming: list[dict]) -> list[dict]:
    return dedupe_variants_stable([*(existing or []), *(incoming or [])])


def _split_variants(variants: list[dict]) -> tuple[list[dict], list[dict]]:
    verified = [v for v in variants if isinstance(v, dict) and _is_verified_variant(v)]
    partial = [v for v in variants if isinstance(v, dict) and not _is_verified_variant(v)]
    return verified, partial


def load_imported_accumulated_variants() -> list[dict]:
    imported_dataset = load_json_object(project_root() / "data/output/imported_accumulated_dataset.json")
    if not isinstance(imported_dataset, dict):
        return []
    variants: list[dict] = []
    buckets = [
        imported_dataset.get("variants"),
        (imported_dataset.get("accumulated_clean_export") or {}).get("variants") if isinstance(imported_dataset.get("accumulated_clean_export"), dict) else None,
        (imported_dataset.get("final_export") or {}).get("variants") if isinstance(imported_dataset.get("final_export"), dict) else None,
    ]
    for bucket in buckets:
        if not isinstance(bucket, list):
            continue
        variants.extend([copy.deepcopy(v) for v in bucket if isinstance(v, dict)])
    return dedupe_variants_stable(variants)


def _extract_result_variants(result_row: dict) -> list[dict]:
    if not isinstance(result_row, dict):
        return []
    result = result_row.get("result") if isinstance(result_row.get("result"), dict) else result_row
    variants = []
    for key in ["variants", "verified_variants", "partial_variants", "accumulated_variants"]:
        bucket = result.get(key) if isinstance(result, dict) else None
        if isinstance(bucket, list):
            variants.extend([v for v in bucket if isinstance(v, dict)])
    parsed = (((result.get("trace") or {}).get("discovery_parsed_json_debug") or {}) if isinstance(result, dict) else {})
    candidate_variants = parsed.get("candidate_variants", []) if isinstance(parsed, dict) else []
    if isinstance(candidate_variants, list):
        variants.extend([v for v in candidate_variants if isinstance(v, dict) and _is_real_full_variant(v)])
    return [v for v in variants if _is_real_full_variant(v)]


def load_all_accumulated_variants() -> dict:
    paths = get_output_paths()
    inputs_loaded = {
        "imported_accumulated_dataset": 0,
        "combined_clean": 0,
        "combined_old": 0,
        "vehicle_variants_verified": 0,
        "vehicle_variants_partial": 0,
        "run_history_embedded": 0,
        "latest_batch_full_variants": 0,
    }
    merged_variants: list[dict] = []

    imported_variants = [v for v in load_imported_accumulated_variants() if isinstance(v, dict)]
    inputs_loaded["imported_accumulated_dataset"] = len(imported_variants)
    merged_variants.extend(imported_variants)

    combined_clean = load_json_object(project_root() / "data/output/combined_vehicle_variants_final_clean.json")
    clean_variants = [v for v in combined_clean.get("variants", []) if isinstance(v, dict)] if isinstance(combined_clean, dict) else []
    inputs_loaded["combined_clean"] = len(clean_variants)
    merged_variants.extend(clean_variants)

    combined_old = load_json_object(project_root() / "data/output/combined_vehicle_variants_final.json")
    old_variants = [v for v in combined_old.get("variants", []) if isinstance(v, dict)] if isinstance(combined_old, dict) else []
    inputs_loaded["combined_old"] = len(old_variants)
    merged_variants.extend(old_variants)

    verified = [v for v in load_json_list(paths["vehicle_variants_verified"]) if isinstance(v, dict)]
    partial = [v for v in load_json_list(paths["vehicle_variants_partial"]) if isinstance(v, dict)]
    inputs_loaded["vehicle_variants_verified"] = len(verified)
    inputs_loaded["vehicle_variants_partial"] = len(partial)
    merged_variants.extend(verified)
    merged_variants.extend(partial)

    run_history = load_json_list(paths["run_history"])
    history_variants = []
    for run in run_history:
        if not isinstance(run, dict):
            continue
        history_variants.extend([v for v in (run.get("variants") or []) if isinstance(v, dict) and _is_real_full_variant(v)])
    inputs_loaded["run_history_embedded"] = len(history_variants)
    merged_variants.extend(history_variants)

    latest = load_json_object(project_root() / "data/output/latest_batch_result.json")
    latest_variants = []
    for row in latest.get("results", []) if isinstance(latest, dict) else []:
        latest_variants.extend(_extract_result_variants(row))
    inputs_loaded["latest_batch_full_variants"] = len(latest_variants)
    merged_variants.extend(latest_variants)

    sources = load_json_list(paths["vehicle_sources"])
    deduped = [v for v in dedupe_variants_stable(merged_variants) if isinstance(v, dict) and not is_mock_contaminated_variant(v)]
    verified, partial = _split_variants(deduped)
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
    final_export["audit"]["accumulation_counts"] = {
        "imported_accumulated_dataset": int(loaded["inputs_loaded"].get("imported_accumulated_dataset", 0) or 0),
        "verified_output": int(loaded["inputs_loaded"].get("vehicle_variants_verified", 0) or 0),
        "partial_output": int(loaded["inputs_loaded"].get("vehicle_variants_partial", 0) or 0),
        "final_merged_variants": len(final_export.get("variants", [])),
        "shrink_guard_previous_count": int(loaded["inputs_loaded"].get("imported_accumulated_dataset", 0) or 0),
        "shrink_guard_new_count": len(final_export.get("variants", [])),
    }
    assert_no_mock_in_final_export(final_export)
    return final_export


def build_resume_package() -> dict:
    p = get_output_paths()
    accumulated_clean_export = build_final_export()
    variants = accumulated_clean_export.get("variants", [])
    shrink = ((accumulated_clean_export.get("audit") or {}).get("accumulation_counts") or {})
    previous_count = int(shrink.get("shrink_guard_previous_count", 0) or 0)
    new_count = int(shrink.get("shrink_guard_new_count", len(variants)) or 0)
    if previous_count > 0 and new_count < previous_count:
        raise ValueError("Accumulated export shrink detected. Refusing to generate resume package.")
    makes = {str(v.get("make", "")).strip().lower() for v in variants if isinstance(v, dict) and v.get("make")}
    models = {f"{str(v.get('make','')).strip().lower()}::{str(v.get('model','')).strip().lower()}" for v in variants if isinstance(v, dict) and v.get("make") and v.get("model")}
    normalized_state = normalize_batch_state_for_resume(load_batch_state(), get_ordered_seed_list("IL"), variants=variants, market="IL")
    return {
        "schema_version": "resume_package_v1",
        "created_at": _now(),
        "batch_state": normalized_state,
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
    return normalize_batch_state_for_resume(imported_state, get_ordered_seed_list(market), market=market)


def normalize_batch_state_for_resume(batch_state: dict, ordered_seeds: list[dict], variants: list[dict] | None = None, market: str = "IL") -> dict:
    canonical_by_id = {s["seed_id"]: seed_to_dict(s, default_market=market) for s in ordered_seeds}
    canonical_ids = [s["seed_id"] for s in ordered_seeds]
    canonical_set = set(canonical_ids)

    def _parse_sid(sid: str):
        parts = str(sid or "").split("__")
        if len(parts) != 5:
            return None
        try:
            return {"make": parts[0], "model": parts[1], "year_start": int(parts[2]), "year_end": int(parts[3]), "market": parts[4]}
        except Exception:
            return None

    variants = variants or []
    seen_mm_year = set()
    for v in variants:
        if not isinstance(v, dict):
            continue
        mk = normalize_token(v.get("make"))
        md = normalize_token(v.get("model"))
        ys = v.get("year_start")
        ye = v.get("year_end")
        if mk and md and isinstance(ys, int) and isinstance(ye, int):
            seen_mm_year.add((mk, md, ys, ye))

    incoming_ids = list(batch_state.get("processed_seed_ids") or [])
    processed_seeds_rows = batch_state.get("processed_seeds") if isinstance(batch_state.get("processed_seeds"), list) else []
    for s in processed_seeds_rows:
        if isinstance(s, dict) and s.get("seed_id"):
            incoming_ids.append(s["seed_id"])

    processed = set()
    for sid in incoming_ids:
        if sid in canonical_set:
            processed.add(sid)
            continue
        legacy = _parse_sid(sid)
        if not legacy:
            continue
        for can in ordered_seeds:
            cmk = normalize_token(can.get("make"))
            cmd = normalize_token(can.get("model"))
            if legacy["make"] != cmk or legacy["model"] != cmd:
                continue
            cys, cye = int(can["year_start"]), int(can["year_end"])
            overlaps = not (legacy["year_end"] < cys or legacy["year_start"] > cye)
            has_variant_overlap = any(mk == cmk and md == cmd and not (ye < cys or ys > cye) for mk, md, ys, ye in seen_mm_year)
            if overlaps and (has_variant_overlap or (legacy["year_start"] >= cys and legacy["year_end"] <= cye)):
                processed.add(can["seed_id"])

    ordered_processed = [sid for sid in canonical_ids if sid in processed]
    processed_set = set(ordered_processed)
    next_seed_id = next((sid for sid in canonical_ids if sid not in processed_set), None)
    contiguous_idx = -1
    for idx, sid in enumerate(canonical_ids):
        if sid in processed_set:
            contiguous_idx = idx
            continue
        break
    last_completed = canonical_ids[contiguous_idx] if contiguous_idx >= 0 else None

    failed_seed_ids = [sid for sid in (batch_state.get("failed_seed_ids") or []) if sid in canonical_set and sid not in processed_set]
    skipped_seed_ids = [sid for sid in (batch_state.get("skipped_seed_ids") or []) if sid in canonical_set and sid not in processed_set]
    failed_details = [d for d in (batch_state.get("failed_details") or []) if isinstance(d, dict) and d.get("seed_id") not in processed_set]

    now = _now()
    normalized = {
        "schema_version": BATCH_STATE_SCHEMA,
        "market": batch_state.get("market") or market or "IL",
        "created_at": batch_state.get("created_at") or now,
        "updated_at": now,
        "last_batch_id": batch_state.get("last_batch_id"),
        "total_seeds": len(ordered_seeds),
        "processed_seed_ids": ordered_processed,
        "processed_seeds": [canonical_by_id[sid] for sid in ordered_processed],
        "failed_seed_ids": failed_seed_ids,
        "skipped_seed_ids": skipped_seed_ids,
        "in_progress_seed_id": None,
        "last_completed_seed_id": last_completed,
        "next_seed_id": next_seed_id,
        "run_history": batch_state.get("run_history", []),
        "failed_details": failed_details,
    }
    _refresh_coverage(normalized, ordered_seeds)
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
        merged = normalize_batch_state_for_resume(merged, get_ordered_seed_list(market), market=market)
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
        existing_imported = load_imported_accumulated_variants()
        incoming = dedupe_variants_stable([v for v in variants if isinstance(v, dict)])
        if overwrite:
            merged_imported = incoming
            if len(existing_imported) > 0 and len(incoming) < len(existing_imported):
                result["warnings"].append("Destructive overwrite applied to imported accumulated dataset.")
        else:
            merged_imported = dedupe_variants_stable([*existing_imported, *incoming])
            if len(existing_imported) > 0 and len(incoming) < len(existing_imported):
                result["warnings"].append("Imported accumulated dataset merged with local accumulated variants to prevent shrink.")
        save_json(project_root() / "data/output/imported_accumulated_dataset.json", {"created_at": _now(), "variants": merged_imported})
        verified = load_json_list(paths["vehicle_variants_verified"])
        partial = load_json_list(paths["vehicle_variants_partial"])
        merged_verified = _merge_variant_lists([] if overwrite else verified, [v for v in merged_imported if _is_verified_variant(v)])
        merged_partial = _merge_variant_lists([] if overwrite else partial, [v for v in merged_imported if not _is_verified_variant(v)])
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
        incoming_variants = dedupe_variants_stable(variants)
        existing_imported = load_imported_accumulated_variants()
        if overwrite:
            imported_variants = incoming_variants
            if len(existing_imported) > 0 and len(incoming_variants) < len(existing_imported):
                result["warnings"].append("Destructive overwrite applied to imported accumulated dataset.")
        else:
            imported_variants = dedupe_variants_stable([*existing_imported, *incoming_variants])
            if len(existing_imported) > 0 and len(incoming_variants) < len(existing_imported):
                result["warnings"].append("Imported accumulated dataset merged with local accumulated variants to prevent shrink.")
        save_json(project_root() / "data/output/imported_accumulated_dataset.json", {"created_at": _now(), "variants": imported_variants})
        verified = load_json_list(paths["vehicle_variants_verified"])
        partial = load_json_list(paths["vehicle_variants_partial"])
        v_new, p_new = _split_variants(imported_variants)
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
        normalized_state = normalize_batch_state_for_resume(imported_state, get_ordered_seed_list(market), variants=variants, market=market)
        save_json(_batch_state_path(), normalized_state)
        result["processed_added"] = max(0, len(set(normalized_state.get("processed_seed_ids", [])) - set(state.get("processed_seed_ids", []))))
        result["variants_verified_added"] = max(0, len(merged_verified) - len(verified))
        result["variants_partial_added"] = max(0, len(merged_partial) - len(partial))
        c = acc.get("counts", {}) if isinstance(acc, dict) else {}
        result["imported_variants"] = len(imported_variants)
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

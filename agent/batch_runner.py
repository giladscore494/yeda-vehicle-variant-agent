from __future__ import annotations

from datetime import datetime, timezone
from collections import defaultdict
import uuid
from typing import Callable

from core.ingest import load_model_seeds
from agent.runner import run_single_model
from storage.json_store import get_output_paths, load_json_list, load_json_object, save_json, project_root

BATCH_STATE_SCHEMA = "batch_state_v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_token(value: str) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    return text.replace("/", "-").replace(" ", "_")


def build_seed_id(make: str, model: str, year_start: int, year_end: int, market: str = "IL") -> str:
    return f"{normalize_token(make)}__{normalize_token(model)}__{int(year_start)}__{int(year_end)}__{normalize_token(market)}"


def get_ordered_seed_list(market: str = "IL") -> list[dict]:
    seeds = load_model_seeds()
    ordered = sorted(
        seeds,
        key=lambda s: ((s.make or "").lower(), (s.model or "").lower(), int(s.year_start or 0), int(s.year_end or 0)),
    )
    result = []
    for seed in ordered:
        result.append(
            {
                "make": seed.make,
                "model": seed.model,
                "year_start": int(seed.year_start or 0),
                "year_end": int(seed.year_end or 0),
                "market": market,
                "seed_id": build_seed_id(seed.make, seed.model, int(seed.year_start or 0), int(seed.year_end or 0), market),
            }
        )
    return result


def _batch_state_path():
    return project_root() / "data/output/batch_state.json"


def _default_state(market: str, ordered_seeds: list[dict]) -> dict:
    by_make = _empty_coverage_by_make(ordered_seeds)
    now = _now()
    return {
        "schema_version": BATCH_STATE_SCHEMA,
        "market": market,
        "created_at": now,
        "updated_at": now,
        "last_batch_id": None,
        "total_seeds": len(ordered_seeds),
        "processed_seed_ids": [],
        "failed_seed_ids": [],
        "skipped_seed_ids": [],
        "in_progress_seed_id": None,
        "last_completed_seed_id": None,
        "next_seed_id": ordered_seeds[0]["seed_id"] if ordered_seeds else None,
        "coverage_by_make": by_make,
        "run_history": [],
        "failed_details": [],
    }


def _empty_coverage_by_make(ordered_seeds: list[dict]) -> dict:
    coverage = {}
    for seed in ordered_seeds:
        make = seed["make"]
        if make not in coverage:
            coverage[make] = {
                "total": 0,
                "processed": 0,
                "verified_variants": 0,
                "partial_variants": 0,
                "unresolved": 0,
                "failed": 0,
                "completed": False,
            }
        coverage[make]["total"] += 1
    return coverage


def load_batch_state(market: str = "IL") -> dict:
    ordered = get_ordered_seed_list(market)
    path = _batch_state_path()
    state = load_json_object(path)
    if not state or state.get("schema_version") != BATCH_STATE_SCHEMA or state.get("market") != market:
        state = _default_state(market, ordered)
        save_json(path, state)
    return state


def _save_state(state: dict):
    state["updated_at"] = _now()
    save_json(_batch_state_path(), state)


def _refresh_coverage(state: dict, ordered_seeds: list[dict]):
    coverage = _empty_coverage_by_make(ordered_seeds)
    by_seed = {s["seed_id"]: s for s in ordered_seeds}
    for seed_id in state.get("processed_seed_ids", []):
        seed = by_seed.get(seed_id)
        if seed:
            coverage[seed["make"]]["processed"] += 1
    for seed_id in state.get("failed_seed_ids", []):
        seed = by_seed.get(seed_id)
        if seed:
            coverage[seed["make"]]["failed"] += 1

    for run in load_json_list(get_output_paths()["run_history"]):
        make = run.get("make")
        if make not in coverage:
            continue
        summary = run.get("classification_summary") or {}
        coverage[make]["verified_variants"] += int(summary.get("verified_count", run.get("verified_count", 0)) or 0)
        coverage[make]["partial_variants"] += int(summary.get("partial_count", run.get("partial_count", 0)) or 0)
        coverage[make]["unresolved"] += int(summary.get("unresolved_count", run.get("unresolved_count", 0)) or 0)

    for make, c in coverage.items():
        c["completed"] = c["processed"] >= c["total"] and c["total"] > 0
    state["coverage_by_make"] = coverage


def _eligible(seed: dict, state: dict, include_failed: bool) -> bool:
    sid = seed["seed_id"]
    if sid in state.get("processed_seed_ids", []):
        return False
    if not include_failed and sid in state.get("failed_seed_ids", []):
        return False
    return True


def run_next_batch(limit=5, market="IL", make_filter=None, force_refresh=False, use_cache=True, resume=True, include_failed=False, progress_callback: Callable | None = None):
    ordered = get_ordered_seed_list(market)
    state = load_batch_state(market) if resume else _default_state(market, ordered)

    if state.get("in_progress_seed_id"):
        interrupted = state["in_progress_seed_id"]
        if interrupted not in state["failed_seed_ids"]:
            state["failed_seed_ids"].append(interrupted)
        state.setdefault("failed_details", []).append({"seed_id": interrupted, "reason": "Previous run interrupted before completion", "created_at": _now()})
        state["in_progress_seed_id"] = None

    candidates = ordered
    if make_filter:
        candidates = [s for s in ordered if s["make"].lower() == make_filter.lower()]

    queue = [s for s in candidates if _eligible(s, state, include_failed)]
    if not queue:
        _refresh_coverage(state, ordered)
        state["next_seed_id"] = None
        _save_state(state)
        return {"status": "completed_all", "message": "All seeds processed.", "processed": 0, "remaining": 0}

    batch_id = str(uuid.uuid4())
    started_at = _now()
    batch_items = queue[:limit]
    results = []
    for idx, seed in enumerate(batch_items, start=1):
        sid = seed["seed_id"]
        state["in_progress_seed_id"] = sid
        state["last_batch_id"] = batch_id
        _refresh_coverage(state, ordered)
        _save_state(state)
        if progress_callback:
            progress_callback({"index": idx, "total": len(batch_items), "seed": seed, "results": list(results)})
        try:
            result = run_single_model(seed["make"], seed["model"], seed["year_start"], seed["year_end"], market=market, use_cache=use_cache, force_refresh=force_refresh)
            status = result.get("status")
            if status in {"completed", "partial", "error"}:
                if sid not in state["processed_seed_ids"]:
                    state["processed_seed_ids"].append(sid)
                if status == "error" and sid not in state["failed_seed_ids"]:
                    state["failed_seed_ids"].append(sid)
            state["last_completed_seed_id"] = sid
            state["in_progress_seed_id"] = None
            results.append({"seed": seed, "result": result})
        except Exception as exc:  # noqa: BLE001
            if sid not in state["failed_seed_ids"]:
                state["failed_seed_ids"].append(sid)
            state.setdefault("failed_details", []).append({"seed_id": sid, "reason": str(exc), "created_at": _now()})
            state["in_progress_seed_id"] = None
            results.append({"seed": seed, "result": {"status": "error", "error": str(exc)}})

        remaining_queue = [s for s in candidates if _eligible(s, state, include_failed)]
        state["next_seed_id"] = remaining_queue[0]["seed_id"] if remaining_queue else None
        _refresh_coverage(state, ordered)
        _save_state(state)

    run_meta = {
        "batch_id": batch_id,
        "started_at": started_at,
        "finished_at": _now(),
        "requested_limit": limit,
        "processed": len(batch_items),
        "started_from_seed_id": batch_items[0]["seed_id"],
        "ended_at_seed_id": batch_items[-1]["seed_id"],
        "status": "completed",
    }
    state.setdefault("run_history", []).append(run_meta)
    _refresh_coverage(state, ordered)
    _save_state(state)

    latest_batch_path = project_root() / "data/output/latest_batch_result.json"
    save_json(latest_batch_path, {"batch": run_meta, "results": results})
    return {"status": "completed", "batch_id": batch_id, "processed": len(batch_items), "remaining": len([s for s in candidates if _eligible(s, state, include_failed)]), "results": results}


def get_batch_progress(market="IL") -> dict:
    ordered = get_ordered_seed_list(market)
    state = load_batch_state(market)
    _refresh_coverage(state, ordered)
    next_seed = next((s for s in ordered if s["seed_id"] == state.get("next_seed_id")), None)
    total = len(ordered)
    processed = len(state.get("processed_seed_ids", []))
    failed = len(state.get("failed_seed_ids", []))
    coverage_rows = []
    for make, c in state.get("coverage_by_make", {}).items():
        coverage_rows.append({"make": make, **c, "remaining": max(c.get("total", 0) - c.get("processed", 0), 0)})
    coverage_rows = sorted(coverage_rows, key=lambda r: r["make"].lower())
    return {"total_seeds": total, "processed": processed, "remaining": max(total - processed, 0), "failed": failed, "percent_complete": round((processed / total) * 100, 1) if total else 0.0, "current_make": (next_seed or {}).get("make"), "next_seed": next_seed, "coverage_by_make": coverage_rows}


def build_final_export(include_partial=True, include_verified=True, include_conflicts=False, include_unresolved=False) -> dict:
    paths = get_output_paths()
    verified = load_json_list(paths["vehicle_variants_verified"]) if include_verified else []
    partial = load_json_list(paths["vehicle_variants_partial"]) if include_partial else []
    variants = {}
    for row in partial:
        if row.get("variant_id"):
            variants[row["variant_id"]] = row
    for row in verified:
        if row.get("variant_id"):
            variants[row["variant_id"]] = row
    return {
        "schema_version": "vehicle_variants_final_v1",
        "created_at": _now(),
        "counts": {"verified": len(verified), "partial": len(partial), "total_variants": len(variants)},
        "variants": list(variants.values()),
        "sources": load_json_list(paths["vehicle_sources"]),
        "conflicts": load_json_list(paths["vehicle_conflicts"]) if include_conflicts else [],
        "unresolved": load_json_list(paths["unresolved_models"]) if include_unresolved else [],
    }


def rebuild_batch_state_from_outputs(market="IL") -> dict:
    ordered = get_ordered_seed_list(market)
    state = _default_state(market, ordered)
    for run in load_json_list(get_output_paths()["run_history"]):
        sid = run.get("seed_id")
        if not sid:
            sid = build_seed_id(run.get("make"), run.get("model"), run.get("year_start") or 0, run.get("year_end") or 0, run.get("market") or market)
        if sid and sid not in state["processed_seed_ids"]:
            state["processed_seed_ids"].append(sid)
        if run.get("status") == "error" and sid not in state["failed_seed_ids"]:
            state["failed_seed_ids"].append(sid)
    remaining = [s for s in ordered if s["seed_id"] not in state["processed_seed_ids"]]
    state["next_seed_id"] = remaining[0]["seed_id"] if remaining else None
    _refresh_coverage(state, ordered)
    _save_state(state)
    return state

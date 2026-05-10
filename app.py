import json
from pathlib import Path
from typing import Any

import streamlit as st

from agent.batch_runner import (
    build_final_export,
    get_batch_progress,
    get_ordered_seed_list,
    load_batch_state,
    load_local_canonical_resume_package,
    sanitize_repair_queue_state,
    sync_batch_state_from_canonical,
    repair_and_audit_zero_variant_processed_seeds,
    run_next_batch,
)
from agent.rerun_queue_manager import RerunQueueManager
from agent.runner import run_single_model
from core.ingest import get_makes, get_models_by_make
from storage.json_store import get_output_paths

st.set_page_config(page_title="Yeda Vehicle Variant Agent", layout="wide")


def _safe_dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _status_snapshot(market: str = "IL") -> dict:
    canonical = _safe_dict(load_local_canonical_resume_package())
    ordered = get_ordered_seed_list(market)
    batch_state = sanitize_repair_queue_state(_safe_dict(sync_batch_state_from_canonical(market=market)), ordered)
    progress = _safe_dict(get_batch_progress(market=market))
    final_export = _safe_dict(build_final_export())

    needs_retry = list(batch_state.get("needs_retry_seed_ids") or [])
    processed = list(batch_state.get("processed_seed_ids") or [])
    invalid_retry = list(batch_state.get("invalid_needs_retry_seed_ids") or [])
    last_push = _safe_dict(_safe_dict(canonical.get("merge_metadata")).get("last_push_result"))

    rerun_manager = RerunQueueManager(market=market)
    rerun_active = rerun_manager.queue_exists() and rerun_manager.has_pending()
    rerun_progress = rerun_manager.progress_summary() if rerun_manager.queue_exists() else None

    return {
        "canonical": canonical,
        "batch_state": batch_state,
        "progress": progress,
        "total_seeds": len(ordered),
        "processed_count": len(processed),
        "needs_retry": needs_retry,
        "invalid_retry": invalid_retry,
        "current_repair_seed": needs_retry[0] if needs_retry else None,
        "next_normal_seed": batch_state.get("next_seed_id") or _safe_dict(progress.get("next_seed")).get("seed_id"),
        "safe_to_continue": (not rerun_active) and len(needs_retry) == 0,
        "variants_count": len(list(_safe_dict(final_export).get("variants") or [])),
        "last_push": last_push,
        "rerun_active": rerun_active,
        "rerun_progress": rerun_progress,
    }


def _run_next_safe_batch(batch_size: int, market: str, auto_push_per_seed: bool) -> dict:
    repair_res = repair_and_audit_zero_variant_processed_seeds(market=market)
    result = run_next_batch(
        limit=batch_size,
        market=market,
        resume=True,
        include_failed=True,
        auto_push_per_seed=auto_push_per_seed,
        auto_push_canonical=auto_push_per_seed,
    )
    return {"repair_refresh": repair_res, "batch": result}


def _render_json_download(path: Path | None, label: str, file_name: str, missing_message: str) -> None:
    if path and path.exists():
        with open(path, "rb") as f:
            st.download_button(label, f.read(), file_name=file_name, mime="application/json")
    else:
        st.warning(missing_message)


st.title("Yeda Vehicle Variant Agent")
market = st.selectbox("Market", ["IL", "EU", "GLOBAL"], index=0)

main_tab, manual_tab, diag_tab = st.tabs(["Main Run", "Manual Single Model", "Export / Diagnostics"])

with main_tab:
    snap = _status_snapshot(market=market)
    rerun_progress = snap.get("rerun_progress") or {}

    if snap.get("rerun_active"):
        st.subheader("Mode: Rerun Queue")
        total_rerun = int(rerun_progress.get("total_rerun") or 0)
        completed = int(rerun_progress.get("completed_count") or 0)
        pending = int(rerun_progress.get("pending_count") or 0)
        failed_retry = int(rerun_progress.get("failed_retry_count") or 0)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total rerun seeds", total_rerun)
        c2.metric("Completed", completed)
        c3.metric("Pending", pending)
        c4.metric("Failed retry", failed_retry)
        position = int(rerun_progress.get("current_position") or (completed + 1 if pending else completed))
        st.write(
            {
                "current_seed": rerun_progress.get("current_seed"),
                "current_position": f"{position} / {total_rerun}" if total_rerun else None,
                "normal_continuation_paused_at": rerun_progress.get("normal_continuation_seed"),
                "variants": snap["variants_count"],
            }
        )
        st.progress(min(1.0, max(0.0, (rerun_progress.get("progress_percent") or 0) / 100.0)))
    else:
        st.subheader("Mode: Normal Batch")
        c1, c2, c3 = st.columns(3)
        c1.metric("Variants", snap["variants_count"])
        c2.metric("Processed", f"{snap['processed_count']} / {snap['total_seeds']}")
        c3.metric("Safe To Continue", "Yes" if snap["safe_to_continue"] else "No")
        st.write(
            {
                "next_normal_seed": snap["next_normal_seed"],
                "last_github_push_status": snap["last_push"],
            }
        )

    batch_size = st.number_input("Batch size", min_value=1, max_value=20, value=1, step=1)
    auto_push = st.checkbox("Save + push to GitHub after every completed model", value=True)

    if st.button("Run next safe batch", type="primary"):
        out = _run_next_safe_batch(batch_size=batch_size, market=market, auto_push_per_seed=auto_push)
        st.json(out)
        per_seed_canonical = list(_safe_dict(out.get("batch")).get("per_seed_canonical") or [])
        pushed_any = any(
            ((_safe_dict(p.get("canonical_persist")).get("push_result") or {}).get("ok"))
            for p in per_seed_canonical
        )
        st.success(f"Batch complete. pushed_any={bool(pushed_any)}")

with manual_tab:
    makes = get_makes()
    mk = st.selectbox("Make", makes)
    model_seeds = get_models_by_make(mk)
    model_names = sorted({m.model for m in model_seeds})
    mdl = st.selectbox("Model", model_names)
    seed = next((x for x in model_seeds if x.model == mdl), None)
    force_mock = st.checkbox("Force mock mode (recommended for local/testing)", value=True)
    persist_manual = st.checkbox("Persist result to canonical", value=False)

    if st.button("Run single model"):
        r = run_single_model(
            make=mk,
            model=mdl,
            year_start=seed.year_start if seed else None,
            year_end=seed.year_end if seed else None,
            market=market,
            force_mock=force_mock,
            allow_mock_fallback=True,
        )
        st.json(r)
        if persist_manual:
            st.info("Manual persist requested: running one safe batch persist cycle.")
            st.json(_run_next_safe_batch(batch_size=1, market=market, auto_push_per_seed=True))

with diag_tab:
    snap = _status_snapshot(market=market)
    paths = get_output_paths()

    _render_json_download(paths.get("batch_state"), "Download batch_state.json", "batch_state.json", "batch_state.json not found yet")
    _render_json_download(Path("data/canonical/resume_package_canonical.json"), "Download resume_package_canonical.json", "resume_package_canonical.json", "resume_package_canonical.json not found yet")

    st.json(
        {
            "repair_queue": snap["needs_retry"],
            "invalid_needs_retry_seed_ids": snap["invalid_retry"],
            "last_completed_seed_id": snap["batch_state"].get("last_completed_seed_id"),
            "next_seed": snap["next_normal_seed"],
            "last_push_result": snap["last_push"],
        }
    )

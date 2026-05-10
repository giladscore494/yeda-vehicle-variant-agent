import json
from pathlib import Path
from typing import Any

import streamlit as st

from agent.batch_runner import run_next_batch
from agent.problem_queue import (
    compute_problem_repair_state,
    load_canonical as load_problem_queue_canonical,
    delete_problem_queue,
    problem_queue_path,
    refresh_problem_repair_state,
    regenerate_problem_queue,
)
from agent.runner import run_single_model
from core.ingest import get_makes, get_models_by_make
from storage.json_store import get_output_paths

st.set_page_config(page_title="Yeda Vehicle Variant Agent", layout="wide")


def _safe_dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _status_snapshot(market: str = "IL") -> dict:
    """Canonical-first status snapshot.

    Reads exclusively from ``data/canonical/resume_package_canonical.json``
    via ``problem_queue.load_canonical``.  Never reads
    ``data/output/rerun_queue.json``, ``batch_state.json``, or
    ``latest_batch_result.json`` for UI state.
    """
    canonical = _safe_dict(load_problem_queue_canonical())
    prs = compute_problem_repair_state(canonical)
    bs = _safe_dict(canonical.get("batch_state"))
    ace = _safe_dict(canonical.get("accumulated_clean_export"))

    active = bool(prs.get("active"))
    progress = _safe_dict(prs.get("progress"))
    nc = _safe_dict(prs.get("normal_continuation"))

    variants_count = len(list(ace.get("variants") or []))
    processed_count = len(list(bs.get("processed_seed_ids") or []))

    # When problem_queue is active the normal continuation is FROZEN.
    # The "next normal seed" must never advance past the paused point.
    if active:
        next_normal_seed = nc.get("next_seed_id")
    else:
        next_normal_seed = bs.get("next_seed_id")

    needs_retry = [s for s in (bs.get("needs_retry_seed_ids") or []) if isinstance(s, str)]
    invalid_retry = [s for s in (bs.get("invalid_needs_retry_seed_ids") or []) if isinstance(s, str)]
    last_push = _safe_dict(_safe_dict(canonical.get("merge_metadata")).get("last_push_result"))

    return {
        "active_mode": "problem_queue" if active else "normal_batch",
        "canonical": canonical,
        "batch_state": bs,
        "prs": prs,
        # Problem Queue progress axis
        "pq_active": active,
        "pq_total": prs.get("total", 0),
        "pq_completed": progress.get("completed", 0),
        "pq_pending": progress.get("pending", 0),
        "pq_failed_retry": progress.get("failed_retry", 0),
        "pq_current_position": progress.get("current_position", "0 / 0"),
        "pq_current_seed": prs.get("current_seed_id"),
        "pq_last_completed_seed": prs.get("last_completed_seed_id"),
        "pq_normal_paused_at": nc.get("next_seed_id"),
        # Common fields
        "next_normal_seed": next_normal_seed,
        "variants_count": variants_count,
        "processed_count": processed_count,
        # Diag fields
        "needs_retry": needs_retry,
        "invalid_retry": invalid_retry,
        "last_push": last_push,
    }


def _run_next_safe_batch(batch_size: int, market: str, auto_push_per_seed: bool) -> dict:
    # Canonical-only runner: no pre-pass legacy `repair_refresh` audit.
    # All repair work flows through `problem_repair_state` in canonical.
    result = run_next_batch(
        limit=batch_size,
        market=market,
        resume=True,
        include_failed=True,
        auto_push_per_seed=auto_push_per_seed,
        auto_push_canonical=auto_push_per_seed,
    )
    return {"batch": result}


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

    if snap["pq_active"]:
        st.subheader("Mode: Problem Queue")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total", snap["pq_total"])
        c2.metric("Completed", snap["pq_completed"])
        c3.metric("Pending", snap["pq_pending"])
        c4.metric("Failed retry", snap["pq_failed_retry"])
        st.write(
            {
                "active_mode": "problem_queue",
                "Total": snap["pq_total"],
                "Completed": snap["pq_completed"],
                "Pending": snap["pq_pending"],
                "Failed retry": snap["pq_failed_retry"],
                "Current position": snap["pq_current_position"],
                "Current seed": snap["pq_current_seed"],
                "Last completed seed": snap["pq_last_completed_seed"],
                "Normal continuation paused at": snap["pq_normal_paused_at"],
                "Variants count": snap["variants_count"],
                "Processed count": snap["processed_count"],
            }
        )
        pq_pct = (snap["pq_completed"] / snap["pq_total"] * 100.0) if snap["pq_total"] else 0.0
        st.progress(min(1.0, max(0.0, pq_pct / 100.0)))
    else:
        st.subheader("Mode: Normal Batch")
        c1, c2 = st.columns(2)
        c1.metric("Variants", snap["variants_count"])
        c2.metric("Processed", snap["processed_count"])
        st.write(
            {
                "active_mode": "normal_batch",
                "next_normal_seed": snap["next_normal_seed"],
                "last_completed_normal_seed": snap["batch_state"].get("last_completed_seed_id"),
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

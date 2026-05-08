import json
try:
    import streamlit as st
except Exception as exc:
    raise RuntimeError("Streamlit is required to run app.py") from exc
import pandas as pd
from storage.json_store import ensure_output_files, load_outputs_summary, get_output_paths, load_json_list, safe_get
from storage.export import export_verified_for_yeda
from core.ingest import get_makes, get_models_by_make, count_makes, count_models
from agent.runner import run_single_model
from agent.batch_runner import run_next_batch, get_batch_progress, load_batch_state, rebuild_batch_state_from_outputs, build_final_export, build_resume_package, detect_import_file_type, import_progress_json, repair_coverage_until_clean, cleanup_retryable_schema_errors
from tools.gemini_client import GeminiClient

st.set_page_config(page_title="Yeda Vehicle Variant Agent", layout="wide")
ensure_output_files()
client = GeminiClient()
paths = get_output_paths()
summary = load_outputs_summary()


def is_malformed_run_record(record):
    return not isinstance(record, dict) or "status" not in record


st.sidebar.header("Settings")
try:
    cfg = client.get_config_status()
except Exception as exc:
    cfg = {"api_key": "unknown", "api_key_source": "unknown", "google_genai_import_ok": False, "client_ready": False, "import_error": str(exc), "fast_model": None, "strong_model": None, "grounding_supported": None}
st.sidebar.write(f"Gemini API status: {'✅ found' if safe_get(cfg, 'api_key', 'missing') == 'found' else '⚠️ missing'}")
st.sidebar.subheader('Gemini config status')
st.sidebar.write({'api_key': safe_get(cfg, 'api_key', 'missing'), 'api_key_source': safe_get(cfg, 'api_key_source'), 'google_genai_import_ok': safe_get(cfg, 'client_import_ok'), 'client_ready': safe_get(cfg, 'client_ready'), 'import_error': safe_get(cfg, 'import_error'), 'fast_model': safe_get(cfg, 'fast_model'), 'strong_model': safe_get(cfg, 'strong_model')})
use_cache = st.sidebar.checkbox("Use cache", value=True)
force_refresh = st.sidebar.checkbox("Force refresh", value=False)
market = st.sidebar.selectbox("Market", ["IL", "EU", "GLOBAL"], index=0)
model_policy = st.sidebar.selectbox("Model policy", ["Pro only", "Mock only", "Advanced"], index=0)
model_mode='pro_only'
force_mock_ui=False
if model_policy=="Mock only":
    force_mock_ui=True
elif model_policy=="Advanced":
    with st.sidebar.expander("Advanced model settings"):
        adv = st.selectbox("Advanced mode", ["fast", "auto", "strong"], index=1)
        model_mode=adv
st.sidebar.write({"model_policy": model_policy, "model_mode": model_mode, "fast_model": safe_get(cfg, "fast_model"), "strong_model": safe_get(cfg, "strong_model")})
batch_limit = st.sidebar.selectbox("Batch limit", [1, 5, 10, 20], index=1)
make_filter = st.sidebar.selectbox("Make filter", [""] + get_makes())

tabs = st.tabs(["Dashboard", "Run Single Model", "Batch Runner", "Agent Inspector", "Variants", "Conflicts", "Sources", "Export"])

with tabs[0]:
    cols = st.columns(3)
    cols[0].metric("Total makes", count_makes())
    cols[1].metric("Total model seeds", count_models())
    cols[2].metric("Runs count", summary.get("run_history", 0))
    run_history = load_json_list(paths["run_history"])
    last_run = run_history[-1] if run_history else {}
    last_run_status = safe_get(last_run, "status", "n/a")
    last_run_id = safe_get(last_run, "run_id", "n/a")
    st.write(
        {
            "verified variants count": summary.get("vehicle_variants_verified", 0),
            "partial variants count": summary.get("vehicle_variants_partial", 0),
            "conflicts count": summary.get("vehicle_conflicts", 0),
            "sources count": summary.get("vehicle_sources", 0),
            "unresolved count": summary.get("unresolved_models", 0),
            "last run status": last_run_status,
            "last run id": last_run_id,
            "Gemini API key status": "present" if client.has_api_key() else "missing",
        }
    )
    if any(is_malformed_run_record(r) for r in run_history):
        st.warning("Some run history records use an older schema.")
    if not client.has_api_key():
        st.warning("Gemini key missing — Gemini runs will fail unless fallback is enabled.")

with tabs[1]:
    makes = get_makes()
    mk = st.selectbox("Make", makes)
    models = get_models_by_make(mk)
    model_names = sorted({x.model for x in models})
    m = st.selectbox("Model", model_names)
    seed = next((x for x in models if x.model == m), None)
    st.write(f"Parsed year range: {seed.year_start}-{seed.year_end}" if seed else "No seed")
    fm = st.checkbox("Force mock mode", value=force_mock_ui or (not client.has_api_key()))
    allow_fallback = st.checkbox('Allow fallback to mock when Gemini fails', value=True)
    if not client.has_api_key() and not fm:
        st.warning("GEMINI_API_KEY is missing. Run will report Gemini failure and may fallback to mock based on setting.")
    if st.button("Run Agent"):
        try:
            r = run_single_model(
                make=mk,
                model=m,
                year_start=seed.year_start if seed else None,
                year_end=seed.year_end if seed else None,
                market=market,
                force_mock=fm,
                allow_mock_fallback=allow_fallback,
                model_mode=model_mode,
                use_cache=use_cache,
                force_refresh=force_refresh,
            )
        except Exception as exc:
            st.error(f"Run failed: {type(exc).__name__}: {exc}")
            st.exception(exc)
            r = None
        if r is None:
            st.stop()
        st.subheader("Run result")
        trace = r.get('trace', {})
        gemini_attempted = trace.get('gemini_attempted')
        if gemini_attempted is None:
            gemini_attempted = (trace.get('execution_mode') == 'gemini') or ((trace.get('gemini_calls_count') or 0) > 0)
        grounding_requested = trace.get('grounding_requested')
        if grounding_requested is None:
            grounding_requested = (trace.get('grounded_calls_count') or 0) > 0
        st.info(f"Execution mode: {trace.get('execution_mode')}\nModel mode: {trace.get('model_mode')}\nDiscovery model: {trace.get('discovery_model_used')}\nVerification model: {trace.get('verification_model_used')}\nEscalated: {trace.get('escalated_to_strong')}\nEscalation reason: {trace.get('escalation_reason')}\nSources required min: {trace.get('sources_required_min')}\nGemini attempted: {gemini_attempted}\nGrounding requested: {grounding_requested}\nGemini error: {trace.get('gemini_error')}")
        if trace.get('execution_mode') != 'gemini':
            st.warning('This result did not come from a real Gemini run.')
        warnings=[]
        if trace.get('final_decision',{}).get('possible_under_split'): warnings.append('possible_under_split')
        if trace.get('range_collapsed'): warnings.append('range_collapsed')
        omitted=[k for k,v in (trace.get('field_verifications') or {}).items() if v.get('reason','').startswith('Field omitted by model')]
        if omitted: warnings.append(f'fields omitted/defaulted to unknown: {omitted}')
        if warnings: st.warning('Warnings: ' + '; '.join(warnings))
        st.json(r)
        with st.expander("Trace JSON"):
            st.json(r.get("trace", {}))
        with st.expander("Raw Discovery JSON"):
            trace_json = r.get('trace', {})
            st.write('discovery_raw_text')
            if trace_json.get('discovery_raw_text'):
                st.code(trace_json.get('discovery_raw_text'))
            st.write('discovery_parse_error', trace_json.get('discovery_parse_error'))
            st.write('repair_attempted', trace_json.get('repair_attempted', False))
            st.write('repair_success', trace_json.get('repair_success', False))
            st.write('json_salvage_used', trace_json.get('json_salvage_used', False))
            st.write('dropped_incomplete_candidate', trace_json.get('dropped_incomplete_candidate', False))
            if trace_json.get('repair_attempted') and trace_json.get('repair_success'):
                st.info('Malformed Gemini JSON was repaired successfully.')
            if trace_json.get('repair_attempted') and not trace_json.get('repair_success'):
                st.error('Gemini JSON repair attempt failed.')
            st.write('discovery_parsed_top_level_keys', trace_json.get('discovery_parsed_top_level_keys'))
            st.write('candidate_extraction_path', trace_json.get('candidate_extraction_path'))
            st.write('candidate_variants_count', trace_json.get('candidate_variants_count'))
            st.write('raw_text_parsed_in_runner', trace_json.get('raw_text_parsed_in_runner', False))
            st.write('variants_saved_to_verified', trace_json.get('variants_saved_to_verified', 0))
            st.write('variants_saved_to_partial', trace_json.get('variants_saved_to_partial', 0))
            st.write('discovery_parsed_json_debug')
            st.json(trace_json.get('discovery_parsed_json_debug'))
            if trace_json.get('discovery_raw_text') and not trace_json.get('discovery_parsed_json_debug'):
                st.error('Raw Gemini text exists but parsed JSON is empty. Parser bug or invalid JSON.')

with tabs[2]:
    st.caption("No run-all button by design.")
    progress = get_batch_progress(market=market)
    st.progress((progress.get("percent_complete", 0.0))/100.0)
    st.write(f"Processed {progress.get('processed', 0)} / {progress.get('total_seeds', 0)} ({progress.get('percent_complete', 0)}%)")
    next_seed = progress.get("next_seed") or {}
    st.write({"current_make": progress.get("current_make"), "next_seed": next_seed})
    st.dataframe(pd.DataFrame(progress.get("coverage_by_make", [])))
    audit = progress.get("coverage_audit", {})
    st.subheader("Coverage Audit")
    st.write({"last_completed_seed_id": audit.get("last_completed_seed_id"), "scanned_count": audit.get("scanned_count"), "holes_count": audit.get("holes_count")})
    if audit.get("holes_count",0) > 0:
        st.warning("Coverage holes detected. Next batch will repair these before continuing.")
    st.dataframe(pd.DataFrame(audit.get("missing_seeds", [])))

    batch_limit_ui = st.selectbox("Batch limit", [1,3,5,10,20], index=2, key='batch_limit_ui')
    resume_ui = st.checkbox("Resume from last position", value=True)
    include_failed_ui = st.checkbox("Include failed retries", value=False)
    use_cache_ui = st.checkbox("Use cache (batch)", value=True)
    force_refresh_ui = st.checkbox("Force refresh (batch)", value=False)
    make_filter_ui = st.selectbox("Make filter (optional)", [""] + get_makes(), key='batch_make_filter_ui')

    current_seed_text = st.empty()
    batch_progress_bar = st.progress(0.0)
    results_placeholder = st.empty()
    run_rows = []

    def _on_progress(payload):
        idx = payload.get("index", 0); total = max(payload.get("total", 1), 1)
        seed = payload.get("seed", {})
        batch_progress_bar.progress(idx/total)
        current_seed_text.write(f"Running {idx} / {total} — Current: {seed.get('make')} {seed.get('model')} {seed.get('year_start')}–{seed.get('year_end')}")
        results_placeholder.dataframe(pd.DataFrame(run_rows) if run_rows else pd.DataFrame())

    if st.button("Run next batch"):
        result = run_next_batch(limit=batch_limit_ui, market=market, make_filter=make_filter_ui or None, force_refresh=force_refresh_ui, use_cache=use_cache_ui, resume=resume_ui, include_failed=include_failed_ui, progress_callback=_on_progress)
        for item in result.get("results", []):
            seed=item.get("seed", {}); r=item.get("result", {})
            run_rows.append({"make":seed.get("make"), "model":seed.get("model"), "status":r.get("status"), "variants":r.get("variants_created",0)})
        results_placeholder.dataframe(pd.DataFrame(run_rows) if run_rows else pd.DataFrame())
        st.json(result)

    if st.button("Retry failed only"):
        st.json(run_next_batch(limit=batch_limit_ui, market=market, make_filter=make_filter_ui or None, force_refresh=force_refresh_ui, use_cache=use_cache_ui, resume=True, include_failed=True))

    st.subheader("Resume from file")
    uploaded = st.file_uploader("Import resume package", type=["json"], key="resume_upload")
    overwrite_import = st.checkbox("Overwrite local state/files", value=False)
    confirm_import = st.checkbox("I confirm importing this progress file", value=False)
    if uploaded is not None:
        payload = json.loads(uploaded.read().decode("utf-8"))
        detected = detect_import_file_type(payload)
        meta={"detected_file_type": detected, "schema_version": payload.get("schema_version") if isinstance(payload, dict) else None}
        if detected == "resume_package" and isinstance(payload, dict):
            if isinstance(payload.get("final_export"), dict):
                fe=payload.get("final_export", {}); bs=payload.get("batch_state", {})
                meta.update({"variants_found": len(fe.get("variants", []) if isinstance(fe.get("variants", []), list) else []), "processed_seed_ids_found": len(bs.get("processed_seed_ids", []) if isinstance(bs.get("processed_seed_ids", []), list) else []), "makes_count": (fe.get("counts", {}) or {}).get("makes_count"), "models_count": (fe.get("counts", {}) or {}).get("models_count")})
            else:
                acc=payload.get("accumulated_clean_export", {})
                meta.update({"variants_found": len(acc.get("variants", []) if isinstance(acc.get("variants", []), list) else []), "processed_seed_ids_found": len((payload.get("batch_state", {}) or {}).get("processed_seed_ids", []) if isinstance((payload.get("batch_state", {}) or {}).get("processed_seed_ids", []), list) else []), "makes_count": (acc.get("counts", {}) or {}).get("makes_count"), "models_count": (acc.get("counts", {}) or {}).get("models_count")})
        st.write(meta)
        if st.button("Import and rebuild progress") and confirm_import:
            imp=import_progress_json(payload, overwrite=overwrite_import, market=market)
            st.success("Imported accumulated dataset and rebuilt progress.")
            st.json(imp)
            p2=get_batch_progress(market=market)
            st.write({"total_variants": imp.get("imported_variants"), "processed_seeds": p2.get("processed"), "next_seed": p2.get("next_seed"), "holes_count": (p2.get("coverage_audit", {}) or {}).get("holes_count")})

    if st.button("Run hole repair batch"):
        st.json(run_next_batch(limit=batch_limit_ui, market=market, resume=True))

    if st.button("Clean retryable schema errors"):
        st.json(cleanup_retryable_schema_errors(market=market))

    reset_confirm = st.checkbox("Confirm reset batch state", value=False)
    if st.button("Reset batch state") and reset_confirm:
        p = get_output_paths()["run_history"].parents[0] / "batch_state.json"
        if p.exists():
            p.unlink()
        st.success("batch_state.json reset")

    if st.button("Rebuild progress from output files"):
        st.json(rebuild_batch_state_from_outputs(market=market))

with tabs[3]:
    runs = load_json_list(paths["run_history"])
    ids = [safe_get(r, "run_id") for r in runs if safe_get(r, "run_id", None)]
    if ids:
        rid = st.selectbox("run_id", ids)
        run = next((r for r in runs if safe_get(r, "run_id") == rid), {})
        keys = ['input','execution_mode','model_mode','discovery_model_used','verification_model_used','escalated_to_strong','escalation_reason','sources_required_min','gemini_attempted','gemini_error','grounding_requested','grounding_supported','search_queries','sources_found','candidate_variants_count','variants_created','verified_count','partial_count','conflict_count','blocked_fields','field_verifications','range_collapsed','range_collapse_reason','discovery_candidates_preview','final_decision','error']
        st.json({k: run.get(k) for k in keys})

with tabs[4]:
    data = load_json_list(paths["vehicle_variants_verified"]) + load_json_list(paths["vehicle_variants_partial"])
    if data:
        df = pd.DataFrame(data)
        st.dataframe(df)
        idx = st.number_input("Selected variant row", min_value=0, max_value=max(len(data)-1,0), value=0)
        st.json(data[int(idx)])

with tabs[5]:
    c = load_json_list(paths["vehicle_conflicts"])
    st.dataframe(pd.DataFrame(c) if c else pd.DataFrame())

with tabs[6]:
    s = load_json_list(paths["vehicle_sources"])
    st.dataframe(pd.DataFrame(s) if s else pd.DataFrame())

with tabs[7]:
    st.subheader("Clean Final Export")
    include_verified_export = st.checkbox("Include verified", value=True)
    include_partial_export = st.checkbox("Include partial", value=True)
    include_conflicts_export = st.checkbox("Include conflicts", value=False)
    include_unresolved_export = st.checkbox("Include unresolved", value=False)
    merge_trim_options = st.checkbox("Merge trim options", value=True)
    strict_no_mock = st.checkbox("Strict no mock", value=True)
    if "clean_final_payload" not in st.session_state:
        st.session_state.clean_final_payload = None
    if st.button("Build clean final export"):
        st.session_state.clean_final_payload = build_final_export(
            include_partial=include_partial_export,
            include_verified=include_verified_export,
            include_conflicts=include_conflicts_export,
            include_unresolved=include_unresolved_export,
            merge_trim_options=merge_trim_options,
            strict_no_mock=strict_no_mock,
        )
    final_payload = st.session_state.clean_final_payload or build_final_export(
        include_partial=include_partial_export,
        include_verified=include_verified_export,
        include_conflicts=include_conflicts_export,
        include_unresolved=include_unresolved_export,
        merge_trim_options=merge_trim_options,
        strict_no_mock=strict_no_mock,
    )
    q = final_payload.get("quality_gate", {})
    c = final_payload.get("counts", {})
    a = final_payload.get("audit", {})
    accumulation_counts = a.get("accumulation_counts", {}) if isinstance(a, dict) else {}
    imported_count = int(accumulation_counts.get("imported_accumulated_dataset", 0) or 0)
    verified_count = int(accumulation_counts.get("verified_output", 0) or 0)
    partial_count = int(accumulation_counts.get("partial_output", 0) or 0)
    final_count = int(accumulation_counts.get("final_merged_variants", c.get("total_variants", 0)) or 0)
    shrink_prev = int(accumulation_counts.get("shrink_guard_previous_count", imported_count) or 0)
    shrink_new = int(accumulation_counts.get("shrink_guard_new_count", final_count) or 0)
    shrink_detected = shrink_prev > 0 and shrink_new < shrink_prev
    st.write({"quality_score": q.get("score"), "grade": q.get("grade"), "passed": q.get("passed")})
    st.write({"counts": c, "source_id_coverage_ratio": a.get("source_id_coverage_ratio"), "verified_ratio": a.get("verified_ratio"), "partial_ratio": a.get("partial_ratio")})
    st.write(
        {
            "imported_accumulated_dataset_count": imported_count,
            "verified_output_count": verified_count,
            "partial_output_count": partial_count,
            "final_merged_variant_count": final_count,
            "shrink_guard_previous_count": shrink_prev,
            "shrink_guard_new_count": shrink_new,
        }
    )
    if not q.get("passed", False):
        st.error("Final export failed quality gate. Do not use this file in Yeda Rechev.")
    if shrink_detected:
        st.error("Accumulated export shrink detected. Refusing to generate resume package.")
    if c.get("mock_removed", 0) > 0:
        st.error("Mock contaminated records were found and removed.")
    if q.get("blocking_issues"):
        st.write({"blocking_issues": q.get("blocking_issues")})
    if q.get("warnings"):
        st.write({"warnings": q.get("warnings")})

    st.download_button("Full accumulated export", json.dumps(final_payload, ensure_ascii=False, indent=2).encode("utf-8"), file_name="combined_vehicle_variants_final_clean.json")
    st.download_button("Download quality report", json.dumps(final_payload.get("quality_gate", {}), ensure_ascii=False, indent=2).encode("utf-8"), file_name="final_export_quality_report.json")
    resume_pkg = None
    if not shrink_detected:
        try:
            resume_pkg = build_resume_package()
        except ValueError as exc:
            st.error(str(exc))
    st.download_button(
        "Resume package export",
        json.dumps(resume_pkg, ensure_ascii=False, indent=2).encode("utf-8") if resume_pkg is not None else b"",
        file_name="resume_package.json",
        disabled=resume_pkg is None,
    )
    out_dir = get_output_paths()["run_history"].parents[0]
    for name in ["latest_batch_result.json", "batch_state.json", "run_history.json"]:
        path = out_dir / name
        if path.exists():
            st.download_button(f"Download {name}", path.read_bytes(), file_name=name)

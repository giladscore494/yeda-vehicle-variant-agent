import json
import logging
try:
    import streamlit as st
except Exception as exc:
    raise RuntimeError("Streamlit is required to run app.py") from exc
import pandas as pd
from storage.json_store import ensure_output_files, load_outputs_summary, get_output_paths, load_json_list, safe_get
from storage.export import export_verified_for_yeda
from core.ingest import get_makes, get_models_by_make, count_makes, count_models
from agent.runner import run_single_model
_BATCH_RUNNER_IMPORT_ERROR = None
try:
    from agent.batch_runner import run_next_batch, get_batch_progress, load_batch_state, rebuild_batch_state_from_outputs, build_final_export, build_resume_package, detect_import_file_type, import_progress_json, repair_coverage_until_clean, cleanup_retryable_schema_errors, persist_canonical_resume_package, push_local_canonical_to_github, pull_canonical_from_github, canonical_integrity_report, load_local_canonical_resume_package, save_local_canonical_resume_package, diagnose_canonical_github_sync, validate_canonical_update, evaluate_continue_guard
except ImportError as exc:
    _BATCH_RUNNER_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

    def _get_batch_runner_error_result():
        """Return a consistent error payload when batch_runner features are unavailable."""
        return {"ok": False, "error": f"Batch runner module unavailable: {_BATCH_RUNNER_IMPORT_ERROR}"}

    def run_next_batch(*args, **kwargs):
        """Drop-in fallback signature when batch_runner cannot be imported."""
        return {"results": [], **_get_batch_runner_error_result()}

    def get_batch_progress(*args, **kwargs):
        return {
            "processed": 0,
            "total_seeds": 0,
            "percent_complete": 0.0,
            "current_make": None,
            "next_seed": {},
            "coverage_by_make": [],
            "coverage_audit": {"last_completed_seed_id": None, "scanned_count": 0, "holes_count": 0, "missing_seeds": []},
            **_get_batch_runner_error_result(),
        }

    def load_batch_state(*args, **kwargs):
        return _get_batch_runner_error_result()

    def rebuild_batch_state_from_outputs(*args, **kwargs):
        return _get_batch_runner_error_result()

    def build_final_export(*args, **kwargs):
        return {"counts": {}, "audit": {}, "quality_gate": {"passed": False}, "variants": [], **_get_batch_runner_error_result()}

    def build_resume_package(*args, **kwargs):
        return {}

    def detect_import_file_type(*args, **kwargs):
        return "unknown"

    def import_progress_json(*args, **kwargs):
        return {"imported_variants": 0, **_get_batch_runner_error_result()}

    def repair_coverage_until_clean(*args, **kwargs):
        return _get_batch_runner_error_result()

    def cleanup_retryable_schema_errors(*args, **kwargs):
        return _get_batch_runner_error_result()

    def persist_canonical_resume_package(*args, **kwargs):
        return _get_batch_runner_error_result()

    def push_local_canonical_to_github(*args, **kwargs):
        return _get_batch_runner_error_result()

    def pull_canonical_from_github(*args, **kwargs):
        return _get_batch_runner_error_result()

    def canonical_integrity_report(*args, **kwargs):
        return {"sync_status": "unavailable", "guard_issues": [_BATCH_RUNNER_IMPORT_ERROR], **_get_batch_runner_error_result()}

    def load_local_canonical_resume_package(*args, **kwargs):
        return {}

    def save_local_canonical_resume_package(*args, **kwargs):
        return _get_batch_runner_error_result()

    def diagnose_canonical_github_sync(*args, **kwargs):
        return {
            "final_diagnosis": f"Batch runner module unavailable: {_BATCH_RUNNER_IMPORT_ERROR}",
            "single_root_cause": "Batch runner import failed.",
            "recommended_action": "Check Python/package compatibility and dependency installation for batch_runner imports.",
            "safe_to_continue_batch": False,
            "ruled_out": [],
            "checks": {},
            **_get_batch_runner_error_result(),
        }

    def validate_canonical_update(*args, **kwargs):
        return {"passed": False, "issues": [_BATCH_RUNNER_IMPORT_ERROR]}

    def evaluate_continue_guard(*args, **kwargs):
        return {"passed": False, "issues": [_BATCH_RUNNER_IMPORT_ERROR], "coverage_audit": {"holes_count": 1}}
from tools.gemini_client import GeminiClient

st.set_page_config(page_title="Yeda Vehicle Variant Agent", layout="wide")
ensure_output_files()
client = GeminiClient()
paths = get_output_paths()
summary = load_outputs_summary()
logger = logging.getLogger(__name__)


def is_malformed_run_record(record):
    return not isinstance(record, dict) or "status" not in record


def _extract_resume_variants_for_ui(payload):
    if not isinstance(payload, dict):
        return []
    variants = []
    accumulated = payload.get("accumulated_clean_export")
    final_export = payload.get("final_export")
    buckets = [
        accumulated.get("variants") if isinstance(accumulated, dict) else None,
        final_export.get("variants") if isinstance(final_export, dict) else None,
        payload.get("variants"),
        payload.get("verified_variants"),
        payload.get("partial_variants"),
    ]
    for bucket in buckets:
        if isinstance(bucket, list):
            variants.extend([v for v in bucket if isinstance(v, dict)])
    return variants


st.sidebar.header("Settings")
try:
    cfg = client.get_config_status()
except Exception as exc:
    cfg = {"api_key": "unknown", "api_key_source": "unknown", "google_genai_import_ok": False, "client_ready": False, "import_error": str(exc), "fast_model": None, "strong_model": None, "grounding_supported": None}
st.sidebar.write(f"Gemini API status: {'✅ found' if safe_get(cfg, 'api_key', 'missing') == 'found' else '⚠️ missing'}")
st.sidebar.subheader('Gemini config status')
st.sidebar.write({'api_key': safe_get(cfg, 'api_key', 'missing'), 'api_key_source': safe_get(cfg, 'api_key_source'), 'google_genai_import_ok': safe_get(cfg, 'client_import_ok'), 'client_ready': safe_get(cfg, 'client_ready'), 'import_error': safe_get(cfg, 'import_error'), 'fast_model': safe_get(cfg, 'fast_model'), 'strong_model': safe_get(cfg, 'strong_model')})
if _BATCH_RUNNER_IMPORT_ERROR:
    st.sidebar.error(f"Batch/Export features limited due to import error: {_BATCH_RUNNER_IMPORT_ERROR}")
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
    continue_guard = evaluate_continue_guard(market=market)
    local_checkpoint = load_local_canonical_resume_package() or {}
    local_checkpoint_variants = _extract_resume_variants_for_ui(local_checkpoint)
    local_checkpoint_state = local_checkpoint.get("batch_state", {}) if isinstance(local_checkpoint.get("batch_state"), dict) else {}
    checkpoint_source = "Local canonical" if isinstance(local_checkpoint, dict) and local_checkpoint else "Missing"
    if st.session_state.get("last_import_source"):
        checkpoint_source = st.session_state.get("last_import_source")
    total_seed_count = int(continue_guard.get("total_seed_count", 0) or 0)
    local_processed_seed_ids = local_checkpoint_state.get("processed_seed_ids", []) if isinstance(local_checkpoint_state.get("processed_seed_ids"), list) else []
    processed_seed_count = int(continue_guard.get("processed_seed_count", len(local_processed_seed_ids)) or 0)
    stopped_at = local_checkpoint_state.get("last_completed_seed_id")
    next_seed_checkpoint = local_checkpoint_state.get("next_seed_id")
    checkpoint_status = "Ready" if continue_guard.get("passed") else "Blocked"
    st.subheader("Current canonical checkpoint")
    st.write(
        {
            "source": checkpoint_source,
            "variants": len([v for v in local_checkpoint_variants if isinstance(v, dict)]),
            "processed_seeds": f"{processed_seed_count} / {total_seed_count}",
            "stopped_at": stopped_at,
            "next_seed": next_seed_checkpoint,
            "safe_to_continue": bool(continue_guard.get("passed")),
            "status": checkpoint_status,
        }
    )

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
    st.write({"continue_guard_passed": continue_guard.get("passed"), "continue_guard_issues": continue_guard.get("issues", [])})

    batch_limit_ui = st.selectbox("Batch limit", [1,3,5,10,20], index=2, key='batch_limit_ui')
    resume_ui = st.checkbox("Resume from last position", value=True)
    include_failed_ui = st.checkbox("Include failed retries", value=False)
    use_cache_ui = st.checkbox("Use cache (batch)", value=True)
    force_refresh_ui = st.checkbox("Force refresh (batch)", value=False)
    auto_push_canonical_ui = st.checkbox("Enable auto-push after successful batch", value=False)
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

    st.subheader("Resume / Canonical Import")
    uploaded = st.file_uploader("Upload resume package JSON", type=["json"], key="batch_runner_resume_upload")
    overwrite_import = st.checkbox("Overwrite local state/files", value=False, key="batch_runner_overwrite_import")
    if uploaded is not None:
        payload = json.loads(uploaded.read().decode("utf-8"))
        st.session_state["uploaded_resume_package_payload"] = payload
        detected = detect_import_file_type(payload)
        variants_found = len(_extract_resume_variants_for_ui(payload))
        st.write({"detected_file_type": detected, "variants_found": variants_found})
    import_col1, import_col2, import_col3, import_col4, import_col5 = st.columns(5)
    if import_col1.button("Import into Batch Runner"):
        uploaded_payload = st.session_state.get("uploaded_resume_package_payload")
        if isinstance(uploaded_payload, dict):
            imp = import_progress_json(uploaded_payload, overwrite=overwrite_import, market=market)
            st.session_state["last_import_status"] = imp
            st.session_state["last_import_source"] = "uploaded"
            st.json(imp)
        else:
            st.error("No uploaded resume package found in current session.")
    if import_col2.button("Pull canonical from GitHub", key="batch_runner_pull_canonical"):
        pull_res = pull_canonical_from_github()
        st.session_state["last_import_source"] = "GitHub canonical" if pull_res.get("ok") else st.session_state.get("last_import_source")
        if pull_res.get("ok"):
            st.success("Canonical pulled from GitHub.")
        else:
            st.error(pull_res.get("error", "Failed pulling canonical from GitHub."))
    if import_col3.button("Use local canonical"):
        local_pkg = load_local_canonical_resume_package()
        if isinstance(local_pkg, dict):
            imp = import_progress_json(local_pkg, overwrite=False, market=market)
            st.session_state["last_import_status"] = imp
            st.session_state["last_import_source"] = "local canonical"
            st.json(imp)
        else:
            st.error("Local canonical package is missing.")
    if import_col4.button("Run coverage audit before continue"):
        refreshed = get_batch_progress(market=market)
        st.json(refreshed.get("coverage_audit", {}))
    if import_col5.button("Continue next batch"):
        guard_now = evaluate_continue_guard(market=market)
        holes_now = int(((guard_now.get("coverage_audit") or {}).get("holes_count", 0) or 0))
        if not guard_now.get("passed"):
            st.error("Batch start blocked by canonical/batch_state guard.")
            st.json(guard_now)
        elif holes_now > 0:
            st.error("Coverage audit failed. Process holes first.")
            st.json(guard_now.get("coverage_audit", {}))
        else:
            result = run_next_batch(limit=batch_limit_ui, market=market, make_filter=make_filter_ui or None, force_refresh=force_refresh_ui, use_cache=use_cache_ui, resume=True, include_failed=include_failed_ui, progress_callback=_on_progress, auto_push_canonical=auto_push_canonical_ui)
            st.json(result)

    candidate_rows = []
    for source_name in ["local_canonical", "build_final_export", "uploaded_resume", "merged_candidate"]:
        prev_pkg = load_local_canonical_resume_package() or {}
        candidate_pkg = None
        quality_score = None
        if source_name == "local_canonical":
            candidate_pkg = prev_pkg
        elif source_name == "build_final_export":
            fe = build_final_export()
            candidate_pkg = {"schema_version": "resume_package_v1", "accumulated_clean_export": {"variants": fe.get("variants", []), "quality_gate": fe.get("quality_gate"), "audit": fe.get("audit")}, "batch_state": load_batch_state(market), "_candidate_source": source_name}
            quality_score = ((fe.get("quality_gate") or {}).get("score") if isinstance(fe, dict) else None)
        elif source_name == "uploaded_resume":
            uploaded_pkg = st.session_state.get("uploaded_resume_package_payload")
            if isinstance(uploaded_pkg, dict):
                candidate_pkg = dict(uploaded_pkg)
                candidate_pkg["_candidate_source"] = source_name
        elif source_name == "merged_candidate":
            try:
                candidate_pkg = build_resume_package()
                candidate_pkg["_candidate_source"] = source_name
                quality_score = (((candidate_pkg.get("accumulated_clean_export") or {}).get("quality_gate") or {}).get("score"))
            except Exception:
                candidate_pkg = None
        if not isinstance(candidate_pkg, dict):
            continue
        v = validate_canonical_update(prev_pkg, candidate_pkg, market=market)
        candidate_rows.append(
            {
                "source": source_name,
                "variant_count": v.get("candidate_variant_count"),
                "processed_seed_count": v.get("candidate_processed_count"),
                "last_completed_seed_id": v.get("candidate_last_completed_seed_id"),
                "next_seed_id": v.get("candidate_next_seed_id"),
                "quality_score": quality_score,
                "passed": v.get("passed"),
                "issues": ", ".join(v.get("issues", [])),
            }
        )
    st.subheader("Canonical Update Candidate")
    st.dataframe(pd.DataFrame(candidate_rows))

    if st.button("Run next batch"):
        result = run_next_batch(limit=batch_limit_ui, market=market, make_filter=make_filter_ui or None, force_refresh=force_refresh_ui, use_cache=use_cache_ui, resume=resume_ui, include_failed=include_failed_ui, progress_callback=_on_progress, auto_push_canonical=auto_push_canonical_ui)
        for item in result.get("results", []):
            seed=item.get("seed", {}); r=item.get("result", {})
            run_rows.append({"make":seed.get("make"), "model":seed.get("model"), "status":r.get("status"), "variants":r.get("variants_created",0)})
        results_placeholder.dataframe(pd.DataFrame(run_rows) if run_rows else pd.DataFrame())
        st.json(result)
        canonical_persist = result.get("canonical_persist")
        if isinstance(canonical_persist, dict):
            if canonical_persist.get("ok"):
                st.success("Canonical resume package updated and pushed.")
            else:
                st.error("Canonical resume package update blocked")
                st.json(canonical_persist.get("validate_result") or canonical_persist)

    if st.button("Retry failed only"):
        st.json(run_next_batch(limit=batch_limit_ui, market=market, make_filter=make_filter_ui or None, force_refresh=force_refresh_ui, use_cache=use_cache_ui, resume=True, include_failed=True))

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
        try:
            st.session_state.clean_final_payload = build_final_export(
                include_partial=include_partial_export,
                include_verified=include_verified_export,
                include_conflicts=include_conflicts_export,
                include_unresolved=include_unresolved_export,
                merge_trim_options=merge_trim_options,
                strict_no_mock=strict_no_mock,
            )
        except ValueError as exc:
            st.error(str(exc))
    try:
        final_payload = st.session_state.clean_final_payload or build_final_export(
            include_partial=include_partial_export,
            include_verified=include_verified_export,
            include_conflicts=include_conflicts_export,
            include_unresolved=include_unresolved_export,
            merge_trim_options=merge_trim_options,
            strict_no_mock=strict_no_mock,
        )
    except ValueError as exc:
        st.error(str(exc))
        final_payload = {"counts": {}, "audit": {}, "quality_gate": {"passed": False}, "variants": []}
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
    st.subheader("Canonical Resume Package")
    integrity = canonical_integrity_report(market=market)
    st.write(
        {
            "local_canonical_count": integrity.get("local_canonical_count"),
            "github_canonical_count": integrity.get("github_canonical_count"),
            "current_imported_count": integrity.get("current_imported_count"),
            "final_merged_count": integrity.get("final_merged_count"),
            "previous_processed_count": integrity.get("previous_processed_count"),
            "new_processed_count": integrity.get("new_processed_count"),
            "last_completed_seed_id": integrity.get("last_completed_seed_id"),
            "next_seed_id": integrity.get("next_seed_id"),
            "sync_status": integrity.get("sync_status"),
            "last_push_commit_sha": integrity.get("last_push_commit_sha"),
            "shrink_guard_status": integrity.get("shrink_guard_status"),
        }
    )
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    if c1.button("Pull canonical from GitHub"):
        pull_res = pull_canonical_from_github()
        if pull_res.get("ok"):
            st.success("Canonical pulled from GitHub.")
        else:
            st.error(pull_res.get("error", "Failed pulling canonical from GitHub."))
    if c2.button("Save uploaded resume as canonical locally"):
        uploaded_pkg = st.session_state.get("uploaded_resume_package_payload")
        if isinstance(uploaded_pkg, dict):
            save_local_canonical_resume_package(uploaded_pkg)
            st.success("Uploaded package saved as local canonical.")
        else:
            st.error("No uploaded resume package found in current session.")
    if c3.button("Push local canonical to GitHub"):
        push_res = push_local_canonical_to_github(market=market)
        if push_res.get("ok"):
            st.success("Local canonical pushed to GitHub.")
        else:
            st.error("Canonical resume package update blocked")
            st.json(push_res.get("validate_result") or push_res)
    if c4.button("Build merged canonical and push"):
        push_res = persist_canonical_resume_package(push_to_github=True, market=market)
        if push_res.get("ok"):
            st.success("Merged canonical built and pushed to GitHub.")
        else:
            st.error("Canonical resume package update blocked")
            st.json(push_res.get("validate_result") or push_res)
    local_canonical = load_local_canonical_resume_package()
    c5.download_button(
        "Export canonical locally",
        json.dumps(local_canonical or {}, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name="resume_package_canonical.json",
    )
    if c6.button("Run canonical integrity check"):
        if integrity.get("guard_issues"):
            st.error("Canonical resume package update blocked")
            st.json(integrity.get("validate_result") or {"issues": integrity.get("guard_issues")})
        else:
            st.success("Canonical integrity check passed.")
    st.subheader("Candidate update validation")
    st.json(integrity.get("validate_result") or {"issues": integrity.get("guard_issues", [])})
    st.subheader("GitHub connectivity/read diagnostic")
    canonical_diag = diagnose_canonical_github_sync()
    diag_summary = {
        "final_diagnosis": canonical_diag.get("final_diagnosis"),
        "single_root_cause": canonical_diag.get("single_root_cause"),
        "recommended_action": canonical_diag.get("recommended_action"),
        "safe_to_continue_batch": canonical_diag.get("safe_to_continue_batch"),
        "last_update_attempt_failed": canonical_diag.get("last_update_attempt_failed"),
        "last_update_guard_issues": canonical_diag.get("last_update_guard_issues"),
        "last_candidate_variant_count": canonical_diag.get("last_candidate_variant_count"),
        "last_previous_variant_count": canonical_diag.get("last_previous_variant_count"),
        "last_candidate_processed_count": canonical_diag.get("last_candidate_processed_count"),
        "last_previous_processed_count": canonical_diag.get("last_previous_processed_count"),
        "ruled_out_count": len(canonical_diag.get("ruled_out", [])),
    }
    st.json(diag_summary)
    with st.expander("Detailed checks"):
        st.json(canonical_diag)
    if canonical_diag.get("single_root_cause") == "No blocking root cause detected.":
        st.success(canonical_diag.get("final_diagnosis"))
    elif canonical_diag.get("safe_to_continue_batch"):
        st.warning(canonical_diag.get("final_diagnosis"))
    else:
        st.error(canonical_diag.get("final_diagnosis"))
    diag_checks = canonical_diag.get("checks", {})
    cfg_log = diag_checks.get("config", {}) or {}
    secrets_log = diag_checks.get("secrets", {}) or {}
    repo_log_value = "configured" if cfg_log.get("repo_value") else "missing"
    branch_log_value = "configured" if cfg_log.get("branch_value") else "missing"
    canonical_path_value = cfg_log.get("canonical_path_value")
    canonical_path_log_value = "expected" if canonical_path_value == "data/canonical/resume_package_canonical.json" else ("configured" if canonical_path_value else "missing")
    token_present_log_value = "true" if secrets_log.get("token_present") else "false"
    local_exists_log_value = "true" if ((diag_checks.get("local_canonical", {}) or {}).get("local_exists")) else "false"
    local_variant_count_raw = ((diag_checks.get("local_canonical", {}) or {}).get("local_variant_count"))
    local_variant_count_log_value = int(local_variant_count_raw) if isinstance(local_variant_count_raw, int) else 0
    repo_status_raw = ((diag_checks.get("repo_api_auth", {}) or {}).get("repo_status_code"))
    branch_status_raw = ((diag_checks.get("branch_check", {}) or {}).get("branch_status_code"))
    contents_status_raw = ((diag_checks.get("github_contents_check", {}) or {}).get("contents_status_code"))
    allowed_status = {200, 401, 403, 404}
    repo_status_log_value = int(repo_status_raw) if isinstance(repo_status_raw, int) and repo_status_raw in allowed_status else -1
    branch_status_log_value = int(branch_status_raw) if isinstance(branch_status_raw, int) and branch_status_raw in allowed_status else -1
    contents_status_log_value = int(contents_status_raw) if isinstance(contents_status_raw, int) and contents_status_raw in allowed_status else -1
    logger.info(
        "canonical_github_diag repo=%s branch=%s canonical_path=%s token_present=%s local_exists=%s local_variant_count=%s repo_status=%s branch_status=%s contents_status=%s",
        repo_log_value,
        branch_log_value,
        canonical_path_log_value,
        token_present_log_value,
        local_exists_log_value,
        local_variant_count_log_value,
        repo_status_log_value,
        branch_status_log_value,
        contents_status_log_value,
    )
    out_dir = get_output_paths()["run_history"].parents[0]
    for name in ["latest_batch_result.json", "batch_state.json", "run_history.json"]:
        path = out_dir / name
        if path.exists():
            st.download_button(f"Download {name}", path.read_bytes(), file_name=name)

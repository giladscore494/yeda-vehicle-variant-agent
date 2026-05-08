import json
try:
    import streamlit as st
except Exception as exc:
    raise RuntimeError("Streamlit is required to run app.py") from exc
import pandas as pd
from storage.json_store import ensure_output_files, load_outputs_summary, get_output_paths, load_json_list, safe_get
from storage.export import export_verified_for_yeda
from core.ingest import get_makes, get_models_by_make, count_makes, count_models
from agent.runner import run_single_model, run_batch
from tools.gemini_client import GeminiClient

st.set_page_config(page_title="Yeda Vehicle Variant Agent", layout="wide")
ensure_output_files()
client = GeminiClient()
paths = get_output_paths()
summary = load_outputs_summary()


def is_malformed_run_record(record):
    return not isinstance(record, dict) or "status" not in record


st.sidebar.header("Settings")
cfg=client.get_config_status()
st.sidebar.write(f"Gemini API status: {'✅ found' if safe_get(cfg, 'has_api_key', False) else '⚠️ missing'}")
st.sidebar.subheader('Gemini config status')
st.sidebar.write({'api_key': 'found' if safe_get(cfg, 'has_api_key', False) else 'missing', 'api_key_source': safe_get(cfg, 'api_key_source'), 'google_genai_import_ok': safe_get(cfg, 'client_import_ok'), 'client_ready': safe_get(cfg, 'client_ready'), 'import_error': safe_get(cfg, 'import_error'), 'fast_model': safe_get(cfg, 'fast_model'), 'strong_model': safe_get(cfg, 'strong_model')})
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
            st.json(trace_json.get('discovery_parsed_json_debug'))
            if trace_json.get('discovery_raw_text'):
                st.code(trace_json.get('discovery_raw_text'))

with tabs[2]:
    st.caption("No run-all button by design.")
    confirm = st.checkbox("I confirm running >10 models", value=False) if batch_limit > 10 else True
    if st.button("Run Next Batch"):
        if batch_limit > 10 and not confirm:
            st.error("Please confirm before running more than 10 models.")
        else:
            st.json(run_batch(limit=batch_limit, make_filter=make_filter or None, market=market, force_mock=not client.has_api_key(), allow_mock_fallback=True, model_mode=model_mode))

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
    for name, path in paths.items():
        st.download_button(f"Download {name}.json", path.read_bytes(), file_name=path.name)
    yeda = json.dumps(export_verified_for_yeda(), ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button("Download Yeda Rechev Export JSON", yeda, file_name="yeda_rechev_export.json")

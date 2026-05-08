import json
try:
    import streamlit as st
except Exception as exc:
    raise RuntimeError("Streamlit is required to run app.py") from exc
import pandas as pd
from storage.json_store import ensure_output_files, load_outputs_summary, get_output_paths, load_json_list
from storage.export import export_verified_for_yeda
from core.ingest import get_makes, get_models_by_make, count_makes, count_models
from agent.runner import run_single_model, run_batch
from tools.gemini_client import GeminiClient

st.set_page_config(page_title="Yeda Vehicle Variant Agent", layout="wide")
ensure_output_files()
client = GeminiClient()
paths = get_output_paths()
summary = load_outputs_summary()

st.sidebar.header("Settings")
cfg=client.get_config_status()
st.sidebar.write(f"Gemini API status: {'✅ found' if cfg['has_api_key'] else '⚠️ missing'}")
st.sidebar.subheader('Gemini config status')
st.sidebar.write({'api_key': 'found' if cfg['has_api_key'] else 'missing', 'api_key_source': cfg['api_key_source'], 'google_genai_import_ok': cfg['client_import_ok'], 'client_ready': cfg['client_ready'], 'import_error': cfg['import_error'], 'fast_model': cfg['fast_model'], 'strong_model': cfg['strong_model']})
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
st.sidebar.write({"model_policy": model_policy, "model_mode": model_mode, "fast_model": cfg["fast_model"], "strong_model": cfg["strong_model"]})
batch_limit = st.sidebar.selectbox("Batch limit", [1, 5, 10, 20], index=1)
make_filter = st.sidebar.selectbox("Make filter", [""] + get_makes())

show_raw_debug = st.sidebar.checkbox("Show raw Gemini debug", value=True)

tabs = st.tabs(["Dashboard", "Run Single Model", "Batch Runner", "Agent Inspector", "Variants", "Conflicts", "Sources", "Raw Gemini", "Export"])

with tabs[0]:
    cols = st.columns(3)
    cols[0].metric("Total makes", count_makes())
    cols[1].metric("Total model seeds", count_models())
    cols[2].metric("Runs count", summary.get("run_history", 0))
    st.write(
        {
            "verified variants count": summary.get("vehicle_variants_verified", 0),
            "partial variants count": summary.get("vehicle_variants_partial", 0),
            "conflicts count": summary.get("vehicle_conflicts", 0),
            "sources count": summary.get("vehicle_sources", 0),
            "unresolved count": summary.get("unresolved_models", 0),
            "last run status": (load_json_list(paths["run_history"])[-1]["status"] if load_json_list(paths["run_history"]) else "n/a"),
            "Gemini API key status": "present" if client.has_api_key() else "missing",
        }
    )
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
        if show_raw_debug:
            with st.expander("Raw Gemini response"):
                raw_runs = load_json_list(paths["gemini_raw_runs"])
                selected = next((x for x in reversed(raw_runs) if x.get("run_id") == trace.get("run_id")), None)
                if not selected:
                    st.write("No raw response captured.")
                else:
                    st.text_area("Discovery raw text", value=selected.get("discovery_raw_text") or "", height=400)
                    st.json(selected.get("discovery_parsed_json") or {})
                    st.text_area("Verification raw text", value=selected.get("verification_raw_text") or "", height=400)
                    st.json(selected.get("verification_parsed_json") or {})

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
    ids = [r.get("run_id") for r in runs if r.get("run_id")]
    if ids:
        rid = st.selectbox("run_id", ids)
        run = next(r for r in runs if r.get("run_id") == rid)
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
    raw_runs = load_json_list(paths["gemini_raw_runs"])
    raw_candidates = load_json_list(paths["vehicle_candidates_raw"])
    run_ids = [x.get("run_id") for x in raw_runs if x.get("run_id")]
    if run_ids:
        rid = st.selectbox("Select run_id", list(reversed(run_ids)))
        selected_run = next(x for x in raw_runs if x.get("run_id") == rid)
        st.write({
            "make": selected_run.get("make"), "model": selected_run.get("model"), "year_start": selected_run.get("year_start"),
            "year_end": selected_run.get("year_end"), "market": selected_run.get("market"),
            "discovery_model": selected_run.get("discovery_model_used"), "verification_model": selected_run.get("verification_model_used"),
            "discovery_parse_error": selected_run.get("discovery_parse_error"), "verification_parse_error": selected_run.get("verification_parse_error"),
            "discovery_raw_text_available": bool(selected_run.get("discovery_raw_text")), "verification_raw_text_available": bool(selected_run.get("verification_raw_text")),
        })
        st.text_area("Discovery raw response", value=selected_run.get("discovery_raw_text") or "", height=400)
        st.json(selected_run.get("discovery_parsed_json") or {})
        st.text_area("Verification raw response", value=selected_run.get("verification_raw_text") or "", height=400)
        st.json(selected_run.get("verification_parsed_json") or {})
        selected_candidates = next((x for x in raw_candidates if x.get("run_id") == rid), {})
        cand_list = selected_candidates.get("candidate_variants") or []
        if isinstance(cand_list, list) and cand_list:
            st.dataframe(pd.DataFrame(cand_list))
        with st.expander("Full raw candidates JSON"):
            st.json(selected_candidates)
        st.download_button("Download selected discovery raw text", (selected_run.get("discovery_raw_text") or "").encode("utf-8"), file_name=f"{rid}_discovery_raw.txt")
        st.download_button("Download selected verification raw text", (selected_run.get("verification_raw_text") or "").encode("utf-8"), file_name=f"{rid}_verification_raw.txt")
        st.download_button("Download selected raw run JSON", json.dumps(selected_run, ensure_ascii=False, indent=2).encode("utf-8"), file_name=f"{rid}_raw_run.json")
    st.download_button("Download all gemini_raw_runs.json", paths["gemini_raw_runs"].read_bytes(), file_name=paths["gemini_raw_runs"].name)
    st.download_button("Download all vehicle_candidates_raw.json", paths["vehicle_candidates_raw"].read_bytes(), file_name=paths["vehicle_candidates_raw"].name)

with tabs[8]:
    for name, path in paths.items():
        st.download_button(f"Download {name}.json", path.read_bytes(), file_name=path.name)
    yeda = json.dumps(export_verified_for_yeda(), ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button("Download Yeda Rechev Export JSON", yeda, file_name="yeda_rechev_export.json")
    st.download_button("Download Gemini raw runs JSON", paths["gemini_raw_runs"].read_bytes(), file_name=paths["gemini_raw_runs"].name)
    st.download_button("Download raw candidate variants JSON", paths["vehicle_candidates_raw"].read_bytes(), file_name=paths["vehicle_candidates_raw"].name)

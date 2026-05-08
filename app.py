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
st.sidebar.write(f"Gemini API status: {'✅ found' if client.has_api_key() else '⚠️ missing'}")
market = st.sidebar.selectbox("Market", ["IL", "EU", "GLOBAL"], index=0)
batch_limit = st.sidebar.selectbox("Batch limit", [1, 5, 10, 20], index=1)
make_filter = st.sidebar.selectbox("Make filter", [""] + get_makes())

tabs = st.tabs(["Dashboard", "Run Single Model", "Batch Runner", "Agent Inspector", "Variants", "Conflicts", "Sources", "Export"])

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
        st.warning("Gemini key missing — app runs in mock mode automatically.")

with tabs[1]:
    makes = get_makes()
    mk = st.selectbox("Make", makes)
    models = get_models_by_make(mk)
    model_names = sorted({x.model for x in models})
    m = st.selectbox("Model", model_names)
    seed = next((x for x in models if x.model == m), None)
    st.write(f"Parsed year range: {seed.year_start}-{seed.year_end}" if seed else "No seed")
    fm = st.checkbox("Force mock mode", value=not client.has_api_key())
    effective_mock = fm or not client.has_api_key()
    if not client.has_api_key() and not fm:
        st.warning("GEMINI_API_KEY is missing, forcing mock mode for this run.")
    if st.button("Run Agent"):
        r = run_single_model(mk, m, seed.year_start if seed else None, seed.year_end if seed else None, market, effective_mock)
        st.subheader("Run result")
        st.json(r)
        with st.expander("Trace JSON"):
            st.json(r.get("trace", {}))

with tabs[2]:
    st.caption("No run-all button by design.")
    confirm = st.checkbox("I confirm running >10 models", value=False) if batch_limit > 10 else True
    if st.button("Run Next Batch"):
        if batch_limit > 10 and not confirm:
            st.error("Please confirm before running more than 10 models.")
        else:
            st.json(run_batch(batch_limit, make_filter or None, market, force_mock=not client.has_api_key()))

with tabs[3]:
    runs = load_json_list(paths["run_history"])
    ids = [r.get("run_id") for r in runs if r.get("run_id")]
    if ids:
        rid = st.selectbox("run_id", ids)
        run = next(r for r in runs if r.get("run_id") == rid)
        keys = ["input", "search_queries", "sources_found", "facts_extracted", "variants_created", "verified_count", "partial_count", "conflict_count", "blocked_fields", "final_decision", "error"]
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

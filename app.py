import json
import streamlit as st
import pandas as pd
from storage.json_store import ensure_output_files,load_outputs_summary,get_output_paths,load_json_list
from storage.export import export_verified_for_yeda
from core.ingest import get_makes,get_models_by_make,count_makes,count_models
from agent.runner import run_single_model,run_batch
from tools.gemini_client import GeminiClient

st.set_page_config(page_title='Yeda Vehicle Variant Agent',layout='wide')
ensure_output_files(); client=GeminiClient(); paths=get_output_paths(); summary=load_outputs_summary()
st.sidebar.header('Settings'); st.sidebar.write(f"API status: {'✅ found' if client.has_api_key() else '⚠️ missing'}")
market=st.sidebar.selectbox('Market',['IL','EU','GLOBAL']); batch_limit=st.sidebar.selectbox('Batch limit',[1,3,5,10]); make_filter=st.sidebar.selectbox('Make filter',['']+get_makes())
tabs=st.tabs(['Dashboard','Run Single Model','Batch Runner','Agent Inspector','Variants','Conflicts','Sources','Export'])
with tabs[0]:
    st.metric('Total makes',count_makes()); st.metric('Total model seeds',count_models());
    for k,v in summary.items(): st.write(f'{k}: {v}')
    if not client.has_api_key(): st.warning('Gemini key missing — app is running in mock/demo mode.')
with tabs[1]:
    makes=get_makes(); mk=st.selectbox('Make',makes)
    models=get_models_by_make(mk); m=st.selectbox('Model',[x.model for x in models])
    seed=next((x for x in models if x.model==m),None); st.write(f'Parsed year range: {seed.year_start}-{seed.year_end}')
    fm=st.checkbox('Force mock mode',value=not client.has_api_key())
    if st.button('Run Agent'):
        r=run_single_model(mk,m,seed.year_start,seed.year_end,market,fm); st.json(r)
with tabs[2]:
    if st.button('Run Next Batch'):
        st.json(run_batch(batch_limit,make_filter or None,market,force_mock=not client.has_api_key()))
with tabs[3]:
    runs=load_json_list(paths['run_history']); ids=[r['run_id'] for r in runs]
    if ids: st.json(next(r for r in runs if r['run_id']==st.selectbox('run_id',ids)))
with tabs[4]:
    data=load_json_list(paths['vehicle_variants_verified'])+load_json_list(paths['vehicle_variants_partial'])
    if data: st.dataframe(pd.DataFrame(data))
with tabs[5]:
    c=load_json_list(paths['vehicle_conflicts']); st.dataframe(pd.DataFrame(c) if c else pd.DataFrame())
with tabs[6]:
    s=load_json_list(paths['vehicle_sources']); st.dataframe(pd.DataFrame(s) if s else pd.DataFrame())
with tabs[7]:
    for name,path in paths.items():
        b=path.read_bytes(); st.download_button(f'Download {name}.json',b,file_name=path.name)
    yeda=json.dumps(export_verified_for_yeda(),ensure_ascii=False,indent=2).encode('utf-8')
    st.download_button('Download Yeda Rechev lightweight export JSON',yeda,file_name='yeda_export.json')

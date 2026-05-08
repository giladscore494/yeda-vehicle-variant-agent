import json
from pathlib import Path

def project_root()->Path: return Path(__file__).resolve().parents[1]
def ensure_data_dirs():
    for p in ['data/input','data/output','data/cache']: (project_root()/p).mkdir(parents=True,exist_ok=True)
def ensure_output_files():
    ensure_data_dirs();
    for p in get_output_paths().values():
        if not p.exists(): p.write_text('[]\n',encoding='utf-8')
    for p in [project_root()/'data/cache/search_cache.json',project_root()/'data/cache/extraction_cache.json']:
        if not p.exists(): p.write_text('{}\n',encoding='utf-8')
def load_json_list(path:Path)->list:
    if not path.exists(): return []
    d=json.loads(path.read_text(encoding='utf-8') or '[]')
    return d if isinstance(d,list) else []
def load_json_object(path:Path)->dict:
    if not path.exists(): return {}
    d=json.loads(path.read_text(encoding='utf-8') or '{}')
    return d if isinstance(d,dict) else {}
def save_json(path:Path,data): path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
def append_unique(path:Path,records:list[dict],key_field:str):
    cur=load_json_list(path); idx={r.get(key_field):r for r in cur if isinstance(r,dict) and r.get(key_field)}
    for r in records:
        k=r.get(key_field) or (r.get('url') if key_field=='source_id' else None)
        if k: idx[k]=r
    save_json(path,list(idx.values()))
def get_output_paths():
    b=project_root()/'data/output'
    return {k:b/f'{k}.json' for k in ['vehicle_variants_verified','vehicle_variants_partial','vehicle_conflicts','vehicle_sources','unresolved_models','run_history','gemini_raw_runs','vehicle_candidates_raw']}
def load_outputs_summary()->dict:
    ensure_output_files(); paths=get_output_paths();
    return {k:len(load_json_list(v)) for k,v in paths.items()}
def add_run_history(trace:dict): append_unique(get_output_paths()['run_history'],[trace],'run_id')

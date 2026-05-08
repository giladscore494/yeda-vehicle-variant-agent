from pathlib import Path
from storage.json_store import get_output_paths, load_json_list

def export_verified_for_yeda()->list[dict]:
    out=[]
    for r in load_json_list(get_output_paths()['vehicle_variants_verified']):
        out.append({
            'variant_id':r.get('variant_id'),'make':r.get('make'),'model':r.get('model'),'aliases':r.get('aliases',[]),'year_start':r.get('year_start'),'year_end':r.get('year_end'),'market':r.get('market'),'generation':r.get('generation'),'body_type':(r.get('body_type') or {}).get('value'),'seats':(r.get('seats') or {}).get('value'),'engine':(r.get('engine') or {}).get('value'),'transmission':(r.get('transmission') or {}).get('value'),'fuel_type':(r.get('fuel_type') or {}).get('value'),'drivetrain':(r.get('drivetrain') or {}).get('value'),'confidence':r.get('confidence'),'verification_status':r.get('verification_status'),'sources_count':r.get('sources_count')
        })
    return out

def export_file_bytes(path): return Path(path).read_bytes()

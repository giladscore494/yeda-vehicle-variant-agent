from storage.json_store import get_output_paths, load_json_list
from core.schemas import VehicleVariant, ConflictRecord, EvidenceSource, RunTrace
errs=0; p=get_output_paths()
for r in load_json_list(p['vehicle_variants_verified'])+load_json_list(p['vehicle_variants_partial']):
    try: VehicleVariant(**r)
    except Exception: errs+=1
for r in load_json_list(p['vehicle_conflicts']):
    try: ConflictRecord(**r)
    except Exception: errs+=1
for r in load_json_list(p['vehicle_sources']):
    try: EvidenceSource(**r)
    except Exception: errs+=1
for r in load_json_list(p['run_history']):
    try: RunTrace(**r)
    except Exception: errs+=1
print({'errors':errs})
raise SystemExit(1 if errs else 0)

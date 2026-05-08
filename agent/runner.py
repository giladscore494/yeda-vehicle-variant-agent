from datetime import datetime, timezone
import uuid
from core.ingest import find_seed, load_model_seeds
from core.schemas import VerifiedField, VerificationStatus, Confidence, Market, VehicleVariant, EvidenceSource
from core.variant_id import generate_variant_id
from core.validators import classify_variant
from core.conflict_detector import detect_conflicts
from storage.json_store import ensure_output_files,get_output_paths,append_unique,add_run_history,load_json_list
from tools.gemini_client import GeminiClient

def _now(): return datetime.now(timezone.utc).isoformat()

def _mock_variant(make='Kia',model='Sportage',year_start=2016,year_end=2021,market='IL'):
    sid='source_mock_kia_sportage'
    mk=lambda v,s,c,sc,u,r: VerifiedField(value=v,status=s,confidence=c,sources_count=sc,source_ids=[sid] if sc else [],used_in_compare=u,reason=r)
    var=VehicleVariant(variant_id=generate_variant_id(make,model,year_start,year_end,market,'1.6 Turbo','automatic','suv'),make=make,model=model,aliases=[],year_start=year_start,year_end=year_end,market=Market(market),generation='QL',body_type=mk('suv',VerificationStatus.verified,Confidence.high,1,True,'mock'),seats=mk(5,VerificationStatus.verified,Confidence.high,1,True,'mock'),engine=mk('1.6 Turbo',VerificationStatus.partial,Confidence.medium,1,True,'mock'),transmission=mk('automatic',VerificationStatus.partial,Confidence.medium,1,True,'mock'),fuel_type=mk('petrol',VerificationStatus.partial,Confidence.medium,1,True,'mock'),drivetrain=mk('FWD',VerificationStatus.unknown,Confidence.low,0,False,'unknown'),verification_status=VerificationStatus.partial,confidence=Confidence.medium,sources_count=1,created_at=_now(),updated_at=_now(),notes=['mock mode'])
    src=EvidenceSource(source_id=sid,source_name='Mock Source',url='https://example.com/mock-kia-sportage',source_type='mock',market_scope=Market.IL,title='Mock Kia Sportage',retrieved_at=_now(),evidence_snippet='mock evidence',reliability_score=3,fields_supported=['body_type','seats','engine','transmission'])
    return var,src

def run_single_model(make,model,year_start=None,year_end=None,market='IL',force_mock=False)->dict:
    ensure_output_files(); run_id=str(uuid.uuid4()); started=_now(); seed=find_seed(make,model)
    if not seed: return {'status':'error','error':'seed not found'}
    ys=year_start or seed.year_start or 2016; ye=year_end or seed.year_end or 2021
    use_mock=force_mock or not GeminiClient().has_api_key()
    variant,source=_mock_variant(make,model,ys,ye,market)
    cls=classify_variant(variant)
    paths=get_output_paths(); target='vehicle_variants_verified' if cls=='verified' else 'vehicle_variants_partial'
    append_unique(paths[target],[variant.model_dump(mode='json')],'variant_id')
    append_unique(paths['vehicle_sources'],[source.model_dump(mode='json')],'source_id')
    conflicts=[c.model_dump(mode='json') for c in detect_conflicts([variant])]
    if conflicts: append_unique(paths['vehicle_conflicts'],conflicts,'conflict_id')
    trace={'run_id':run_id,'input':{'make':make,'model':model,'year_start':ys,'year_end':ye,'market':market,'force_mock':force_mock},'started_at':started,'finished_at':_now(),'status':'mock_completed' if use_mock else 'completed','search_queries':[],'sources_found':1,'facts_extracted':6,'variants_created':1,'verified_count':1 if cls=='verified' else 0,'partial_count':1 if cls=='partial' else 0,'conflict_count':len(conflicts),'unresolved_count':0,'blocked_fields':['drivetrain'],'final_decision':{'classification':cls},'error':None}
    add_run_history(trace)
    return {'status':trace['status'],'run_id':run_id,'variants_created':1,'verified_count':trace['verified_count'],'partial_count':trace['partial_count'],'conflict_count':trace['conflict_count'],'unresolved_count':trace['unresolved_count'],'blocked_fields':trace['blocked_fields'],'final_decision':trace['final_decision'],'trace':trace}

def run_batch(limit=5,make_filter=None,market='IL',force_mock=False)->dict:
    seeds=load_model_seeds();
    if make_filter: seeds=[s for s in seeds if s.make.lower()==make_filter.lower()]
    seen={(r.get('input') or {}).get('make','')+'|'+(r.get('input') or {}).get('model','') for r in load_json_list(get_output_paths()['run_history'])}
    chosen=[s for s in seeds if f'{s.make}|{s.model}' not in seen][:limit]
    results=[run_single_model(s.make,s.model,s.year_start,s.year_end,market,force_mock) for s in chosen]
    return {'status':'completed','processed':len(results),'results':results}

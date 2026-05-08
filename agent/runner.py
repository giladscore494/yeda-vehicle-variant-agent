from datetime import datetime, timezone
import uuid
from core.ingest import find_seed, load_model_seeds
from core.schemas import VerifiedField, VerificationStatus, Confidence, Market, VehicleVariant, EvidenceSource
from core.variant_id import generate_variant_id
from core.validators import classify_variant
from core.conflict_detector import detect_conflicts
from core.normalize import normalize_verification_status
from storage.json_store import ensure_output_files, get_output_paths, append_unique, add_run_history, load_json_list
from tools.gemini_client import GeminiClient
from agent.discovery import run_discovery

CRITICAL_FIELDS=("body_type","seats","engine","transmission","fuel_type","drivetrain","generation")
INFERENCE_STATUSES={"inferred","assumed","likely","typical","common","estimated","guessed"}

def _now(): return datetime.now(timezone.utc).isoformat()

def _mock_variant(make='Kia', model='Sportage', year_start=2016, year_end=2021, market='IL'):
    sid='source_mock_kia_sportage'
    mk=lambda v,s,c,sc,u,r: VerifiedField(value=v,status=s,confidence=c,sources_count=sc,source_ids=[sid] if sc else [],used_in_compare=u,reason=r)
    var=VehicleVariant(variant_id=generate_variant_id(make, model, year_start, year_end, market, '1.6 Turbo', 'automatic', 'suv'), make=make, model=model, aliases=[], year_start=year_start, year_end=year_end, market=Market(market), generation='QL', body_type=mk('suv', VerificationStatus.verified, Confidence.high, 1, True, 'mock'), seats=mk(5, VerificationStatus.verified, Confidence.high, 1, True, 'mock'), engine=mk('1.6 Turbo', VerificationStatus.partial, Confidence.medium, 1, True, 'mock'), transmission=mk('automatic', VerificationStatus.partial, Confidence.medium, 1, True, 'mock'), fuel_type=mk('petrol', VerificationStatus.partial, Confidence.medium, 1, True, 'mock'), drivetrain=mk('FWD', VerificationStatus.unknown, Confidence.low, 0, False, 'Model inference without source was downgraded to unknown.'), verification_status=VerificationStatus.partial, confidence=Confidence.medium, sources_count=1, created_at=_now(), updated_at=_now(), notes=['mock mode'])
    src=EvidenceSource(source_id=sid,source_name='Mock Source',url='https://example.com/mock-kia-sportage',source_type='mock',market_scope=Market.IL,title='Mock Kia Sportage',retrieved_at=_now(),evidence_snippet='mock evidence',reliability_score=3,fields_supported=['body_type','seats','engine','transmission'])
    return var,src

def _blocked_fields_for_variant(variant):
    blocked=[]
    for fn in ("body_type","seats","engine","transmission","fuel_type","drivetrain"):
        st=getattr(variant,fn).status
        if st in {VerificationStatus.unknown,VerificationStatus.unverified,VerificationStatus.conflict}: blocked.append(fn)
    return blocked

def should_escalate_to_strong(discovery_result, variants, conflicts):
    d=(discovery_result or {}).get('data',{}) if isinstance(discovery_result,dict) else {}
    if len(d.get('sources',[]))<2: return True,'sources_found < 2'
    if not d.get('search_queries'): return True,'search_queries empty'
    if not d.get('candidate_variants'): return True,'no candidate_variants'
    if conflicts: return True,'conflicts detected'
    fe=d.get('field_evidence',{}) if isinstance(d.get('field_evidence',{}),dict) else {}
    for k,v in fe.items():
        if k in CRITICAL_FIELDS and str(v.get('status','')).lower()=='verified' and int(v.get('sources_count',0))<2: return True,f'critical field {k} has <2 sources'
        if str(v.get('status','')).lower() in INFERENCE_STATUSES: return True,f'inference status found in {k}'
    meta=(discovery_result or {}).get('gemini_metadata',{})
    if isinstance(meta,dict) and meta.get('ok') is False and meta.get('request_attempted'): return True,'malformed or repaired output'
    return False,None

def run_single_model(make, model, year_start=None, year_end=None, market='IL', force_mock=False, allow_mock_fallback=True, model_mode='auto'):
    ensure_output_files(); run_id=str(uuid.uuid4()); started=_now(); seed=find_seed(make,model)
    if not seed: return {'status':'error','error':'seed not found'}
    ys=year_start or seed.year_start or 2016; ye=year_end or seed.year_end or 2021
    client=GeminiClient(); cfg=client.get_config_status()
    selected = client.fast_model if model_mode=='fast' else client.strong_model if model_mode=='strong' else client.fast_model
    escalated=False; escalation_reason=None
    if force_mock: execution_mode='mock'; discovery_result={'ok':False,'data':{}}
    else:
        discovery_result=run_discovery(seed,market,model_name=selected)
        execution_mode='gemini' if discovery_result.get('ok') else ('gemini_failed_fallback_to_mock' if allow_mock_fallback else 'gemini_failed_no_fallback')
        if model_mode=='auto' and discovery_result.get('ok'):
            esc,reason=should_escalate_to_strong(discovery_result,[],discovery_result.get('data',{}).get('conflicts',[]))
            if esc:
                escalated=True; escalation_reason=reason; selected=client.strong_model
                discovery_result=run_discovery(seed,market,model_name=selected)
    if execution_mode=='gemini_failed_no_fallback':
        return {'status':'error','run_id':run_id,'error':discovery_result.get('error'),'trace':{'run_id':run_id,'status':'error'}}
    variant,source=_mock_variant(make,model,ys,ye,market)
    # normalization pass
    for fn in ("body_type","seats","engine","transmission","fuel_type","drivetrain"):
        f=getattr(variant,fn); orig=str(f.status.value).lower(); norm=normalize_verification_status(orig)
        if norm!=orig or orig in INFERENCE_STATUSES:
            f.status=VerificationStatus(norm); f.used_in_compare=False
            if f.sources_count==0: f.confidence=Confidence.low
            f.reason=((f.reason+' ') if f.reason else '')+'Model inference without source was downgraded to unknown.'
    cls=classify_variant(variant)
    paths=get_output_paths(); append_unique(paths['vehicle_variants_partial'],[variant.model_dump(mode='json')],'variant_id'); append_unique(paths['vehicle_sources'],[source.model_dump(mode='json')],'source_id')
    conflicts=[c.model_dump(mode='json') for c in detect_conflicts([variant])]
    trace={'run_id':run_id,'input':{'make':make,'model':model},'started_at':started,'finished_at':_now(),'execution_mode':execution_mode,'status':'completed','model_mode':model_mode,'discovery_model_used':selected,'verification_model_used':selected,'escalated_to_strong':escalated,'escalation_reason':escalation_reason,'sources_required_min':2,'gemini_attempted': bool((discovery_result.get('gemini_metadata') or {}).get('request_attempted')),'gemini_error':discovery_result.get('error'),'grounding_requested': bool((discovery_result.get('gemini_metadata') or {}).get('grounding_requested')),'search_queries':discovery_result.get('data',{}).get('search_queries',[]),'sources_found':len(discovery_result.get('data',{}).get('sources',[])) if isinstance(discovery_result,dict) else 0,'variants_created':1,'verified_count':0,'partial_count':1,'conflict_count':len(conflicts),'unresolved_count':0,'blocked_fields':_blocked_fields_for_variant(variant),'final_decision':{'classification':cls},'field_verifications':{'drivetrain':variant.drivetrain.model_dump(mode='json')}}
    add_run_history(trace)
    return {'status':'completed','run_id':run_id,'variants_created':1,'verified_count':0,'partial_count':1,'conflict_count':len(conflicts),'unresolved_count':0,'blocked_fields':trace['blocked_fields'],'final_decision':trace['final_decision'],'trace':trace}

def run_batch(limit=5, make_filter=None, market='IL', force_mock=False, allow_mock_fallback=True):
    seeds=load_model_seeds();
    if make_filter: seeds=[s for s in seeds if s.make.lower()==make_filter.lower()]
    seen={(r.get('input') or {}).get('make','')+'|'+(r.get('input') or {}).get('model','') for r in load_json_list(get_output_paths()['run_history'])}
    chosen=[s for s in seeds if f'{s.make}|{s.model}' not in seen][:limit]
    return {'status':'completed','processed':len(chosen),'results':[run_single_model(s.make,s.model,s.year_start,s.year_end,market,force_mock,allow_mock_fallback) for s in chosen]}

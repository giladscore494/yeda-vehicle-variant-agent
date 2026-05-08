from tools.gemini_client import GeminiClient
from core.schemas import VerificationStatus, Confidence

CRITICAL_FIELDS=("body_type","seats","engine","transmission","fuel_type","drivetrain","generation","year_start","year_end")

def _unknown_default(reason='Field omitted by model; defaulted to unknown.'):
    return {'value':None,'status':VerificationStatus.unknown.value,'confidence':Confidence.low.value,'sources_count':0,'source_ids':[],'used_in_compare':False,'reason':reason}

def _normalize_fields(resp):
    fv = resp.get('field_verifications') if isinstance(resp,dict) else None
    fv = fv if isinstance(fv,dict) else {}
    for field in CRITICAL_FIELDS:
        entry = fv.get(field)
        if not isinstance(entry,dict):
            fv[field]=_unknown_default()
            continue
        entry.setdefault('value',None)
        entry.setdefault('status',VerificationStatus.unknown.value)
        entry.setdefault('confidence',Confidence.low.value)
        entry.setdefault('sources_count',0)
        entry.setdefault('source_ids',[])
        entry.setdefault('used_in_compare',False)
        if entry.get('status')=='unknown' and 'reason' not in entry:
            entry['reason']='No direct source for this field.'
    resp['field_verifications']=fv
    return resp

def verify_candidate(candidate,sources,model_name=None)->dict:
    r=GeminiClient().generate_json('verify',strong=(model_name is None),model_override=model_name)
    if not r.get('ok'):
        return {'field_verifications':{k:_unknown_default('Gemini unavailable') for k in CRITICAL_FIELDS},'overall_status':'unverified','overall_confidence':'low','blocked_fields':list(CRITICAL_FIELDS),'notes':[r.get('error')]}
    return _normalize_fields(r)

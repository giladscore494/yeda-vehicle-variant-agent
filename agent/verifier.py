from tools.gemini_client import GeminiClient
from core.schemas import VerificationStatus, Confidence

def verify_candidate(candidate,sources)->dict:
    r=GeminiClient().generate_json('verify',strong=True)
    if not r.get('ok'):
        unk={'value':None,'status':VerificationStatus.unknown.value,'confidence':Confidence.low.value,'sources_count':0,'source_ids':[],'used_in_compare':False,'reason':'Gemini unavailable'}
        return {'field_verifications':{k:unk for k in ['body_type','seats','engine','transmission','fuel_type','drivetrain']},'overall_status':'unverified','overall_confidence':'low','blocked_fields':['body_type','seats','engine','transmission','fuel_type','drivetrain'],'notes':[r.get('error')]}
    return r

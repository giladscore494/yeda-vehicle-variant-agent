from agent.prompts import build_discovery_prompt
from tools.gemini_client import GeminiClient

def run_discovery(seed, market='IL', model_name=None) -> dict:
    prompt = build_discovery_prompt(seed, market)
    res = GeminiClient().grounded_generate_json(prompt=prompt, model_override=model_name)
    if not isinstance(res, dict):
        return {'ok': False,'data': None,'error': f'Gemini client returned non-dict: {type(res).__name__}','gemini_metadata': {'ok': False,'provider': 'gemini','model': None,'grounding_requested': True,'request_attempted': False,'error': 'non-dict gemini response','raw_text': None}}
    if not res.get('ok'):
        return {'ok': False,'data': None,'error': res.get('error'),'gemini_metadata': res}
    payload = res.get('data') if isinstance(res.get('data'), dict) else {}
    data = {'search_queries': payload.get('search_queries') if isinstance(payload.get('search_queries'), list) else [],'sources': payload.get('sources') if isinstance(payload.get('sources'), list) else [],'candidate_variants': payload.get('candidate_variants') if isinstance(payload.get('candidate_variants'), list) else [],'conflicts': payload.get('conflicts') if isinstance(payload.get('conflicts'), list) else [],'unresolved': bool(payload.get('unresolved', False)),'unresolved_reason': payload.get('unresolved_reason'),'field_evidence': payload.get('field_evidence', {})}
    return {'ok': True,'data': data,'gemini_metadata': {'ok': bool(res.get('ok')),'provider': res.get('provider', 'gemini'),'model': res.get('model'),'grounding_requested': bool(res.get('grounding_requested', True)),'request_attempted': bool(res.get('request_attempted', True)),'error': res.get('error'),'raw_text': res.get('raw_text')}}

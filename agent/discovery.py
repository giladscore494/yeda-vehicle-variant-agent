from agent.prompts import build_discovery_prompt
from tools.grounding_search import run_grounded_query

def run_discovery(seed,market='IL')->dict:
    prompt=build_discovery_prompt(seed,market)
    res=run_grounded_query(seed.make,seed.model,seed.year_start,seed.year_end,market,prompt)
    if not res.get('ok'):
        return {'ok':False,'error':res.get('error'),'search_queries':[],'sources':[],'candidate_variants':[],'conflicts':[],'unresolved':True,'unresolved_reason':res.get('error')}
    return res

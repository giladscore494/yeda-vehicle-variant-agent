from tools.cache import cache_get, cache_set
from tools.gemini_client import GeminiClient

def run_grounded_query(make,model,year_start,year_end,market,prompt):
    key=f'{make}|{model}|{year_start}|{year_end}|{market}'
    c=cache_get('search_cache',key)
    if c: return c
    r=GeminiClient().grounded_generate_json(prompt)
    cache_set('search_cache',key,r)
    return r

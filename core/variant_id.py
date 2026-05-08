import re, unicodedata

def _s(v):
    t=unicodedata.normalize('NFKD',str(v or '')).encode('ascii','ignore').decode().lower()
    t=re.sub(r'[^a-z0-9]+','_',t)
    return re.sub(r'_+','_',t).strip('_')

def generate_variant_id(make,model,year_start,year_end,market,engine=None,transmission=None,body_type=None)->str:
    return _s('_'.join([make,model,str(year_start),str(year_end),str(market),engine or 'unknown_engine',transmission or 'unknown_transmission',body_type or 'unknown_body']))

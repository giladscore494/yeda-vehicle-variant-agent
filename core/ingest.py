from __future__ import annotations
import re
from pathlib import Path
from core.schemas import VehicleModelSeed

DATA_PATH=Path('data/input/car_models_dict.py')

def load_car_dictionary(path:Path|None=None)->dict:
    p=path or DATA_PATH
    ns={}
    exec(compile(p.read_text(encoding='utf-8'),str(p),'exec'),{},ns)
    candidates=[v for v in ns.values() if isinstance(v,dict) and all(isinstance(x,list) for x in v.values())]
    if 'israeli_car_market_full_compilation' in ns and isinstance(ns['israeli_car_market_full_compilation'],dict):
        return ns['israeli_car_market_full_compilation']
    if not candidates: raise ValueError('No dictionary found')
    return max(candidates,key=lambda d:sum(len(v) for v in d.values()))

def parse_model_entry(make:str, model_raw:str)->list[VehicleModelSeed]:
    m=re.match(r'^(.*?)\s*\(([^)]*)\)\s*$',model_raw)
    name=model_raw; ranges=[(None,None)]
    if m:
        name=m.group(1).strip(); ranges=[]
        for part in [x.strip() for x in m.group(2).split(',')]:
            yr=re.match(r'^(\d{4})\s*-\s*(\d{4})$',part)
            ranges.append((int(yr.group(1)),int(yr.group(2))) if yr else (None,None))
    bits=[b.strip() for b in name.split('/')]
    model=bits[0]; aliases=bits[1:]
    return [VehicleModelSeed(make=make,model_raw=model_raw,model=model,aliases=aliases,year_start=a,year_end=b) for a,b in ranges]

def load_model_seeds()->list[VehicleModelSeed]:
    d=load_car_dictionary(); out=[]
    for make,models in d.items():
        for mr in models: out.extend(parse_model_entry(make,mr))
    return out

def count_makes()->int: return len(load_car_dictionary())
def count_models()->int: return len(load_model_seeds())
def get_makes()->list[str]: return sorted(load_car_dictionary().keys())
def get_models_by_make(make:str)->list[VehicleModelSeed]: return [s for s in load_model_seeds() if s.make.lower()==make.lower()]
def find_seed(make:str,model:str)->VehicleModelSeed|None:
    for s in get_models_by_make(make):
        if s.model.lower()==model.lower(): return s
    return None

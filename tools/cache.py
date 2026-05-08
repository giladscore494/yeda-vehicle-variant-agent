import hashlib, json
from pathlib import Path
from storage.json_store import project_root, load_json_object, save_json

def _path(name): return project_root()/f'data/cache/{name}.json'
def cache_get(name,key): return load_json_object(_path(name)).get(hashlib.sha1(key.encode()).hexdigest())
def cache_set(name,key,value):
    p=_path(name); d=load_json_object(p); d[hashlib.sha1(key.encode()).hexdigest()]=value; save_json(p,d)

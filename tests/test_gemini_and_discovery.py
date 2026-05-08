from types import SimpleNamespace
from agent import discovery
from tools.gemini_client import GeminiClient

def test_generate_json_missing_key_returns_not_attempted(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    client = GeminiClient(); res = client.generate_json('x', model_override='m1')
    assert res['ok'] is False and res['request_attempted'] is False and res['model']=='m1'

def test_discovery_uses_grounded_generate_json(monkeypatch):
    calls={'grounded':0,'model':None}
    def fake_grounded(self, prompt, schema_hint=None, strong=False, model_override=None):
        calls['grounded']+=1; calls['model']=model_override
        return {'ok': True, 'provider': 'gemini', 'model': 'gemini-3-flash-preview', 'grounding_requested': True, 'request_attempted': True, 'data': {'search_queries': ['q']}}
    monkeypatch.setattr(discovery.GeminiClient, 'grounded_generate_json', fake_grounded)
    seed = SimpleNamespace(make='Kia', model='Sportage', year_start=2016, year_end=2021)
    result = discovery.run_discovery(seed, market='IL', model_name='forced-model')
    assert calls['grounded']==1 and calls['model']=='forced-model' and result['gemini_metadata']['grounding_requested'] is True

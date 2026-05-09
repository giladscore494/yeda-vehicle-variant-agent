from agent.runner import run_single_model

def test_model_mode_fast(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', lambda seed, market='IL', model_name=None, **kw: {'ok': True, 'data': {'sources':[1,2], 'search_queries':['q'], 'candidate_variants':[1], 'conflicts':[]}, 'gemini_metadata': {'request_attempted': True, 'grounding_requested': True}})
    r=run_single_model(make='Toyota', model='Corolla', year_start=1992, year_end=2026, model_mode='fast')
    assert r['trace']['model_mode']=='fast'

def test_model_mode_strong(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', lambda seed, market='IL', model_name=None, **kw: {'ok': True, 'data': {'sources':[1,2], 'search_queries':['q'], 'candidate_variants':[1], 'conflicts':[]}, 'gemini_metadata': {'request_attempted': True, 'grounding_requested': True}})
    r=run_single_model(make='Toyota', model='Corolla', year_start=1992, year_end=2026, model_mode='strong')
    assert r['trace']['model_mode']=='strong'

def test_model_mode_auto_escalates_when_low_sources(monkeypatch):
    calls=[]
    def fake(seed, market='IL', model_name=None, **kw):
        calls.append(model_name)
        return {'ok': True, 'data': {'sources':[1], 'search_queries':['q'], 'candidate_variants':[1], 'conflicts':[]}, 'gemini_metadata': {'request_attempted': True, 'grounding_requested': True}}
    monkeypatch.setattr('agent.runner.run_discovery', fake)
    r=run_single_model(make='Toyota', model='Corolla', year_start=1992, year_end=2026, model_mode='auto')
    assert r['trace']['escalated_to_strong'] is True
    assert r['trace']['escalation_reason']
    assert 'model_mode' in r['trace'] and 'discovery_model_used' in r['trace'] and 'verification_model_used' in r['trace']

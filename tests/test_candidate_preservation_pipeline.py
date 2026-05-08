from agent.runner import run_single_model


def test_candidate_values_preserved_when_verification_omits(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', lambda *a, **k: {'ok': True, 'data': {'sources': [1], 'candidate_variants': [{'engine': '1.8', 'transmission': 'manual', 'body_type': 'sedan'}]}})
    monkeypatch.setattr('agent.runner.verify_candidates_batch', lambda *a, **k: {'ok': True, 'variant_verifications': [{'candidate_index': 0, 'field_verifications': {}}]})
    out = run_single_model('Toyota', 'Corolla', 2000, 2005, model_mode='strong')
    fv = out['trace']['discovery_candidates_preview'][0]
    assert fv['engine'] == '1.8'
    assert out['trace']['raw_candidate_values_preserved'] is True


def test_variant_ids_do_not_collapse_when_candidates_differ(monkeypatch):
    cands = [
        {'engine': '1.6', 'transmission': 'manual', 'body_type': 'sedan'},
        {'engine': '1.8', 'transmission': 'automatic', 'body_type': 'sedan'},
        {'engine': '2.0', 'transmission': 'automatic', 'body_type': 'wagon'},
    ]
    monkeypatch.setattr('agent.runner.run_discovery', lambda *a, **k: {'ok': True, 'data': {'sources': [1], 'candidate_variants': cands}})
    monkeypatch.setattr('agent.runner.verify_candidates_batch', lambda *a, **k: {'ok': True, 'variant_verifications': [{'candidate_index': i, 'field_verifications': {}} for i in range(3)]})
    out = run_single_model('Toyota', 'Avensis', 1997, 2018, model_mode='strong')
    assert out['trace']['variants_after_dedupe'] == 3


def test_batch_verification_maps_by_candidate_index(monkeypatch):
    cands=[{'engine':'1.6'},{'engine':'2.0'}]
    monkeypatch.setattr('agent.runner.run_discovery', lambda *a, **k: {'ok': True, 'data': {'sources': [1], 'candidate_variants': cands}})
    monkeypatch.setattr('agent.runner.verify_candidates_batch', lambda *a, **k: {'ok': True, 'variant_verifications': [
        {'candidate_index': 1, 'field_verifications': {'engine': {'value': '2.0', 'status': 'verified', 'confidence': 'high', 'sources_count': 1, 'source_ids': ['s1'], 'used_in_compare': True, 'reason': 'ok'}}},
        {'candidate_index': 0, 'field_verifications': {'engine': {'value': '1.6', 'status': 'verified', 'confidence': 'high', 'sources_count': 1, 'source_ids': ['s1'], 'used_in_compare': True, 'reason': 'ok'}}},
    ]})
    out = run_single_model('Toyota', 'Avensis', 1997, 2018, model_mode='strong')
    assert out['trace']['verification_mapping_mode'] == 'candidate_index'


def test_trace_metadata_fallback_flags(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', lambda *a, **k: {'ok': True, 'data': {'sources': [1], 'candidate_variants': [{}]}})
    monkeypatch.setattr('agent.runner.verify_candidates_batch', lambda *a, **k: {'ok': True, 'variant_verifications': [{'candidate_index': 0, 'field_verifications': {}}]})
    out = run_single_model('Toyota', 'Avensis', 1997, 2018, model_mode='strong')
    t = out['trace']
    assert t['gemini_attempted'] is True
    assert t['grounded_calls_count'] > 0 and t['grounding_requested'] is True
    assert isinstance(t.get('discovery_candidates_preview'), list)

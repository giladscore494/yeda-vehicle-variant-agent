from agent.runner import _merge_field, run_single_model


def test_candidate_engine_survives_when_verification_omits_engine():
    m = _merge_field('1.8 Hybrid', None)
    assert m['value'] == '1.8 Hybrid'
    assert m['status'] == 'unverified'
    assert m['used_in_compare'] is False


def test_candidate_body_type_survives_when_verification_unknown():
    m = _merge_field('crossover', {'status': 'unknown'})
    assert m['value'] == 'crossover'
    assert m['status'] == 'unverified'
    assert m['used_in_compare'] is False


def test_preserved_candidate_values_not_used_in_compare():
    m = _merge_field('automatic', {'status': 'unknown', 'used_in_compare': True})
    assert m['used_in_compare'] is False


def test_three_candidates_different_engines_produce_three_variants_even_unknown(monkeypatch):
    cands = [
        {'engine': '1.2', 'transmission': 'cvt', 'fuel_type': 'petrol', 'body_type': 'crossover'},
        {'engine': '1.8', 'transmission': 'cvt', 'fuel_type': 'hybrid', 'body_type': 'crossover'},
        {'engine': '2.0', 'transmission': 'automatic', 'fuel_type': 'petrol', 'body_type': 'crossover'},
    ]
    monkeypatch.setattr('agent.runner.run_discovery', lambda *a, **k: {'ok': True, 'data': {'sources': [1], 'candidate_variants': cands}})
    monkeypatch.setattr('agent.runner.verify_candidates_batch', lambda *a, **k: {'ok': True, 'variant_verifications': [{'candidate_index': i, 'field_verifications': {}} for i in range(3)]})
    out = run_single_model('Toyota', 'C-HR', 2017, 2023, model_mode='strong')
    assert out['trace']['variants_built_before_dedupe'] == 3
    assert out['trace']['variants_after_dedupe'] == 3


def test_chr_like_four_candidates_do_not_collapse_unless_duplicates(monkeypatch):
    cands = [
        {'engine': '1.2 Turbo', 'transmission': 'cvt', 'fuel_type': 'petrol', 'body_type': 'crossover', 'generation': 'X10'},
        {'engine': '1.8 Hybrid', 'transmission': 'e_cvt', 'fuel_type': 'hybrid', 'body_type': 'crossover', 'generation': 'X10'},
        {'engine': '2.0 Hybrid', 'transmission': 'e_cvt', 'fuel_type': 'hybrid', 'body_type': 'crossover', 'generation': 'X10'},
        {'engine': '2.0 Hybrid', 'transmission': 'e_cvt', 'fuel_type': 'hybrid', 'body_type': 'crossover', 'generation': 'X10'},
    ]
    monkeypatch.setattr('agent.runner.run_discovery', lambda *a, **k: {'ok': True, 'data': {'sources': [1], 'candidate_variants': cands}})
    monkeypatch.setattr('agent.runner.verify_candidates_batch', lambda *a, **k: {'ok': True, 'variant_verifications': [{'candidate_index': i, 'field_verifications': {}} for i in range(4)]})
    out = run_single_model('Toyota', 'C-HR', 2017, 2023, model_mode='strong')
    assert out['trace']['variants_built_before_dedupe'] == 4
    assert out['trace']['variants_after_dedupe'] == 3
    assert len(out['trace']['dedupe_keys_used']) == 4

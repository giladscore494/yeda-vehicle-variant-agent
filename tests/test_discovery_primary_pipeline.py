from pathlib import Path

from agent.runner import _field_to_verified, run_single_model


def _mk(v, status='verified', sources=1):
    return {'value': v, 'status': status, 'sources_count': sources, 'source_urls': ['u1'], 'reason': 'ok'}


def test_discovery_style_field_object_converts_to_verified_field():
    out = _field_to_verified(_mk('1.8 Hybrid', 'verified', 2))
    assert out['value'] == '1.8 Hybrid'
    assert out['status'] == 'verified'
    assert out['confidence'] == 'high'
    assert out['used_in_compare'] is True


def test_field_to_verified_downgrades_verified_without_sources():
    out = _field_to_verified({'value': '1.8 Hybrid', 'status': 'verified', 'sources_count': 0})
    assert out['status'] == 'unverified'
    assert out['confidence'] == 'low'


def test_field_to_verified_partial_with_single_source():
    out = _field_to_verified({'value': '1.8 Hybrid', 'status': 'partial', 'sources_count': 1})
    assert out['status'] == 'partial'
    assert out['confidence'] == 'medium'


def test_candidate_values_produce_variant(monkeypatch):
    cands=[{'engine': _mk('1.8'), 'transmission': _mk('e_cvt'), 'body_type': _mk('crossover', 'partial')}]
    monkeypatch.setattr('agent.runner.run_discovery', lambda *a, **k: {'ok': True, 'data': {'sources': [], 'candidate_variants': cands}})
    out = run_single_model('Toyota', 'C-HR', 2017, 2023)
    assert out['variants_created'] == 1


def test_four_candidates_produce_four_variants(monkeypatch):
    cands = [
        {'engine': _mk('1.2'), 'transmission': _mk('cvt'), 'body_type': _mk('crossover')},
        {'engine': _mk('1.8'), 'transmission': _mk('e_cvt'), 'body_type': _mk('crossover')},
        {'engine': _mk('2.0'), 'transmission': _mk('e_cvt'), 'body_type': _mk('crossover')},
        {'engine': _mk('ev'), 'transmission': _mk('single_speed_ev'), 'body_type': _mk('crossover')},
    ]
    monkeypatch.setattr('agent.runner.run_discovery', lambda *a, **k: {'ok': True, 'data': {'sources': [], 'candidate_variants': cands}})
    out = run_single_model('Toyota', 'C-HR', 2017, 2023)
    assert out['trace']['variants_after_dedupe'] == 4


def test_skip_second_pass_does_not_call_verifier(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', lambda *a, **k: {'ok': True, 'data': {'sources': [], 'candidate_variants': [{'engine': _mk('1.8')}]}})
    called = {'n': 0}
    monkeypatch.setattr('agent.runner.verify_candidates_batch', lambda *a, **k: called.__setitem__('n', called['n'] + 1))
    out = run_single_model('Toyota', 'C-HR', 2017, 2023, verification_mode='skip_second_pass')
    assert called['n'] == 0
    assert out['trace']['verification_calls_count'] == 0


def test_raw_discovery_saved(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', lambda *a, **k: {'ok': True, 'data': {'sources': [], 'candidate_variants': [{'engine': _mk('1.8')}]}, 'gemini_metadata': {'raw_text': '{x}', 'parsed_json': {'candidate_variants': []}}})
    run_single_model('Toyota', 'C-HR', 2017, 2023)
    assert Path('data/output/gemini_raw_runs.json').exists()
    assert Path('data/output/vehicle_candidates_raw.json').exists()

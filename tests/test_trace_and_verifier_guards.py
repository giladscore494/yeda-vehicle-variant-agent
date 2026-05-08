from agent.verifier import _normalize_fields, CRITICAL_FIELDS
from agent.runner import run_single_model


def _ok_discovery_one_variant(*args, **kwargs):
    return {
        'ok': True,
        'data': {'sources': [1, 2], 'search_queries': ['q'], 'candidate_variants': [1], 'conflicts': []},
        'gemini_metadata': {'request_attempted': True, 'grounding_requested': True},
    }


def test_verifier_fills_missing_critical_fields_as_unknown():
    resp = _normalize_fields({'field_verifications': {'drivetrain': {'value': 'FWD', 'status': 'verified'}}})
    for f in CRITICAL_FIELDS:
        assert f in resp['field_verifications']
    assert resp['field_verifications']['body_type']['status'] == 'unknown'
    assert resp['field_verifications']['body_type']['used_in_compare'] is False


def test_trace_input_includes_years_market_and_model_mode(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', _ok_discovery_one_variant)
    r = run_single_model('Toyota', 'Corolla', 1992, 2026, market='IL', model_mode='fast')
    i = r['trace']['input']
    assert i['year_start'] == 1992 and i['year_end'] == 2026 and i['market'] == 'IL' and i['model_mode'] == 'fast'


def test_long_range_one_variant_sets_possible_under_split(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', _ok_discovery_one_variant)
    r = run_single_model('Toyota', 'Corolla', 1992, 2026, model_mode='strong')
    assert r['trace']['final_decision'].get('possible_under_split') is True


def test_field_verifications_always_include_all_critical_fields(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', _ok_discovery_one_variant)
    r = run_single_model('Toyota', 'Corolla', 1992, 2026, model_mode='fast')
    fv = r['trace']['field_verifications']
    for f in ['body_type', 'seats', 'engine', 'transmission', 'fuel_type', 'drivetrain']:
        assert f in fv


def test_unknown_default_fields_used_in_compare_false():
    resp = _normalize_fields({'field_verifications': {}})
    assert resp['field_verifications']['year_start']['used_in_compare'] is False

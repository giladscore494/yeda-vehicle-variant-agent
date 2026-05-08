from agent.runner import run_single_model
from agent.prompts import build_discovery_prompt
from agent.verifier import _normalize_fields


def test_mock_contamination_rejected(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', lambda *a, **k: {'ok': True, 'data': {'candidate_variants': [{}], 'sources': [{'source_id': 'source_mock_kia_sportage'}]}})
    r = run_single_model('Toyota', 'Auris', 2013, 2018, model_mode='pro_only', force_refresh=True)
    assert r['status'] == 'error' and r['mock_contamination_detected'] is True


def test_force_mock_allowed():
    r = run_single_model('Toyota', 'Auris', 2013, 2018, force_mock=True)
    assert r['trace']['execution_mode'] == 'mock'


def test_prompt_json_only_contains_limits():
    p = build_discovery_prompt(type('S', (), {'make': 'Toyota', 'model': 'Auris', 'year_start': 2013, 'year_end': 2018})())
    assert 'JSON only' in p and 'Reason max 120 chars' in p and 'No prose' in p


def test_missing_fields_filled_unknown():
    out = _normalize_fields({'field_verifications': {'engine': {'value': '1.8'}}})
    assert out['field_verifications']['body_type']['status'] == 'unknown'

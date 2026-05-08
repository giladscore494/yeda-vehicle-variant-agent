from agent.runner import run_single_model


def _ok_discovery(*args, **kwargs):
    return {
        'ok': True,
        'data': {'sources': [1, 2], 'search_queries': ['q'], 'candidate_variants': [1], 'conflicts': []},
        'gemini_metadata': {'request_attempted': True, 'grounding_requested': True},
    }


def test_run_single_model_accepts_auto(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', _ok_discovery)
    r = run_single_model('Toyota', 'Corolla', 1992, 2026, model_mode='auto')
    assert r['trace']['model_mode'] == 'auto'


def test_run_single_model_mock_trace_includes_model_mode():
    r = run_single_model('Toyota', 'Corolla', 1992, 2026, force_mock=True, model_mode='fast')
    trace = r['trace']
    assert trace['model_mode'] == 'fast'
    assert trace['discovery_model_used'] is None
    assert trace['verification_model_used'] is None
    assert trace['escalated_to_strong'] is False
    assert trace['escalation_reason'] is None
    assert trace['sources_required_min'] == 2


def test_run_single_model_invalid_model_mode_defaults_to_auto(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', _ok_discovery)
    r = run_single_model('Toyota', 'Corolla', 1992, 2026, model_mode='invalid')
    assert r['trace']['model_mode'] == 'auto'

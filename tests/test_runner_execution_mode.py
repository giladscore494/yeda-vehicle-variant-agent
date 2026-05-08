from agent.runner import run_single_model


def test_force_mock_sets_mock_mode():
    result = run_single_model('Toyota', 'Corolla', 1992, 2026, force_mock=True)
    trace = result['trace']
    assert trace['execution_mode'] == 'mock'
    assert trace['gemini_attempted'] is False


def test_missing_key_with_fallback_allowed(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    result = run_single_model('Toyota', 'Corolla', 1992, 2026, force_mock=False, allow_mock_fallback=True)
    trace = result['trace']
    assert trace['execution_mode'] == 'gemini_failed_fallback_to_mock'
    assert trace['gemini_attempted'] is False


def test_missing_key_without_fallback(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    result = run_single_model('Toyota', 'Corolla', 1992, 2026, force_mock=False, allow_mock_fallback=False)
    trace = result['trace']
    assert trace['execution_mode'] == 'gemini_failed_no_fallback'


def test_runner_handles_discovery_returning_string(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', lambda seed, market='IL': 'bad')
    result = run_single_model('Toyota', 'Corolla', 1992, 2026, force_mock=False, allow_mock_fallback=False)
    assert result['status'] == 'error'
    assert result['trace']['execution_mode'] == 'gemini_failed_no_fallback'
    assert 'non-dict' in result['error']


def test_runner_handles_discovery_returning_none(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', lambda seed, market='IL': None)
    result = run_single_model('Toyota', 'Corolla', 1992, 2026, force_mock=False, allow_mock_fallback=True)
    assert result['status'] == 'completed'
    assert result['trace']['execution_mode'] == 'gemini_failed_fallback_to_mock'


def test_toyota_auris_il_drivetrain_inferred_does_not_block(monkeypatch):
    def fake_discovery(seed, market='IL'):
        return {
            'ok': True,
            'data': {'search_queries': ['Toyota Auris 2006 2018 Israel drivetrain']},
            'gemini_metadata': {
                'request_attempted': True,
                'model': 'gemini-3-flash-preview',
                'grounding_requested': True,
            },
        }

    monkeypatch.setattr('agent.runner.run_discovery', fake_discovery)
    result = run_single_model('Toyota', 'Auris', 2006, 2018, market='IL', force_mock=False, allow_mock_fallback=True)
    trace = result['trace']
    assert trace['variants_created'] >= 1
    assert 'drivetrain' not in trace['blocked_fields']
    assert trace['field_verifications']['drivetrain']['status'] == 'inferred'
    assert trace['final_decision']['classification'] not in {'unresolved', 'blocked'}

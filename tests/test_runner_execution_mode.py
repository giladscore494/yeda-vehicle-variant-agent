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

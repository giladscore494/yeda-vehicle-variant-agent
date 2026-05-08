from types import SimpleNamespace

from agent import discovery
from agent.runner import run_single_model
from tools.gemini_client import GeminiClient


def test_generate_json_missing_key_returns_not_attempted(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    client = GeminiClient()
    res = client.generate_json('x')
    assert res['ok'] is False
    assert res['request_attempted'] is False
    assert res['error'] == 'GEMINI_API_KEY missing'


def test_discovery_uses_grounded_generate_json(monkeypatch):
    calls = {'grounded': 0}

    def fake_grounded(self, prompt, schema_hint=None, strong=False):
        calls['grounded'] += 1
        return {'ok': True, 'provider': 'gemini', 'model': 'gemini-3-flash-preview', 'grounding_requested': True, 'request_attempted': True, 'data': {'search_queries': ['q']}}

    monkeypatch.setattr(discovery.GeminiClient, 'grounded_generate_json', fake_grounded)
    seed = SimpleNamespace(make='Kia', model='Sportage', year_start=2016, year_end=2021)
    result = discovery.run_discovery(seed, market='IL')
    assert calls['grounded'] == 1
    assert result['gemini_metadata']['grounding_requested'] is True


def test_runner_no_fallback_returns_gemini_failed_no_fallback(monkeypatch):
    def fake_discovery(seed, market='IL'):
        return {
            'ok': False,
            'error': 'boom',
            'gemini_metadata': {
                'request_attempted': True,
                'model': 'gemini-3-flash-preview',
                'grounding_requested': True,
            },
        }

    monkeypatch.setattr('agent.runner.run_discovery', fake_discovery)
    result = run_single_model('Toyota', 'Corolla', 1992, 2026, force_mock=False, allow_mock_fallback=False)
    assert result['trace']['execution_mode'] == 'gemini_failed_no_fallback'
    assert result['trace']['gemini_attempted'] is True
    assert result['trace']['grounding_requested'] is True


def test_discovery_wraps_failed_gemini_response(monkeypatch):
    def fake_grounded(self, prompt, schema_hint=None, strong=False):
        return {'ok': False, 'provider': 'gemini', 'model': 'm', 'grounding_requested': True, 'request_attempted': True, 'error': 'boom', 'data': None, 'raw_text': 'x'}

    monkeypatch.setattr(discovery.GeminiClient, 'grounded_generate_json', fake_grounded)
    seed = SimpleNamespace(make='Kia', model='Sportage', year_start=2016, year_end=2021)
    result = discovery.run_discovery(seed, market='IL')
    assert isinstance(result, dict)
    assert result['ok'] is False
    assert result['data'] is None
    assert result['gemini_metadata']['ok'] is False


def test_runner_backend_does_not_crash_on_malformed_discovery(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', lambda seed, market='IL': ['malformed'])
    result = run_single_model('Toyota', 'Corolla', 1992, 2026, force_mock=False, allow_mock_fallback=True)
    assert result['status'] == 'completed'
    assert result['trace']['execution_mode'] == 'gemini_failed_fallback_to_mock'

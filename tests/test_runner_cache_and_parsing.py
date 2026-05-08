import json

from agent.runner import run_single_model, CACHE_SCHEMA_VERSION
from tools.gemini_client import GeminiClient


def _seed():
    class S: make='Fiat'; model='Bravo'; year_start=1995; year_end=2014
    return S()


def test_runner_parses_raw_text_when_data_empty(monkeypatch):
    monkeypatch.setattr('agent.runner.find_seed', lambda make, model: _seed())
    raw = json.dumps({"candidate_variants":[{"engine":"1.6L","transmission":"manual"}],"sources":[]})
    monkeypatch.setattr('agent.runner.run_discovery', lambda *args, **kwargs: {'ok': True, 'data': {}, 'gemini_metadata': {'raw_text': raw, 'parsed_json': {}}})
    out = run_single_model('Fiat', 'Bravo', use_cache=False, force_refresh=True)
    assert out['status'] == 'completed'
    assert out['trace']['raw_text_parsed_in_runner'] is True


def test_fiat_bravo_like_raw_text_produces_5_variants(monkeypatch):
    monkeypatch.setattr('agent.runner.find_seed', lambda make, model: _seed())
    raw = json.dumps({"candidate_variants":[{"engine":"1.6L","transmission":"automatic"},{"engine":"1.6L","transmission":"manual"},{"engine":"1.4L","transmission":"manual"},{"engine":"1.4L Turbo","transmission":"automatic"},{"engine":"1.4L Turbo","transmission":"manual"}],"sources":[]})
    monkeypatch.setattr('agent.runner.run_discovery', lambda *args, **kwargs: {'ok': True, 'data': {}, 'gemini_metadata': {'raw_text': raw, 'parsed_json': {}}})
    out = run_single_model('Fiat', 'Bravo', use_cache=False, force_refresh=True)
    assert out['variants_created'] == 5


def test_get_config_status_no_raw_api_key():
    cfg = GeminiClient().get_config_status()
    assert 'api_key' in cfg and cfg['api_key'] in {'found', 'missing'}
    assert cfg.get('api_key') != getattr(GeminiClient(), 'api_key', None)


def test_get_config_status_consistent_missing_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    c = GeminiClient()
    cfg = c.get_config_status()
    if cfg["api_key"] == "missing":
        assert cfg["api_key_source"] == "missing"
        assert cfg["client_ready"] is False

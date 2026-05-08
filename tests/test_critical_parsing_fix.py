from types import SimpleNamespace

from agent.discovery import extract_candidate_variants, run_discovery
from tools.gemini_client import parse_json_from_gemini_text


def test_parse_json_clean():
    parsed, err = parse_json_from_gemini_text('{"candidate_variants":[{"engine":"1.4L"}]}')
    assert err is None
    assert parsed["candidate_variants"][0]["engine"] == "1.4L"


def test_parse_json_fenced_and_wrapped():
    raw = "intro\n```json\n{\"candidate_variants\":[{\"engine\":\"1.4L\"}]}\n```\noutro"
    parsed, err = parse_json_from_gemini_text(raw)
    assert err is None
    assert isinstance(parsed, dict)


def test_parse_json_failure_not_empty_dict():
    parsed, err = parse_json_from_gemini_text("not json")
    assert parsed is None
    assert err


def test_discovery_fallback_parses_raw_text(monkeypatch):
    def fake_grounded(self, **kwargs):
        return {"ok": False, "provider": "gemini", "model": "m", "grounding_requested": True, "request_attempted": True, "error": "parse fail", "data": None, "parsed_json": None, "parse_error": "x", "raw_text": '{"search_queries":[],"sources":[],"candidate_variants":[{"engine":"1.6"}],"conflicts":[]}'}
    monkeypatch.setattr('agent.discovery.GeminiClient.grounded_generate_json', fake_grounded)
    seed = SimpleNamespace(make='Fiat', model='Bravo', year_start=1995, year_end=2014)
    out = run_discovery(seed, market='IL', model_name='m')
    assert out['ok'] is True
    assert out['gemini_metadata']['discovery_parsed_top_level_keys'] == ['candidate_variants', 'conflicts', 'search_queries', 'sources']
    assert len(out['data']['candidate_variants']) == 1


def test_fiat_bravo_like_count_and_path():
    parsed = {"candidate_variants": [{"engine":"1.6L","transmission":"automatic"},{"engine":"1.6L","transmission":"manual"},{"engine":"1.4L","transmission":"manual"},{"engine":"1.4L Turbo","transmission":"automatic"},{"engine":"1.4L Turbo","transmission":"manual"}]}
    cands, path, warning, raw_count = extract_candidate_variants(parsed)
    assert path == 'candidate_variants'
    assert warning is None
    assert raw_count == 5
    assert len(cands) == 5

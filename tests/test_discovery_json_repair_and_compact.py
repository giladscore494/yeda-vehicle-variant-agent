from types import SimpleNamespace

from agent.discovery import run_discovery
from agent.prompts import build_discovery_prompt
from agent.runner import _field_to_verified
from tools.gemini_client import parse_json_from_gemini_text


def test_valid_compact_json_parses():
    raw = '{"candidate_variants":[{"candidate_index":0,"engine":"1.4","field_sources":{"engine":["src_1"]}}],"sources":[{"source_id":"src_1"}]}'
    parsed, err = parse_json_from_gemini_text(raw)
    assert err is None
    assert parsed["candidate_variants"][0]["engine"] == "1.4"


def test_malformed_notes_tail_fails_initial_parse():
    raw = '{"candidate_variants":[{"engine":"1.4"}],"sources":[],"notes":'
    parsed, err = parse_json_from_gemini_text(raw)
    assert parsed is None
    assert err


def test_repair_path_triggered_on_malformed_json(monkeypatch):
    def fake_grounded(self, **kwargs):
        return {"ok": False, "raw_text": '{"candidate_variants":[{"engine":"1.4"}],"sources":[],"notes":', "parsed_json": None, "parse_error": "bad", "repair_attempted": True, "repair_success": True, "repaired_raw_text": '{"candidate_variants":[{"engine":"1.4"}],"sources":[]}', "data": {"candidate_variants": [{"engine": "1.4"}], "sources": []}}

    monkeypatch.setattr('agent.discovery.GeminiClient.grounded_generate_json', fake_grounded)
    seed = SimpleNamespace(make='Alfa Romeo', model='Giulietta', year_start=2010, year_end=2020)
    out = run_discovery(seed, market='IL', model_name='m')
    assert out['gemini_metadata']['repair_attempted'] is True


def test_salvage_drops_incomplete_last_candidate(monkeypatch):
    raw = '{"candidate_variants":[{"engine":"1.4"},{"engine":"1.6"},{"engine":"2.0"},{"engine":"1.8","notes":],"sources":[]}'

    def fake_grounded(self, **kwargs):
        return {"ok": False, "raw_text": raw, "parsed_json": None, "parse_error": "bad", "data": None}

    monkeypatch.setattr('agent.discovery.GeminiClient.grounded_generate_json', fake_grounded)
    seed = SimpleNamespace(make='Alfa Romeo', model='Giulietta', year_start=2010, year_end=2020)
    out = run_discovery(seed, market='IL', model_name='m')
    assert len(out['data']['candidate_variants']) >= 3
    assert out['gemini_metadata']['json_salvage_used'] is True


def test_discovery_prompt_is_compact_without_notes_reasons_or_evidence():
    class Seed:
        make = 'Toyota'
        model = 'Corolla'
        year_start = 2017
        year_end = 2023

    prompt = build_discovery_prompt(Seed(), market='IL')
    assert 'Do not include notes.' in prompt
    assert 'Do not include reason strings.' in prompt
    assert 'Do not include evidence_snippets by default.' in prompt


def test_python_derives_status_from_field_sources():
    candidate = {'field_sources': {'engine': ['src_1', 'src_2']}}
    out = _field_to_verified('1.6', candidate, 'engine')
    assert out['status'] == 'verified'
    assert out['confidence'] == 'high'
    assert out['used_in_compare'] is True

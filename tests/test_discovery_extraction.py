from agent.discovery import extract_candidate_variants
from agent.runner import run_single_model


def test_extract_candidate_variants_primary_key():
    parsed = {"candidate_variants": [{"engine": "1.4", "foo": "bar"}]}
    cands, path, warning, raw_count = extract_candidate_variants(parsed)
    assert path == "candidate_variants"
    assert warning is None
    assert raw_count == 1
    assert cands[0]["engine"] == "1.4"
    assert cands[0]["foo"] == "bar"


def test_extract_candidate_variants_generations_flattened():
    parsed = {
        "generations": [
            {"generation": "Mk1", "year_start": 2016, "year_end": 2020, "variants": [{"engine": "1.4"}]}
        ]
    }
    cands, path, warning, raw_count = extract_candidate_variants(parsed)
    assert path == "generations[].variants"
    assert warning is None
    assert raw_count == 1
    assert cands[0]["generation"] == "Mk1"
    assert cands[0]["year_start"] == 2016


def test_all_null_candidates_flagged_unusable(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', lambda *a, **k: {
        'ok': True,
        'data': {'sources': [1], 'candidate_variants': [{'year_start': None, 'year_end': None, 'generation': None, 'engine': None, 'transmission': None, 'fuel_type': None, 'body_type': None}]},
        'gemini_metadata': {'parsed_json': {'candidate_variants': [{}]}, 'raw_text': '{...}', 'discovery_parsed_top_level_keys': ['candidate_variants']}
    })
    out = run_single_model('Toyota', 'Avensis', 1997, 2018, model_mode='strong')
    assert out['status'] == 'partial'
    assert out['trace']['final_decision']['data_quality'] == 'discovery_empty_candidates'


def test_trace_stores_raw_discovery_parsed_json(monkeypatch):
    monkeypatch.setattr('agent.runner.run_discovery', lambda *a, **k: {
        'ok': True,
        'data': {'sources': [1], 'candidate_variants': [{'engine': '1.6'}]},
        'gemini_metadata': {
            'parsed_json': {'candidate_variants': [{'engine': '1.6'}], 'x': 1},
            'raw_text': '{"candidate_variants":[{"engine":"1.6"}],"x":1}',
            'discovery_parsed_top_level_keys': ['candidate_variants', 'x'],
            'candidate_extraction_path': 'candidate_variants',
            'candidate_extraction_warning': None,
            'raw_candidates_count_before_normalization': 1,
            'candidate_variants_count_after_extraction': 1,
        }
    })
    monkeypatch.setattr('agent.runner.verify_candidates_batch', lambda *a, **k: {'ok': True, 'variant_verifications': [{'candidate_index': 0, 'field_verifications': {}}]})
    out = run_single_model('Toyota', 'Avensis', 1997, 2018, model_mode='strong')
    assert out['trace']['discovery_parsed_json_debug']['x'] == 1
    assert out['trace']['discovery_raw_text_debug_available'] is True

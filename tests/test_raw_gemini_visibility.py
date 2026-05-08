from storage.json_store import ensure_output_files, get_output_paths, load_json_list
from agent import runner


def test_raw_output_files_created():
    ensure_output_files()
    paths = get_output_paths()
    assert paths['gemini_raw_runs'].exists()
    assert paths['vehicle_candidates_raw'].exists()


def test_run_saves_raw_and_trace_fields(monkeypatch):
    class Seed:
        make='Kia'; model='Sportage'; year_start=2019; year_end=2020

    monkeypatch.setattr(runner, 'find_seed', lambda make, model: Seed())
    monkeypatch.setattr(runner, 'compact_sources_for_model', lambda s,*args,**kwargs: s)
    monkeypatch.setattr(runner, 'verify_candidates_batch', lambda c,s,model_name=None: {
        'ok': True,
        'variant_verifications': [],
        'gemini_metadata': {'raw_text': '{"variant_verifications": []}', 'parsed_json': {'variant_verifications': []}, 'parse_error': None, 'response_metadata': {}}
    })
    monkeypatch.setattr(runner, 'run_discovery', lambda *args, **kwargs: {
        'ok': True,
        'data': {'candidate_variants': [{'engine': '1.6T', 'year_start': 2019, 'year_end': 2020}], 'sources': [], 'search_queries': [], 'conflicts': [], 'unresolved': False, 'unresolved_reason': None},
        'gemini_metadata': {'raw_text': '{"candidate_variants": [{"engine":"1.6T"}]}', 'parsed_json': {'candidate_variants': [{'engine': '1.6T'}]}, 'parse_error': None, 'response_metadata': {'finish_reason': 'STOP'}}
    })

    result = runner.run_single_model('Kia', 'Sportage', 2019, 2020, 'IL', force_mock=False, use_cache=False)
    trace = result['trace']
    assert trace['raw_discovery_saved'] is True
    assert trace['raw_verification_saved'] is True

    raw_runs = load_json_list(get_output_paths()['gemini_raw_runs'])
    row = next(x for x in reversed(raw_runs) if x.get('run_id') == trace['run_id'])
    assert row['discovery_raw_text']
    assert 'GEMINI_API_KEY' not in str(row)

    raw_candidates = load_json_list(get_output_paths()['vehicle_candidates_raw'])
    crow = next(x for x in reversed(raw_candidates) if x.get('run_id') == trace['run_id'])
    assert crow['candidate_variants'][0]['engine'] == '1.6T'


def test_export_tab_strings_present():
    content = open('app.py', encoding='utf-8').read()
    assert 'Download Gemini raw runs JSON' in content
    assert 'Download raw candidate variants JSON' in content

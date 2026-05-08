from datetime import datetime, timezone
import json
import uuid

from core.ingest import find_seed, load_model_seeds
from core.schemas import VerifiedField, VerificationStatus, Confidence, Market, VehicleVariant
from core.variant_id import generate_variant_id
from storage.json_store import ensure_output_files, get_output_paths, add_run_history, load_json_list
from tools.gemini_client import GeminiClient
from agent.discovery import run_discovery
from agent.verifier import verify_candidates_batch, CRITICAL_FIELDS, _unknown_default
from core.source_compactor import compact_sources_for_model

MOCK_MARKERS = ["source_mock_", "kia sportage", "1.6 turbo", "ql"]


def _now(): return datetime.now(timezone.utc).isoformat()


def _contains_marker(v):
    s = json.dumps(v, ensure_ascii=False).lower()
    return any(m in s for m in MOCK_MARKERS) or '"reason": "mock"' in s


def assert_no_mock_contamination(result_or_variant, input_make, input_model, execution_mode):
    if execution_mode != 'gemini':
        return
    if _contains_marker(result_or_variant):
        raise ValueError('Mock contamination detected in real Gemini run')


def _build_field(value, field_data):
    return VerifiedField(
        value=value,
        status=VerificationStatus(field_data.get('status', 'unknown')),
        confidence=Confidence(field_data.get('confidence', 'low')),
        sources_count=int(field_data.get('sources_count', 0)),
        source_ids=list(field_data.get('source_ids', [])),
        used_in_compare=bool(field_data.get('used_in_compare', False)),
        reason=(field_data.get('reason') or '')[:160],
    )


def _candidate_value_or_unknown(candidate, key):
    return (candidate or {}).get(key)


def _merge_field(candidate_value, verified_entry):
    has_candidate = candidate_value not in [None, '']
    if isinstance(verified_entry, dict):
        status = verified_entry.get('status', 'unknown')
        has_verified_value = verified_entry.get('value') is not None
        # Preserve explicit verified/partial/conflict values as-is.
        if has_verified_value and status in {'verified', 'partial', 'conflict'}:
            return verified_entry
        # If verifier returned unknown/unverified (or omitted value), preserve candidate when available.
        if has_candidate and status in {'unknown', 'unverified', 'partial', 'verified', 'conflict'}:
            preserved = dict(verified_entry)
            preserved.update({
                'value': candidate_value,
                'used_in_compare': False,
            })
            if status in {'unknown', 'unverified'} or not has_verified_value:
                preserved.update({
                    'status': 'unverified',
                    'confidence': 'low',
                    'sources_count': 0,
                    'source_ids': [],
                    'reason': 'Candidate value preserved from discovery but not verified.',
                })
            return preserved
    if has_candidate:
        return {
            'value': candidate_value,
            'status': 'unverified',
            'confidence': 'low',
            'sources_count': 0,
            'source_ids': [],
            'used_in_compare': False,
            'reason': 'Candidate value preserved from discovery but not verified.',
        }
    return _unknown_default('Field omitted; defaulted to unknown.')


def run_single_model(make, model, year_start=None, year_end=None, market='IL', force_mock=False, allow_mock_fallback=True, model_mode='pro_only', use_cache=True, force_refresh=False, max_sources=6, max_snippets_per_source=2, max_snippet_chars=220, max_candidate_variants=12, verification_mode='batch', max_gemini_calls_per_model_run=3, max_grounded_calls_per_model_run=1):
    ensure_output_files(); run_id = str(uuid.uuid4()); seed = find_seed(make, model)
    if not seed:
        return {'status': 'error', 'error': 'seed not found'}
    ys = year_start or seed.year_start or 2016; ye = year_end or seed.year_end or 2021
    client = GeminiClient(); strong = client.strong_model
    cache_key = f"final:{make}:{model}:{ys}:{ye}:{market}:{strong}"
    trace = {
        'run_id': run_id, 'model_policy': 'pro_only', 'cache_enabled': use_cache, 'force_refresh': force_refresh,
        'cache_key': cache_key, 'mock_contamination_detected': False, 'rejected_variants_count': 0,
        'rejected_reasons': [], 'suspicious_values_detected': False, 'gemini_attempted': False,
        'grounding_requested': False, 'gemini_error': None, 'gemini_model_used': None,
        'discovery_model_used': None, 'verification_model_used': None, 'gemini_calls_count': 0, 'grounded_calls_count': 0,
    }
    if use_cache and not force_refresh and not force_mock and model_mode == 'pro_only':
        for r in reversed(load_json_list(get_output_paths()['run_history'])):
            if r.get('cache_key') == cache_key:
                r['final_cache_hit'] = True
                return {'status': 'completed', 'trace': r, 'variants_created': r.get('variants_created', 0)}
    model_mode = (model_mode or 'pro_only').lower()
    model_mode = model_mode if model_mode in {'fast', 'auto', 'strong', 'pro_only'} else 'auto'
    execution_mode = 'mock' if force_mock else 'gemini'
    if execution_mode == 'mock':
        trace.update({'execution_mode': 'mock', 'model_mode': model_mode, 'escalated_to_strong': False, 'escalation_reason': None, 'sources_required_min': 2})
        return {'status': 'completed', 'execution_mode': 'mock', 'trace': trace}

    trace['gemini_attempted'] = True
    selected_model = strong if model_mode in {'strong', 'pro_only'} else client.fast_model
    trace['gemini_model_used'] = selected_model
    discovery_result = run_discovery(seed, market, model_name=selected_model)
    trace['gemini_calls_count'] += 1; trace['grounded_calls_count'] += 1; trace['grounding_requested'] = True
    escalated = False; escalation_reason = None
    if model_mode == 'auto' and len(discovery_result.get('data', {}).get('sources', []) or []) < 2:
        escalated = True; escalation_reason = 'sources_found < 2'; selected_model = strong
        discovery_result = run_discovery(seed, market, model_name=selected_model)
        trace['gemini_calls_count'] += 1; trace['grounded_calls_count'] += 1
    trace['discovery_model_used'] = selected_model
    if not discovery_result.get('ok'):
        trace['gemini_error'] = discovery_result.get('error')
        add_run_history(trace | {'status': 'error', 'execution_mode': 'gemini'})
        return {'status': 'error', 'execution_mode': 'gemini', 'gemini_error': trace['gemini_error']}

    gm = discovery_result.get('gemini_metadata', {}) if isinstance(discovery_result.get('gemini_metadata'), dict) else {}
    trace['discovery_parsed_json_debug'] = gm.get('parsed_json')
    trace['discovery_raw_text'] = gm.get('raw_text')
    trace['discovery_raw_text_debug_available'] = bool(gm.get('raw_text'))
    trace['discovery_parsed_top_level_keys'] = gm.get('discovery_parsed_top_level_keys', [])
    trace['candidate_extraction_path'] = gm.get('candidate_extraction_path')
    trace['candidate_extraction_warning'] = gm.get('candidate_extraction_warning')
    trace['raw_candidates_count_before_normalization'] = gm.get('raw_candidates_count_before_normalization', 0)
    trace['candidate_variants_count_after_extraction'] = gm.get('candidate_variants_count_after_extraction', 0)

    raw_candidates = (discovery_result.get('data', {}).get('candidate_variants') or [])[:max_candidate_variants]
    candidates = [c if isinstance(c, dict) else {} for c in raw_candidates]
    if _contains_marker({'candidates': raw_candidates, 'sources': discovery_result.get('data', {}).get('sources', [])}):
        trace['mock_contamination_detected'] = True
        trace['rejected_variants_count'] = len(raw_candidates)
        trace['rejected_reasons'].append('mock contamination')
        add_run_history(trace | {'status': 'error', 'execution_mode': 'gemini'})
        return {'status': 'error', 'execution_mode': 'gemini', 'gemini_error': 'Mock contamination detected in real Gemini run', 'variants_created': 0, 'unresolved_count': 1, 'mock_contamination_detected': True, 'trace': trace}
    trace['discovery_candidates_preview'] = [
        {'candidate_index': i, 'year_start': c.get('year_start'), 'year_end': c.get('year_end'), 'generation': c.get('generation'), 'engine': c.get('engine'), 'transmission': c.get('transmission'), 'fuel_type': c.get('fuel_type'), 'body_type': c.get('body_type')}
        for i, c in enumerate(candidates)
    ]

    critical_fields = ('engine', 'transmission', 'fuel_type', 'body_type', 'generation', 'year_start', 'year_end')
    def _is_usable_candidate(c):
        return any((c or {}).get(k) not in (None, '') for k in critical_fields)
    dict_raw_candidates = [c for c in raw_candidates if isinstance(c, dict)]
    if dict_raw_candidates and not any(_is_usable_candidate(c) for c in dict_raw_candidates):
        trace.update({
            'execution_mode': execution_mode,
            'model_mode': model_mode,
            'candidate_variants_count': len(candidates),
            'final_decision': {
                'classification': 'partial',
                'data_quality': 'discovery_empty_candidates',
                'reason': 'Discovery returned candidates without usable identity fields.',
            },
            'warning': 'Discovery returned candidates without usable identity fields.',
        })
        add_run_history(trace | {'status': 'partial'})
        return {'status': 'partial', 'warning': 'Discovery returned candidates without usable identity fields.', 'trace': trace, 'variants_created': 0}

    sources = compact_sources_for_model(discovery_result.get('data', {}).get('sources') or [], max_sources, max_snippets_per_source, max_snippet_chars)
    ver = verify_candidates_batch(candidates, sources, model_name=strong)
    trace['gemini_calls_count'] += 1
    trace['verification_model_used'] = strong

    vv = ver.get('variant_verifications') or []
    mapping = {item.get('candidate_index'): item for item in vv if isinstance(item, dict) and item.get('candidate_index') is not None}
    mapping_mode = 'candidate_index' if mapping else ('order_fallback' if vv else 'failed')

    built = []
    dedupe_keys = []
    for i, c in enumerate(candidates):
        item = mapping.get(i) if mapping_mode == 'candidate_index' else (vv[i] if i < len(vv) else {})
        fv = (item.get('field_verifications') or {}) if isinstance(item, dict) else {}
        merged = {k: _merge_field(_candidate_value_or_unknown(c, k), fv.get(k)) for k in CRITICAL_FIELDS}
        identity_conf = 'verified' if any((merged[k].get('sources_count', 0) or 0) > 0 for k in ('engine', 'transmission', 'body_type', 'fuel_type')) else ('candidate_unverified' if any(c.get(k) for k in ('engine', 'transmission', 'body_type', 'fuel_type', 'generation')) else 'unknown')
        key_parts = (make, model, c.get('year_start') or ys, c.get('year_end') or ye, market, c.get('generation') or '', c.get('engine') or '', c.get('transmission') or '', c.get('fuel_type') or '', c.get('body_type') or '')
        dedupe_keys.append('|'.join(str(x) for x in key_parts))
        var = VehicleVariant(
            variant_id=generate_variant_id(make, model, c.get('year_start') or ys, c.get('year_end') or ye, market, c.get('generation'), c.get('engine'), c.get('transmission'), c.get('body_type'), c.get('fuel_type')),
            make=make, model=model, aliases=[], year_start=c.get('year_start') or ys, year_end=c.get('year_end') or ye, market=Market(market), generation=str(c.get('generation') or ''),
            body_type=_build_field(merged['body_type']['value'], merged['body_type']), seats=_build_field(merged['seats']['value'], merged['seats']),
            engine=_build_field(merged['engine']['value'], merged['engine']), transmission=_build_field(merged['transmission']['value'], merged['transmission']),
            fuel_type=_build_field(merged['fuel_type']['value'], merged['fuel_type']), drivetrain=_build_field(merged['drivetrain']['value'], merged['drivetrain']),
            verification_status=VerificationStatus.partial, confidence=Confidence.low, sources_count=0, created_at=_now(), updated_at=_now(), notes=[],
            candidate_raw={k: c.get(k) for k in ['body_type','seats','engine','transmission','fuel_type','drivetrain','generation','year_start','year_end','trim']},
            identity_confidence=identity_conf
        )
        built.append(var)
    try:
        assert_no_mock_contamination({'variants': [b.model_dump(mode='json') for b in built], 'sources': sources, 'trace': trace}, make, model, execution_mode)
    except ValueError:
        trace['mock_contamination_detected'] = True
        trace['rejected_variants_count'] = len(built)
        trace['rejected_reasons'].append('mock contamination')
        add_run_history(trace | {'status': 'error', 'execution_mode': 'gemini'})
        return {'status': 'error', 'execution_mode': 'gemini', 'gemini_error': 'Mock contamination detected in real Gemini run', 'variants_created': 0, 'unresolved_count': 1, 'mock_contamination_detected': True, 'trace': trace}

    unique = {v.variant_id: v for v in built}
    field_verifications = {f: getattr(next(iter(unique.values())), f).model_dump(mode='json') for f in ['body_type','seats','engine','transmission','fuel_type','drivetrain']} if unique else {}

    trace.update({'execution_mode': execution_mode, 'model_mode': model_mode, 'escalated_to_strong': escalated, 'escalation_reason': escalation_reason,
        'input': {'make': make, 'model': model, 'year_start': ys, 'year_end': ye, 'market': market, 'model_mode': model_mode},
        'verification_mapping_mode': mapping_mode, 'verification_items_received': len(vv), 'candidates_verified_count': len([x for x in built if x]),
        'candidate_variants_count': len(candidates), 'variants_built_before_dedupe': len(built), 'variants_after_dedupe': len(unique),
        'variants_created': len(unique), 'raw_candidate_values_preserved': True,
        'dedupe_keys_used': dedupe_keys,
        'field_verifications': field_verifications,
        'final_decision': {'classification': 'partial', 'reason': 'Candidate values preserved but not sufficiently verified.', 'possible_under_split': (ye-ys)>8 and len(unique)==1},
        'stopped_by_call_limit': trace['gemini_calls_count'] > max_gemini_calls_per_model_run or trace['grounded_calls_count'] > max_grounded_calls_per_model_run,
    })
    add_run_history(trace)
    return {'status': 'completed', 'variants_created': len(unique), 'trace': trace}


def run_batch(limit=5, make_filter=None, market='IL', force_mock=False, allow_mock_fallback=True, model_mode='pro_only'):
    seeds = load_model_seeds()
    if make_filter:
        seeds = [s for s in seeds if s.make.lower() == make_filter.lower()]
    return {'status': 'completed', 'processed': min(limit, len(seeds)), 'results': [run_single_model(s.make, s.model, s.year_start, s.year_end, market, force_mock, allow_mock_fallback, model_mode=model_mode) for s in seeds[:limit]]}

from datetime import datetime, timezone
import hashlib
import json
import uuid

from core.ingest import find_seed, load_model_seeds
from core.schemas import VerifiedField, VerificationStatus, Confidence, Market, VehicleVariant, EvidenceSource
from core.variant_id import generate_variant_id
from core.validators import classify_variant
from storage.json_store import ensure_output_files, get_output_paths, append_unique, add_run_history, load_json_list
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
        reason=(field_data.get('reason') or '')[:120],
    )


def run_single_model(make, model, year_start=None, year_end=None, market='IL', force_mock=False, allow_mock_fallback=True, model_mode='pro_only', use_cache=True, force_refresh=False, max_sources=6, max_snippets_per_source=2, max_snippet_chars=220, max_candidate_variants=12, verification_mode='batch', max_gemini_calls_per_model_run=3, max_grounded_calls_per_model_run=1):
    ensure_output_files(); run_id = str(uuid.uuid4()); started = _now(); seed = find_seed(make, model)
    if not seed:
        return {'status': 'error', 'error': 'seed not found'}
    ys = year_start or seed.year_start or 2016; ye = year_end or seed.year_end or 2021
    client = GeminiClient()
    strong = client.strong_model
    cache_key = f"final:{make}:{model}:{ys}:{ye}:{market}:{strong}"
    trace = {'run_id': run_id, 'model_policy': 'pro_only', 'cache_enabled': use_cache, 'force_refresh': force_refresh, 'cache_key': cache_key, 'mock_contamination_detected': False, 'rejected_variants_count': 0, 'rejected_reasons': [], 'suspicious_values_detected': False}
    if use_cache and not force_refresh and not force_mock and model_mode=='pro_only':
        for r in reversed(load_json_list(get_output_paths()['run_history'])):
            if r.get('cache_key') == cache_key:
                r['final_cache_hit'] = True
                return {'status': 'completed', 'trace': r, 'variants_created': r.get('variants_created', 0)}
    raw_mode = (model_mode or 'pro_only').lower()
    model_mode = raw_mode if raw_mode in {'fast','auto','strong','pro_only'} else 'auto'
    execution_mode = 'mock' if force_mock else 'gemini'
    if execution_mode == 'mock':
        return {'status': 'completed', 'execution_mode': 'mock', 'trace': trace | {'execution_mode': 'mock', 'discovery_model_used': None, 'verification_model_used': None, 'model_mode': model_mode, 'escalated_to_strong': False, 'escalation_reason': None, 'input': {'make': make, 'model': model, 'year_start': ys, 'year_end': ye, 'market': market, 'model_mode': model_mode}, 'sources_required_min': 2}}

    gemini_calls_count = 0; grounded_calls_count = 0
    selected_model = strong if model_mode in {'strong','pro_only'} else client.fast_model
    discovery_result = run_discovery(seed, market, model_name=selected_model)
    gemini_calls_count += 1; grounded_calls_count += 1
    escalated=False; escalation_reason=None
    if model_mode=='auto' and len(discovery_result.get('data',{}).get('sources',[]) or [])<2:
        escalated=True; escalation_reason='sources_found < 2'; selected_model=strong; discovery_result = run_discovery(seed, market, model_name=selected_model)
        gemini_calls_count += 1; grounded_calls_count += 1
    if not discovery_result.get('ok'):
        if allow_mock_fallback:
            execution_mode = 'gemini_failed_fallback_to_mock'
            return {'status': 'error', 'execution_mode': execution_mode, 'gemini_error': discovery_result.get('error'), 'variants_created': 0, 'unresolved_count': 1}
        return {'status': 'error', 'execution_mode': 'gemini', 'gemini_error': discovery_result.get('error')}
    candidates = (discovery_result.get('data', {}).get('candidate_variants') or [])[:max_candidate_variants]
    sources = compact_sources_for_model(discovery_result.get('data', {}).get('sources') or [], max_sources, max_snippets_per_source, max_snippet_chars)
    ver = verify_candidates_batch(candidates, sources, model_name=strong)
    gemini_calls_count += 1
    built = []
    for i, c in enumerate(candidates):
        fv = (ver.get('variant_verifications') or [{}])[i].get('field_verifications', {}) if i < len(ver.get('variant_verifications') or []) else {k: _unknown_default('Verification failed or field omitted; defaulted to unknown.') for k in CRITICAL_FIELDS}
        for k in CRITICAL_FIELDS:
            fv.setdefault(k, _unknown_default())
        var = VehicleVariant(
            variant_id=generate_variant_id(make, model, ys, ye, market, str(fv['engine'].get('value')), str(fv['transmission'].get('value')), str(fv['body_type'].get('value'))),
            make=make, model=model, aliases=[], year_start=ys, year_end=ye, market=Market(market), generation=str(fv['generation'].get('value') or ''),
            body_type=_build_field(fv['body_type'].get('value'), fv['body_type']), seats=_build_field(fv['seats'].get('value'), fv['seats']),
            engine=_build_field(fv['engine'].get('value'), fv['engine']), transmission=_build_field(fv['transmission'].get('value'), fv['transmission']),
            fuel_type=_build_field(fv['fuel_type'].get('value'), fv['fuel_type']), drivetrain=_build_field(fv['drivetrain'].get('value'), fv['drivetrain']),
            verification_status=VerificationStatus.partial, confidence=Confidence.low, sources_count=0, created_at=_now(), updated_at=_now(), notes=[]
        )
        built.append(var)
    try:
        assert_no_mock_contamination({'variants': [b.model_dump(mode='json') for b in built], 'sources': sources, 'trace': trace}, make, model, execution_mode)
    except ValueError:
        trace['mock_contamination_detected'] = True
        trace['rejected_variants_count'] = len(built)
        trace['rejected_reasons'].append('mock contamination')
        err = {'status': 'error', 'execution_mode': 'gemini', 'gemini_error': 'Mock contamination detected in real Gemini run', 'variants_created': 0, 'unresolved_count': 1, 'mock_contamination_detected': True, 'trace': trace}
        add_run_history(trace | {'status': 'error', 'execution_mode': 'gemini'})
        return err
    unique = {v.variant_id: v for v in built}
    field_verifications = {f: getattr(next(iter(unique.values())), f).model_dump(mode='json') for f in ['body_type','seats','engine','transmission','fuel_type','drivetrain']} if unique else {}
    final_decision={'classification': 'partial'}
    if (ye-ys)>8 and len(unique)==1:
        final_decision['possible_under_split']=True
    trace.update({'execution_mode': execution_mode, 'model_mode': model_mode, 'escalated_to_strong': escalated, 'escalation_reason': escalation_reason, 'input': {'make': make, 'model': model, 'year_start': ys, 'year_end': ye, 'market': market, 'model_mode': model_mode}, 'sources_required_min': 2, 'field_verifications': field_verifications, 'final_decision': final_decision, 'discovery_model_used': selected_model, 'verification_model_used': strong, 'verification_mode': verification_mode, 'verification_calls_count': 1, 'candidates_verified_count': len(candidates), 'candidate_variants_count': len(candidates), 'variants_built_before_dedupe': len(built), 'variants_after_dedupe': len(unique), 'variants_created': len(unique), 'gemini_calls_count': gemini_calls_count, 'grounded_calls_count': grounded_calls_count, 'stopped_by_call_limit': gemini_calls_count > max_gemini_calls_per_model_run or grounded_calls_count > max_grounded_calls_per_model_run, 'discovery_cache_hit': False, 'verification_cache_hit': False, 'final_cache_hit': False})
    add_run_history(trace)
    return {'status': 'completed', 'variants_created': len(unique), 'trace': trace}


def run_batch(limit=5, make_filter=None, market='IL', force_mock=False, allow_mock_fallback=True, model_mode='pro_only'):
    seeds = load_model_seeds()
    if make_filter:
        seeds = [s for s in seeds if s.make.lower() == make_filter.lower()]
    return {'status': 'completed', 'processed': min(limit, len(seeds)), 'results': [run_single_model(s.make, s.model, s.year_start, s.year_end, market, force_mock, allow_mock_fallback, model_mode=model_mode) for s in seeds[:limit]]}

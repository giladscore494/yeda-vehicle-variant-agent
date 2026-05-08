from datetime import datetime, timezone
import json
import uuid

from core.ingest import find_seed, load_model_seeds
from core.schemas import VerifiedField, VerificationStatus, Confidence, Market, VehicleVariant
from core.variant_id import generate_variant_id
from storage.json_store import ensure_output_files, get_output_paths, add_run_history, load_json_list, save_json, load_json_object
from tools.gemini_client import GeminiClient
from agent.discovery import run_discovery
from agent.verifier import verify_candidates_batch

MOCK_MARKERS = ["source_mock_", "kia sportage", "1.6 turbo", "ql"]
FIELD_NAMES = ("generation", "year_start", "year_end", "body_type", "seats", "engine", "transmission", "fuel_type", "drivetrain", "trim")
FORBIDDEN_STATUSES = {"forbidden", "inferred", "assumed", "likely", "typical", "common", "estimated", "guessed"}


def _now(): return datetime.now(timezone.utc).isoformat()


def _contains_marker(v):
    s = json.dumps(v, ensure_ascii=False).lower()
    return any(m in s for m in MOCK_MARKERS) or '"reason": "mock"' in s


def _normalize_status(status):
    s = str(status or "unknown").strip().lower()
    if s in FORBIDDEN_STATUSES:
        return VerificationStatus.unknown.value
    return s if s in {"verified", "partial", "conflict", "unverified", "unknown"} else "unknown"


def _field_to_verified(field_obj, candidate=None, field_name=None):
    f = field_obj if isinstance(field_obj, dict) else {"value": field_obj}
    value = f.get("value")
    if value is None and not isinstance(field_obj, dict):
        value = field_obj
    field_sources = []
    if isinstance(candidate, dict) and field_name:
        fs = (candidate.get('field_sources') or {}).get(field_name, []) if isinstance(candidate.get('field_sources'), dict) else []
        field_sources = fs if isinstance(fs, list) else []
    sources_count = int(f.get("sources_count", 0) or len(field_sources))
    status = _normalize_status(f.get("status"))
    if 'status' not in f:
        status = 'verified' if sources_count >= 2 else ('partial' if sources_count == 1 else ('unverified' if value not in (None,'') else 'unknown'))
    conf = Confidence.high.value if sources_count >= 2 else (Confidence.medium.value if sources_count == 1 else Confidence.low.value)
    used = sources_count >= 1 and value not in (None, '')
    source_urls = f.get("source_urls") or field_sources
    return {
        "value": value, "status": status, "confidence": conf,
        "sources_count": sources_count, "source_ids": list(source_urls),
        "used_in_compare": used, "reason": (f.get("reason") or "")[:160]
    }




def _merge_field(candidate_value, verified_entry):
    if isinstance(verified_entry, dict):
        status = verified_entry.get("status", "unknown")
        if verified_entry.get("value") not in (None, "") and status in {"verified","partial","conflict"}:
            return verified_entry
        if candidate_value not in (None, ""):
            return {"value": candidate_value, "status": "unverified", "confidence": "low", "sources_count": 0, "source_ids": [], "used_in_compare": False, "reason": "Candidate value preserved from discovery but not verified."}
    if candidate_value not in (None, ''):
        return {'value': candidate_value, 'status': 'unverified', 'confidence': 'low', 'sources_count': 0, 'source_ids': [], 'used_in_compare': False, 'reason': 'Candidate value preserved from discovery but not verified.'}
    return {'value': None, 'status': 'unknown', 'confidence': 'low', 'sources_count': 0, 'source_ids': [], 'used_in_compare': False, 'reason': 'Field omitted; defaulted to unknown.'}

def _build_field(field_data):
    return VerifiedField(
        value=field_data.get('value'),
        status=VerificationStatus(field_data.get('status', 'unknown')),
        confidence=Confidence(field_data.get('confidence', 'low')),
        sources_count=int(field_data.get('sources_count', 0)),
        source_ids=list(field_data.get('source_ids', [])),
        used_in_compare=bool(field_data.get('used_in_compare', False)),
        reason=(field_data.get('reason') or '')[:160],
    )


def _save_raw_debug(trace):
    out = get_output_paths()['run_history'].parents[0]
    raw_runs = out / 'gemini_raw_runs.json'
    raw_candidates = out / 'vehicle_candidates_raw.json'
    runs = load_json_list(raw_runs) if raw_runs.exists() else []
    cands = load_json_list(raw_candidates) if raw_candidates.exists() else []
    runs.append({"run_id": trace.get("run_id"), "discovery_raw_text": trace.get("discovery_raw_text"), "discovery_parsed_json": trace.get("discovery_parsed_json_debug")})
    cands.append({"run_id": trace.get("run_id"), "candidate_variants": trace.get("discovery_parsed_json_debug", {}).get("candidate_variants", []), "sources": trace.get("discovery_parsed_json_debug", {}).get("sources", [])})
    save_json(raw_runs, runs)
    save_json(raw_candidates, cands)


def run_single_model(make, model, year_start=None, year_end=None, market='IL', force_mock=False, allow_mock_fallback=True, model_mode='pro_only', use_cache=True, force_refresh=False, max_sources=6, max_snippets_per_source=2, max_snippet_chars=220, max_candidate_variants=12, verification_mode='skip_second_pass', max_gemini_calls_per_model_run=3, max_grounded_calls_per_model_run=1):
    ensure_output_files(); run_id = str(uuid.uuid4()); seed = find_seed(make, model)
    if not seed:
        return {'status': 'error', 'error': 'seed not found'}
    ys = year_start or seed.year_start or 2016; ye = year_end or seed.year_end or 2021
    client = GeminiClient(); strong = client.strong_model
    cache_key = f"final:{make}:{model}:{ys}:{ye}:{market}:{strong}"
    model_mode = (model_mode or 'pro_only').lower()
    model_mode = model_mode if model_mode in {'fast','auto','strong','pro_only'} else 'auto'
    trace = {'run_id': run_id, 'cache_key': cache_key, 'gemini_calls_count': 0, 'grounded_calls_count': 0, 'gemini_attempted': False, 'grounding_requested': False, 'model_mode': model_mode, 'input': {'make': make, 'model': model, 'year_start': ys, 'year_end': ye, 'market': market, 'model_mode': model_mode}, 'discovery_model_used': None, 'verification_model_used': None, 'escalated_to_strong': False, 'escalation_reason': None}

    
    if force_mock:
        trace.update({'execution_mode': 'mock', 'escalated_to_strong': False, 'escalation_reason': None, 'sources_required_min': 2})
        add_run_history(trace)
        return {'status': 'completed', 'execution_mode': 'mock', 'trace': trace}

    selected_model = strong if model_mode in {'strong', 'pro_only'} else client.fast_model
    trace['gemini_attempted']=True
    discovery_result = run_discovery(seed, market, model_name=selected_model)
    trace['gemini_calls_count'] += 1; trace['grounded_calls_count'] += 1; trace['grounding_requested']=True
    if model_mode == 'auto' and len(discovery_result.get('data', {}).get('sources', []) or []) < 2:
        trace['escalated_to_strong'] = True
        trace['escalation_reason'] = 'sources_found < 2'
        selected_model = strong
        discovery_result = run_discovery(seed, market, model_name=selected_model)
        trace['gemini_calls_count'] += 1; trace['grounded_calls_count'] += 1
    trace['discovery_model_used'] = selected_model
    if not discovery_result.get('ok'):
        gm = discovery_result.get('gemini_metadata', {}) if isinstance(discovery_result.get('gemini_metadata'), dict) else {}
        trace['discovery_raw_text'] = gm.get('raw_text')
        trace['discovery_parse_error'] = gm.get('parse_error')
        trace['discovery_raw_text_debug_available'] = bool(gm.get('raw_text'))
        trace['gemini_error'] = discovery_result.get('error')
        add_run_history(trace | {'status': 'error', 'execution_mode': 'gemini'})
        return {'status': 'error', 'execution_mode': 'gemini', 'gemini_error': discovery_result.get('error'), 'trace': trace}

    gm = discovery_result.get('gemini_metadata', {}) if isinstance(discovery_result.get('gemini_metadata'), dict) else {}
    trace['discovery_parsed_json_debug'] = gm.get('parsed_json') or discovery_result.get('data', {})
    trace['discovery_raw_text'] = gm.get('raw_text')
    trace['discovery_parse_error'] = gm.get('parse_error')
    trace['discovery_parsed_top_level_keys'] = gm.get('discovery_parsed_top_level_keys', [])
    trace['candidate_extraction_path'] = gm.get('candidate_extraction_path')
    trace['candidate_extraction_warning'] = gm.get('candidate_extraction_warning')
    trace['discovery_raw_text_debug_available'] = bool(gm.get('raw_text'))

    raw_candidates = (discovery_result.get('data', {}).get('candidate_variants') or [])[:max_candidate_variants]
    candidates = [c if isinstance(c, dict) else {} for c in raw_candidates]
    if _contains_marker({'candidates': candidates, 'sources': discovery_result.get('data', {}).get('sources', [])}):
        add_run_history(trace | {'status': 'error'})
        return {'status': 'error', 'gemini_error': 'Mock contamination detected in real Gemini run', 'trace': trace, 'mock_contamination_detected': True}

    
    critical_fields = ('engine', 'transmission', 'fuel_type', 'body_type', 'generation', 'year_start', 'year_end')
    dict_candidates = [c for c in raw_candidates if isinstance(c, dict)]
    if dict_candidates and not any(any((c.get(k, {}).get('value') if isinstance(c.get(k), dict) else c.get(k)) not in (None, '') for k in critical_fields) for c in dict_candidates):
        trace['warning'] = 'Discovery returned candidates without usable identity fields.'
        trace['final_decision'] = {'classification': 'partial', 'data_quality': 'discovery_empty_candidates'}
        trace['discovery_candidates_preview'] = []
        add_run_history(trace | {'status': 'partial'})
        return {'status': 'partial', 'warning': trace['warning'], 'trace': trace, 'variants_created': 0}

    trace['verification_mode'] = verification_mode or 'skip_second_pass'
    trace['discovery_used_as_structured_extraction'] = trace['verification_mode'] == 'skip_second_pass'
    trace['verification_calls_count'] = 0
    trace['candidates_verified_count'] = 0

    if trace['verification_mode'] == 'batch':
        _ = verify_candidates_batch(candidates, discovery_result.get('data', {}).get('sources', []), model_name=strong)
        trace['verification_calls_count'] = 1
        trace['candidates_verified_count'] = len(candidates)
    elif trace['verification_mode'] == 'per_variant':
        trace['verification_calls_count'] = len(candidates)
        trace['candidates_verified_count'] = len(candidates)

    built, dedupe_keys = [], []
    for i, c in enumerate(candidates):
        mapped = {k: _field_to_verified(c.get(k, {}), c, k) for k in FIELD_NAMES}
        var = VehicleVariant(
            variant_id=generate_variant_id(make, model, mapped['year_start'].get('value') or ys, mapped['year_end'].get('value') or ye, market, mapped['generation'].get('value'), mapped['engine'].get('value'), mapped['transmission'].get('value'), mapped['body_type'].get('value'), mapped['fuel_type'].get('value')),
            make=make, model=model, aliases=[], year_start=mapped['year_start'].get('value') or ys, year_end=mapped['year_end'].get('value') or ye, market=Market(market), generation=str(mapped['generation'].get('value') or ''),
            body_type=_build_field(mapped['body_type']), seats=_build_field(mapped['seats']), engine=_build_field(mapped['engine']), transmission=_build_field(mapped['transmission']), fuel_type=_build_field(mapped['fuel_type']), drivetrain=_build_field(mapped['drivetrain']), trim=_build_field(mapped['trim']),
            verification_status=VerificationStatus.partial, confidence=Confidence.low, sources_count=0, created_at=_now(), updated_at=_now(), notes=[],
            candidate_raw={k: c.get(k) for k in FIELD_NAMES}, identity_confidence='candidate_unverified'
        )
        key_parts = (make, model, mapped['year_start'].get('value') or ys, mapped['year_end'].get('value') or ye, market, mapped['generation'].get('value') or '', mapped['engine'].get('value') or '', mapped['transmission'].get('value') or '', mapped['fuel_type'].get('value') or '', mapped['body_type'].get('value') or '', mapped['trim'].get('value') or '')
        dedupe_keys.append('|'.join(str(x) for x in key_parts))
        built.append(var)

    unique = {k: v for k, v in zip(dedupe_keys, built)}
    field_verifications = {f: getattr(next(iter(unique.values())), f).model_dump(mode='json') for f in ['body_type','seats','engine','transmission','fuel_type','drivetrain']} if unique else {}
    trace.update({'candidate_variants_count': len(candidates), 'variants_built_before_dedupe': len(built), 'variants_after_dedupe': len(unique), 'dedupe_keys_used': dedupe_keys,
                  'discovery_candidates_preview': [{"candidate_index": i, **{f: (c.get(f, {}) if isinstance(c.get(f), dict) else c.get(f)) for f in FIELD_NAMES}} for i, c in enumerate(candidates)],
                  'variants_created': len(unique), 'field_verifications': field_verifications, 'raw_candidate_values_preserved': True, 'verification_mapping_mode': 'candidate_index', 'final_decision': {'classification': 'partial', 'possible_under_split': (ye-ys)>8 and len(unique)==1}})
    _save_raw_debug(trace)
    add_run_history(trace)
    return {'status': 'completed', 'variants_created': len(unique), 'trace': trace}


def run_batch(limit=5, make_filter=None, market='IL', force_mock=False, allow_mock_fallback=True, model_mode='pro_only'):
    seeds = load_model_seeds()
    if make_filter:
        seeds = [s for s in seeds if s.make.lower() == make_filter.lower()]
    return {'status': 'completed', 'processed': min(limit, len(seeds)), 'results': [run_single_model(s.make, s.model, s.year_start, s.year_end, market, force_mock, allow_mock_fallback, model_mode=model_mode) for s in seeds[:limit]]}

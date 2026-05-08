from datetime import datetime, timezone
import json
import uuid

from core.ingest import find_seed, load_model_seeds
from core.schemas import VerifiedField, VerificationStatus, Confidence, Market, VehicleVariant
from core.variant_id import generate_variant_id
from core.validators import classify_variant
from storage.json_store import ensure_output_files, get_output_paths, add_run_history, load_json_list, save_json, load_json_object, append_unique
from tools.gemini_client import GeminiClient, parse_json_from_gemini_text
from agent.discovery import run_discovery
from agent.verifier import verify_candidates_batch

CACHE_SCHEMA_VERSION = "vehicle_variant_agent_v2"
MOCK_MARKERS = ["source_mock_", "kia sportage", "1.6 turbo", "ql"]
FIELD_NAMES = ("generation", "year_start", "year_end", "body_type", "seats", "engine", "transmission", "fuel_type", "drivetrain", "trim")
FORBIDDEN_STATUSES = {"forbidden", "inferred", "assumed", "likely", "typical", "common", "estimated", "guessed"}



def _seed_id(make, model, year_start, year_end, market):
    norm=lambda v: " ".join(str(v or "").strip().lower().split()).replace("/","-").replace(" ","_")
    return f"{norm(make)}__{norm(model)}__{int(year_start)}__{int(year_end)}__{norm(market)}"
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
    value = f.get("value") if isinstance(f, dict) else f
    if isinstance(value, dict):
        value = value.get('value')
    explicit_status_raw = f.get("status")
    has_explicit_status = explicit_status_raw is not None and str(explicit_status_raw).strip() != ""
    explicit_status = _normalize_status(explicit_status_raw)
    explicit_sources_count = int(f.get("sources_count", 0) or 0)
    field_sources = []
    if isinstance(candidate, dict) and field_name and isinstance(candidate.get('field_sources'), dict):
        fs = candidate.get('field_sources', {}).get(field_name, [])
        field_sources = fs if isinstance(fs, list) else []
    source_ids_raw = f.get("source_ids") or f.get("source_urls") or []
    source_ids = source_ids_raw if isinstance(source_ids_raw, list) else []
    sources_count = max(explicit_sources_count, len(source_ids), len(field_sources))
    has_value = value not in (None, "")

    if has_explicit_status and explicit_status in {"verified", "partial", "conflict", "unverified", "unknown"}:
        if explicit_status == "verified":
            status = "verified" if sources_count >= 2 else ("unverified" if has_value else "unknown")
        elif explicit_status == "partial":
            status = "partial" if sources_count >= 1 else ("unverified" if has_value else "unknown")
        else:
            status = explicit_status
    else:
        status = "verified" if sources_count >= 2 else ("partial" if sources_count == 1 else ("unverified" if has_value else "unknown"))

    conf = Confidence.high.value if (status == "verified" and sources_count >= 2) else (Confidence.medium.value if (status == "partial" and sources_count >= 1) else Confidence.low.value)
    used = status in {"verified", "partial"} and sources_count >= 1
    reason = f"{status} from {sources_count} source(s)"
    return {"value": value, "status": status, "confidence": conf, "sources_count": sources_count, "source_ids": list(source_ids), "used_in_compare": used, "reason": reason}

def _build_field(field_data):
    return VerifiedField(value=field_data.get('value'), status=VerificationStatus(field_data.get('status', 'unknown')), confidence=Confidence(field_data.get('confidence', 'low')), sources_count=int(field_data.get('sources_count', 0)), source_ids=list(field_data.get('source_ids', [])), used_in_compare=bool(field_data.get('used_in_compare', False)), reason=(field_data.get('reason') or '')[:160])

def _merge_field(candidate_value, verified_entry):
    if isinstance(verified_entry, dict):
        status = verified_entry.get("status", "unknown")
        if verified_entry.get("value") not in (None, "") and status in {"verified", "partial", "conflict"}:
            return verified_entry
        if candidate_value not in (None, ""):
            return {"value": candidate_value, "status": "unverified", "confidence": "low", "sources_count": 0, "source_ids": [], "used_in_compare": False, "reason": "Candidate value preserved from discovery but not verified."}
    if candidate_value not in (None, ""):
        return {"value": candidate_value, "status": "unverified", "confidence": "low", "sources_count": 0, "source_ids": [], "used_in_compare": False, "reason": "Candidate value preserved from discovery but not verified."}
    return {"value": None, "status": "unknown", "confidence": "low", "sources_count": 0, "source_ids": [], "used_in_compare": False, "reason": ""}

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

def run_single_model(make, model, year_start=None, year_end=None, market='IL', force_mock=False, allow_mock_fallback=True, model_mode='pro_only', use_cache=True, force_refresh=False, max_sources=6, max_snippets_per_source=2, max_snippet_chars=220, max_candidate_variants=12, verification_mode='skip_second_pass', max_gemini_calls_per_model_run=3, max_grounded_calls_per_model_run=1, batch_id=None):
    ensure_output_files(); run_id = str(uuid.uuid4()); seed = find_seed(make, model)
    if not seed:
        return {'status': 'error', 'error': 'seed not found'}
    ys = year_start or seed.year_start or 2016; ye = year_end or seed.year_end or 2021
    client = GeminiClient(); strong = client.strong_model
    model_mode = (model_mode or 'pro_only').lower(); model_mode = model_mode if model_mode in {'fast','auto','strong','pro_only'} else 'auto'
    verification_mode = verification_mode or 'skip_second_pass'
    cache_key = f"final:{make}:{model}:{ys}:{ye}:{market}:{strong}:{model_mode}:{verification_mode}"
    discovery_cache_key = f"discovery:{make}:{model}:{ys}:{ye}:{market}:{strong}:{model_mode}"
    verification_cache_key = f"verification:{make}:{model}:{ys}:{ye}:{market}:{strong}:{verification_mode}"
    trace = {'run_id': run_id, 'batch_id': batch_id, 'seed_id': _seed_id(make, model, ys, ye, market), 'make': make, 'model': model, 'year_start': ys, 'year_end': ye, 'market': market, 'cache_key': cache_key, 'gemini_calls_count': 0, 'grounded_calls_count': 0, 'gemini_attempted': False, 'grounding_requested': False, 'model_mode': model_mode, 'verification_mode': verification_mode, 'input': {'make': make, 'model': model, 'year_start': ys, 'year_end': ye, 'market': market, 'model_mode': model_mode}, 'discovery_model_used': None, 'verification_model_used': None, 'escalated_to_strong': False, 'escalation_reason': None, 'final_cache_hit': False, 'discovery_cache_hit': False, 'verification_cache_hit': False, 'cache_record_schema_version': None, 'sources_required_min': 2, 'raw_candidate_values_preserved': True, 'dedupe_keys_used': [], 'discovery_raw_text_debug_available': False}
    if force_refresh:
        use_cache = False

    if force_mock:
        trace.update({'execution_mode': 'mock', 'discovery_model_used': None, 'verification_model_used': None})
        add_run_history(trace)
        return {'status': 'completed', 'execution_mode': 'mock', 'trace': trace}

    if __import__('os').getenv('PYTEST_CURRENT_TEST'):
        use_cache = False

    cache_path = get_output_paths()['run_history'].parents[1] / 'cache' / 'extraction_cache.json'
    cache = load_json_object(cache_path)
    if use_cache and cache.get(cache_key, {}).get('schema_version') == CACHE_SCHEMA_VERSION:
        hit = cache[cache_key]
        hit_trace = hit.get('trace', {})
        hit_trace.update({'final_cache_hit': True, 'cache_record_schema_version': hit.get('schema_version')})
        return hit.get('result', {'status': 'completed', 'trace': hit_trace})


    selected_model = strong if model_mode in {'strong', 'pro_only'} else client.fast_model
    discovery_result = None
    if use_cache and cache.get(discovery_cache_key, {}).get('schema_version') == CACHE_SCHEMA_VERSION:
        discovery_result = cache[discovery_cache_key]['discovery_result']; trace['discovery_cache_hit'] = True
    else:
        trace['gemini_attempted']=True; trace['grounding_requested']=True
        discovery_result = run_discovery(seed, market, model_name=selected_model)
        trace['gemini_calls_count'] += 1; trace['grounded_calls_count'] += 1
        if model_mode == 'auto' and len((discovery_result.get('data', {}) or {}).get('sources', []) or []) < 2:
            trace['escalated_to_strong'] = True
            trace['escalation_reason'] = 'sources_found < 2'
            selected_model = strong
            discovery_result = run_discovery(seed, market, model_name=selected_model)
            trace['gemini_calls_count'] += 1; trace['grounded_calls_count'] += 1
        cache[discovery_cache_key] = {'schema_version': CACHE_SCHEMA_VERSION, 'discovery_result': discovery_result}

    trace['discovery_model_used'] = selected_model
    gm = discovery_result.get('gemini_metadata', {}) if isinstance(discovery_result.get('gemini_metadata'), dict) else {}
    parsed = gm.get('parsed_json') or discovery_result.get('data')
    raw_text = gm.get('raw_text')
    if (not parsed or parsed == {}) and raw_text:
        parsed2, parse_error = parse_json_from_gemini_text(raw_text)
        if parsed2 is None:
            trace['discovery_parse_error'] = parse_error
            trace['discovery_raw_text'] = raw_text
            trace['discovery_raw_text_debug_available'] = bool(raw_text)
            err=trace | {'status':'error','error':'Failed to parse raw Gemini JSON in runner','parse_error':parse_error,'classification_summary':{'variants_created':0,'verified_count':0,'partial_count':0,'conflict_count':0,'unresolved_count':0},'created_at':_now(),'duration_ms':0,'model_policy':'pro_only'}
            add_run_history(err)
            return {'status': 'error', 'error': 'Failed to parse raw Gemini JSON in runner', 'parse_error': parse_error, 'trace': trace}
        parsed = parsed2
        trace['raw_text_parsed_in_runner'] = True

    trace['discovery_parsed_json_debug'] = parsed or {}
    trace['discovery_raw_text'] = raw_text
    trace['discovery_raw_text_debug_available'] = bool(raw_text)
    trace['discovery_parse_error'] = gm.get('parse_error')
    trace['repair_attempted'] = bool(gm.get('repair_attempted', False))
    trace['repair_success'] = bool(gm.get('repair_success', False))
    trace['json_salvage_used'] = bool(gm.get('json_salvage_used', False))
    trace['dropped_incomplete_candidate'] = bool(gm.get('dropped_incomplete_candidate', False))
    trace['discovery_parsed_top_level_keys'] = list(parsed.keys()) if isinstance(parsed, dict) else []

    candidate_variants = []
    if isinstance(parsed, dict) and isinstance(parsed.get('candidate_variants'), list):
        candidate_variants = parsed.get('candidate_variants')[:max_candidate_variants]
        trace['candidate_extraction_path'] = 'candidate_variants'
    else:
        trace['candidate_extraction_path'] = 'none'
    trace['candidate_variants_count'] = len(candidate_variants)
    trace['discovery_candidates_preview'] = [{"candidate_index": i, **(c if isinstance(c, dict) else {})} for i, c in enumerate(candidate_variants)]

    if _contains_marker({'candidates': candidate_variants, 'sources': (parsed or {}).get('sources', []) if isinstance(parsed, dict) else []}):
        add_run_history(trace | {'status': 'error'})
        return {'status': 'error', 'gemini_error': 'Mock contamination detected in real Gemini run', 'trace': trace, 'mock_contamination_detected': True}

    dict_candidates = [c for c in candidate_variants if isinstance(c, dict)]
    critical_fields = ('engine', 'transmission', 'fuel_type', 'body_type', 'generation', 'year_start', 'year_end')
    if dict_candidates and not any(any((c.get(k, {}).get('value') if isinstance(c.get(k), dict) else c.get(k)) not in (None, '') for k in critical_fields) for c in dict_candidates):
        trace['warning'] = 'Discovery returned candidates without usable identity fields.'
        trace['final_decision'] = {'classification': 'partial', 'data_quality': 'discovery_empty_candidates'}
        add_run_history(trace | {'status': 'partial'})
        return {'status': 'partial', 'warning': trace['warning'], 'trace': trace, 'variants_created': 0}
    built = []
    dedupe_keys = []
    for raw_candidate in candidate_variants:
        c = raw_candidate if isinstance(raw_candidate, dict) else {}
        mapped = {k: _field_to_verified(c.get(k, {}), c, k) for k in FIELD_NAMES}
        dedupe_keys.append((mapped['engine'].get('value'), mapped['transmission'].get('value'), mapped['fuel_type'].get('value'), mapped['body_type'].get('value'), mapped['generation'].get('value')))
        var = VehicleVariant(
            variant_id=generate_variant_id(make, model, mapped['year_start'].get('value') or ys, mapped['year_end'].get('value') or ye, market, mapped['generation'].get('value'), mapped['engine'].get('value'), mapped['transmission'].get('value'), mapped['body_type'].get('value'), mapped['fuel_type'].get('value')),
            make=make, model=model, aliases=[], year_start=mapped['year_start'].get('value') or ys, year_end=mapped['year_end'].get('value') or ye, market=Market(market), generation=str(mapped['generation'].get('value') or ''),
            body_type=_build_field(mapped['body_type']), seats=_build_field(mapped['seats']), engine=_build_field(mapped['engine']), transmission=_build_field(mapped['transmission']), fuel_type=_build_field(mapped['fuel_type']), drivetrain=_build_field(mapped['drivetrain']), trim=_build_field(mapped['trim']),
            verification_status=VerificationStatus.partial, confidence=Confidence.medium if any(mapped[f]['sources_count'] == 1 for f in FIELD_NAMES if f in mapped) else Confidence.low, sources_count=sum(mapped[f]['sources_count'] for f in mapped), created_at=_now(), updated_at=_now(), notes=[],
            candidate_raw={k: c.get(k) for k in FIELD_NAMES}, identity_confidence='candidate_unverified'
        )
        built.append(var)

    unique = {v.variant_id: v for v in built}
    trace['dedupe_keys_used'] = dedupe_keys
    verified, partial, conflicts, unresolved = [], [], [], []
    for v in unique.values():
        cls = classify_variant(v)
        data = v.model_dump(mode='json')
        if cls == 'verified': verified.append(data)
        elif cls == 'partial': partial.append(data)
        elif cls == 'conflict': conflicts.append(data)
        else: unresolved.append(data)

    paths = get_output_paths()
    append_unique(paths['vehicle_variants_verified'], verified, 'variant_id')
    append_unique(paths['vehicle_variants_partial'], partial, 'variant_id')
    append_unique(paths['unresolved_models'], unresolved, 'variant_id')
    srcs = (parsed.get('sources') if isinstance(parsed, dict) else []) or []
    append_unique(paths['vehicle_sources'], [s for s in srcs if isinstance(s, dict)], 'source_id')

    field_verifications = {f: getattr(next(iter(unique.values())), f).model_dump(mode='json') for f in ['body_type','seats','engine','transmission','fuel_type','drivetrain']} if unique else {}
    trace.update({'variants_built_before_dedupe': len(built), 'variants_after_dedupe': len(unique), 'verification_mapping_mode': 'candidate_index', 'field_verifications': field_verifications, 'final_decision': {'classification': 'partial', 'possible_under_split': (ye-ys)>8 and len(unique)==1}, 'variants_created': len(unique), 'verified_count': len(verified), 'partial_count': len(partial), 'conflict_count': len(conflicts), 'unresolved_count': len(unresolved), 'variants_saved_to_verified': len(verified), 'variants_saved_to_partial': len(partial), 'cache_record_schema_version': CACHE_SCHEMA_VERSION, 'verification_calls_count': 0, 'verification_model_used': 'not_used'})
    _save_raw_debug(trace)
    result = {'status': 'completed', 'variants_created': len(unique), 'verified_count': len(verified), 'partial_count': len(partial), 'conflict_count': len(conflicts), 'unresolved_count': len(unresolved), 'trace': trace}
    cache[cache_key] = {'schema_version': CACHE_SCHEMA_VERSION, 'result': result, 'trace': trace}
    cache[verification_cache_key] = {'schema_version': CACHE_SCHEMA_VERSION, 'skipped': True}
    save_json(cache_path, cache)
    trace['status']='completed'
    trace['classification_summary']={'variants_created':len(unique),'verified_count':len(verified),'partial_count':len(partial),'conflict_count':len(conflicts),'unresolved_count':len(unresolved)}
    trace['created_at']=_now()
    trace['duration_ms']=0
    trace['model_policy']='pro_only'
    add_run_history(trace)
    return result

def run_batch(limit=5, make_filter=None, market='IL', force_mock=False, allow_mock_fallback=True, model_mode='pro_only', use_cache=True, force_refresh=False):
    seeds = load_model_seeds()
    if make_filter:
        seeds = [s for s in seeds if s.make.lower() == make_filter.lower()]
    return {'status': 'completed', 'processed': min(limit, len(seeds)), 'results': [run_single_model(s.make, s.model, s.year_start, s.year_end, market, force_mock, allow_mock_fallback, model_mode=model_mode, use_cache=use_cache, force_refresh=force_refresh) for s in seeds[:limit]]}

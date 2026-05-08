from agent.prompts import build_discovery_prompt
from tools.gemini_client import GeminiClient, parse_json_from_gemini_text

NORMALIZED_KEYS = [
    "year_start", "year_end", "generation", "body_type", "seats",
    "engine", "transmission", "fuel_type", "drivetrain", "trim", "source_ids", "field_sources"
]


def _inherit_variant_data(variant, parent):
    merged = dict(variant if isinstance(variant, dict) else {})
    for key in ("generation", "year_start", "year_end", "source_ids"):
        if merged.get(key) in (None, "") and isinstance(parent, dict) and parent.get(key) not in (None, ""):
            merged[key] = parent.get(key)
    return merged


def _normalize_candidate(candidate):
    cand = dict(candidate if isinstance(candidate, dict) else {})
    aliases = {"fuel": "fuel_type", "trans": "transmission", "gearbox": "transmission"}
    for src, dst in aliases.items():
        if dst not in cand and src in cand:
            cand[dst] = cand[src]
    for key in NORMALIZED_KEYS:
        cand.setdefault(key, None if key not in {"source_ids"} else [])
    cand["field_sources"] = cand.get("field_sources") if isinstance(cand.get("field_sources"), dict) else {}
    return cand


def extract_candidate_variants(parsed_json):
    if not isinstance(parsed_json, dict):
        return [], "none", "discovery payload not a dict", 0

    raw = parsed_json.get("candidate_variants")
    if isinstance(raw, list) and raw:
        cands = [_normalize_candidate(c) for c in raw if isinstance(c, dict)]
        return cands, "candidate_variants", None, len(raw)

    alt_paths = [
        ("variants", parsed_json.get("variants")),
        ("vehicle_variants", parsed_json.get("vehicle_variants")),
        ("results[].candidate_variants", [c for r in (parsed_json.get("results") or []) if isinstance(r, dict) for c in (r.get("candidate_variants") or [])]),
    ]
    for path, value in alt_paths:
        if isinstance(value, list) and value:
            cands = [_normalize_candidate(c) for c in value if isinstance(c, dict)]
            return cands, path, None, len(value)

    generations = parsed_json.get("generations")
    if isinstance(generations, list) and generations:
        flattened = []
        for gen in generations:
            if not isinstance(gen, dict):
                continue
            for var in (gen.get("variants") or []):
                if isinstance(var, dict):
                    flattened.append(_normalize_candidate(_inherit_variant_data(var, gen)))
        if flattened:
            return flattened, "generations[].variants", None, len(flattened)

    return [], "none", "no candidate list found in known paths", 0


def _salvage_candidates_from_raw(raw_text: str):
    if not raw_text or '"candidate_variants"' not in raw_text:
        return None
    try:
        left = raw_text.split('"candidate_variants"', 1)[1]
        start = left.find('[')
        end = left.rfind(']')
        if start == -1 or end == -1 or end <= start:
            return None
        arr = left[start:end+1]
        decoder = __import__('json').JSONDecoder()
        idx = 0
        complete = []
        while idx < len(arr):
            while idx < len(arr) and arr[idx] in ' [\n\t,':
                idx += 1
            if idx >= len(arr) or arr[idx] == ']':
                break
            try:
                obj, next_idx = decoder.raw_decode(arr, idx)
                if isinstance(obj, dict):
                    complete.append(_normalize_candidate(obj))
                idx = next_idx
            except Exception:
                break
        if complete:
            return {"candidate_variants": complete, "sources": []}, True
    except Exception:
        return None
    return None


def run_discovery(seed, market='IL', model_name=None) -> dict:
    prompt = build_discovery_prompt(seed, market)
    res = GeminiClient().grounded_generate_json(prompt=prompt, model_override=model_name)
    if not isinstance(res, dict):
        return {'ok': False, 'data': None, 'error': f'Gemini client returned non-dict: {type(res).__name__}', 'gemini_metadata': {'model': None, 'grounding_requested': True, 'request_attempted': False, 'error': 'non-dict gemini response', 'raw_text': None, 'parsed_json': None, 'parse_error': None}}

    payload = res.get('data') if isinstance(res.get('data'), (dict, list)) else None
    if payload is None:
        payload = res.get('parsed_json') if isinstance(res.get('parsed_json'), (dict, list)) else None
    parse_error = res.get('parse_error')
    if payload is None and res.get('raw_text'):
        payload, fallback_error = parse_json_from_gemini_text(res.get('raw_text'))
        parse_error = parse_error or fallback_error
    salvage_used = False
    dropped_incomplete = False
    if payload is None and res.get('raw_text'):
        salvaged = _salvage_candidates_from_raw(res.get('raw_text'))
        if salvaged:
            payload, dropped_incomplete = salvaged
            salvage_used = True

    if payload is None:
        return {
            'ok': False,
            'data': None,
            'error': 'Failed to parse Gemini discovery JSON',
            'gemini_metadata': {
                'model': res.get('model'), 'grounding_requested': bool(res.get('grounding_requested', True)), 'request_attempted': bool(res.get('request_attempted', True)),
                'error': res.get('error'), 'raw_text': res.get('raw_text'), 'parsed_json': None, 'parse_error': parse_error,
                'discovery_raw_text_debug_available': bool(res.get('raw_text')), 'discovery_parsed_top_level_keys': [],
                'candidate_extraction_path': 'none', 'candidate_extraction_warning': 'parse_failed',
                'raw_candidates_count_before_normalization': 0, 'candidate_variants_count_after_extraction': 0, 'json_salvage_used': salvage_used, 'dropped_incomplete_candidate': dropped_incomplete,
            }
        }

    if isinstance(payload, list):
        payload = {'candidate_variants': payload}

    extracted, extraction_path, extraction_warning, raw_count = extract_candidate_variants(payload)
    sources = payload.get('sources') if isinstance(payload.get('sources'), list) else []
    top_level_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
    data = {'search_queries': payload.get('search_queries') if isinstance(payload.get('search_queries'), list) else [], 'sources': sources, 'candidate_variants': extracted, 'conflicts': payload.get('conflicts') if isinstance(payload.get('conflicts'), list) else [], 'unresolved': bool(payload.get('unresolved', False)), 'unresolved_reason': payload.get('unresolved_reason'), 'field_evidence': payload.get('field_evidence', {})}

    return {'ok': True, 'data': data, 'error': None, 'gemini_metadata': {
        'model': res.get('model'), 'grounding_requested': bool(res.get('grounding_requested', True)), 'request_attempted': bool(res.get('request_attempted', True)),
        'error': res.get('error'), 'raw_text': res.get('raw_text'), 'parsed_json': payload, 'parse_error': parse_error,
        'parse_error_original': res.get('parse_error_original'), 'repair_attempted': bool(res.get('repair_attempted', False)), 'repair_success': bool(res.get('repair_success', False)), 'repaired_raw_text': res.get('repaired_raw_text'),
        'discovery_raw_text_debug_available': bool(res.get('raw_text')), 'discovery_parsed_top_level_keys': top_level_keys,
        'candidate_extraction_path': extraction_path, 'candidate_extraction_warning': extraction_warning,
        'raw_candidates_count_before_normalization': raw_count, 'candidate_variants_count_after_extraction': len(extracted), 'json_salvage_used': salvage_used, 'dropped_incomplete_candidate': dropped_incomplete,
    }}

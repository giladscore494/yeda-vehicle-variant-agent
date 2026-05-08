from agent.prompts import build_discovery_prompt
from tools.gemini_client import GeminiClient


NORMALIZED_KEYS = [
    "year_start", "year_end", "generation", "body_type", "seats",
    "engine", "transmission", "fuel_type", "drivetrain", "trim", "source_urls"
]


def _inherit_variant_data(variant, parent):
    merged = dict(variant if isinstance(variant, dict) else {})
    for key in ("generation", "year_start", "year_end", "source_urls"):
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
        cand.setdefault(key, None if key != "source_urls" else [])
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
        ("model_variants", parsed_json.get("model_variants")),
        ("results[].candidate_variants", [c for r in parsed_json.get("results", []) if isinstance(r, dict) for c in (r.get("candidate_variants") or [])]),
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


def run_discovery(seed, market='IL', model_name=None) -> dict:
    prompt = build_discovery_prompt(seed, market)
    res = GeminiClient().grounded_generate_json(prompt=prompt, model_override=model_name)
    if not isinstance(res, dict):
        return {'ok': False, 'data': None, 'error': f'Gemini client returned non-dict: {type(res).__name__}', 'gemini_metadata': {'model': None, 'grounding_requested': True, 'request_attempted': False, 'error': 'non-dict gemini response', 'raw_text': None, 'parsed_json': None, 'parse_error': None}}

    payload = res.get('parsed_json') if isinstance(res.get('parsed_json'), dict) else {}
    extracted, extraction_path, extraction_warning, raw_count = extract_candidate_variants(payload)
    sources = payload.get('sources') if isinstance(payload.get('sources'), list) else []
    top_level_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
    first_candidate_preview = extracted[0] if extracted else None
    all_candidate_shells_empty = bool(extracted) and all(
        all(c.get(k) in (None, '', [], 'unknown') for k in ('year_start', 'year_end', 'generation', 'body_type', 'engine', 'transmission', 'fuel_type', 'drivetrain'))
        for c in extracted if isinstance(c, dict)
    )
    data_quality_warning = 'discovery_returned_empty_candidate_shells' if all_candidate_shells_empty else None
    data = {'search_queries': payload.get('search_queries') if isinstance(payload.get('search_queries'), list) else [], 'sources': sources, 'candidate_variants': extracted, 'conflicts': payload.get('conflicts') if isinstance(payload.get('conflicts'), list) else [], 'unresolved': bool(payload.get('unresolved', False)), 'unresolved_reason': payload.get('unresolved_reason'), 'field_evidence': payload.get('field_evidence', {})}

    ok = res.get('error') is None and not all_candidate_shells_empty
    return {'ok': ok, 'data': data if ok else None, 'error': res.get('error'), 'gemini_metadata': {
        'model': res.get('model'), 'grounding_requested': bool(res.get('grounding_requested', True)), 'request_attempted': bool(res.get('request_attempted', True)),
        'error': res.get('error'), 'raw_text': res.get('raw_text'), 'parsed_json': payload, 'parse_error': res.get('parse_error'),
        'discovery_raw_text_debug_available': bool(res.get('raw_text')), 'discovery_parsed_top_level_keys': top_level_keys,
        'candidate_extraction_path': extraction_path, 'candidate_extraction_warning': extraction_warning,
        'raw_candidates_count_before_normalization': raw_count, 'candidate_variants_count_after_extraction': len(extracted),
        'candidate_variants_count_raw': raw_count, 'sources_count_raw': len(sources), 'first_candidate_preview': first_candidate_preview,
        'data_quality_warning': data_quality_warning,
    }}

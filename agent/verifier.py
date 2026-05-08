from tools.gemini_client import GeminiClient
from core.schemas import VerificationStatus, Confidence
from agent.prompts import build_verification_prompt

CRITICAL_FIELDS = ("body_type", "seats", "engine", "transmission", "fuel_type", "drivetrain", "generation", "year_start", "year_end")


def _unknown_default(reason='Field omitted; defaulted to unknown.'):
    return {'value': None, 'status': VerificationStatus.unknown.value, 'confidence': Confidence.low.value, 'sources_count': 0, 'source_ids': [], 'used_in_compare': False, 'reason': reason}


def _normalize_fields(resp):
    fv = resp.get('field_verifications') if isinstance(resp, dict) else None
    fv = fv if isinstance(fv, dict) else {}
    for field in CRITICAL_FIELDS:
        entry = fv.get(field)
        if not isinstance(entry, dict):
            fv[field] = _unknown_default()
            continue
        entry.setdefault('value', None)
        entry.setdefault('status', VerificationStatus.unknown.value)
        entry.setdefault('confidence', Confidence.low.value)
        entry.setdefault('sources_count', 0)
        entry.setdefault('source_ids', [])
        entry.setdefault('used_in_compare', False)
        entry.setdefault('reason', 'Field omitted; defaulted to unknown.')
    resp['field_verifications'] = fv
    return resp


def verify_candidates_batch(candidates, sources, model_name=None, cost_settings=None) -> dict:
    prompt = build_verification_prompt(candidates, sources)
    r = GeminiClient().generate_json(prompt, strong=True, model_override=model_name)
    if not r.get('ok'):
        return {
            'ok': False,
            'variant_verifications': [
                {
                    'candidate_index': i,
                    'field_verifications': {k: _unknown_default('Verification failed or field omitted; defaulted to unknown.') for k in CRITICAL_FIELDS},
                    'overall_status': 'unverified',
                    'overall_confidence': 'low',
                    'blocked_fields': list(CRITICAL_FIELDS),
                    'notes': [r.get('error')],
                }
                for i, _ in enumerate(candidates or [])
            ],
        }
    data = r.get('data', r)
    vv = data.get('variant_verifications', []) if isinstance(data, dict) else []
    out = []
    for i, item in enumerate(vv):
        item = item if isinstance(item, dict) else {}
        item.setdefault('candidate_index', i)
        item = _normalize_fields({'field_verifications': item.get('field_verifications', {})}) | {
            'overall_status': item.get('overall_status', 'unverified'),
            'overall_confidence': item.get('overall_confidence', 'low'),
            'blocked_fields': item.get('blocked_fields', []),
            'notes': item.get('notes', []),
            'candidate_index': item.get('candidate_index', i),
        }
        out.append(item)
    return {'ok': True, 'variant_verifications': out}

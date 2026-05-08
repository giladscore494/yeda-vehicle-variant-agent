from agent.prompts import build_discovery_prompt
from tools.gemini_client import GeminiClient


def run_discovery(seed, market='IL') -> dict:
    prompt = build_discovery_prompt(seed, market)
    res = GeminiClient().grounded_generate_json(prompt=prompt)
    if not res.get('ok'):
        return {
            'ok': False,
            'error': res.get('error'),
            'gemini_metadata': res,
            'search_queries': [],
            'sources': [],
            'candidate_variants': [],
            'conflicts': [],
            'unresolved': True,
            'unresolved_reason': res.get('error'),
        }

    data = res.get('data') or {}
    if isinstance(data, dict):
        data['provider'] = res.get('provider')
        data['model'] = res.get('model')
        data['grounding_requested'] = True
        data['request_attempted'] = True
    return data

import argparse
import json

from agent.runner import run_batch

p = argparse.ArgumentParser()
p.add_argument('--limit', type=int, default=5)
p.add_argument('--make-filter')
p.add_argument('--market', default='IL')
p.add_argument('--mock', action='store_true')
p.add_argument('--model-mode', choices=['fast', 'strong', 'auto'], default='auto')
a = p.parse_args()
print(json.dumps(run_batch(limit=a.limit, make_filter=a.make_filter, market=a.market, force_mock=a.mock, model_mode=a.model_mode), ensure_ascii=False, indent=2))

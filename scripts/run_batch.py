import argparse, json
from agent.runner import run_batch
p=argparse.ArgumentParser(); p.add_argument('--limit',type=int,default=5); p.add_argument('--make-filter'); p.add_argument('--market',default='IL'); p.add_argument('--mock',action='store_true')
a=p.parse_args(); print(json.dumps(run_batch(a.limit,a.make_filter,a.market,a.mock),ensure_ascii=False,indent=2))

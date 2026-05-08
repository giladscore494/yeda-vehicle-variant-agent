import argparse, json
from agent.runner import run_single_model
p=argparse.ArgumentParser(); p.add_argument('--make',required=True); p.add_argument('--model',required=True); p.add_argument('--market',default='IL'); p.add_argument('--mock',action='store_true')
a=p.parse_args(); print(json.dumps(run_single_model(a.make,a.model,market=a.market,force_mock=a.mock),ensure_ascii=False,indent=2))

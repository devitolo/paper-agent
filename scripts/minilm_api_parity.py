#!/usr/bin/env python3
"""Real-model acceptance harness. Requires offline assets and an approved API.

No downloads, provider calls or application database access. Prints no input text.
"""
import argparse
import json
import math
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from paper_agents.curator_interest import score_interest_fit
from paper_agents.minilm_eval import MiniLMScorer, MODEL_DIR
from paper_agents.minilm_http import HTTPMiniLMScorer

INTERESTS=['incident diagnosis and observability','agent reliability and recovery','enterprise AI governance']
CASES=[
    {'title':'Incident diagnosis','abstract':'This study investigates incident diagnosis using logs, metrics and traces. Controlled evaluations compare recovery and root cause identification. ' * 3},
    {'title':'Agent reliability','abstract':'We examine tool using agents and recovery from failed actions. The evaluation measures coordination failures and the effect of retained context. ' * 3},
    {'title':'Long input','abstract':'This study measures reliability and diagnosis in deployed systems. ' * 700},
    {'title':'Missing','abstract':None},
    {'title':'Invalid','abstract':'short'},
    {'title':'','abstract':'This study investigates reliable incident diagnosis with a controlled evaluation of infrastructure dependencies. ' * 2},
]


def compare(local,remote,tolerance=1e-6):
    left=[score_interest_fit(c,INTERESTS,local) for c in CASES]
    right=[score_interest_fit(c,INTERESTS,remote) for c in CASES]
    max_delta=0.0
    for a,b in zip(left,right):
        if a['status']!=b['status'] or a['status']=='unavailable':raise AssertionError('status mismatch or unavailable')
        if a['status']!='scored':
            if a!=b:raise AssertionError('invalid metadata behavior differs')
            continue
        if a['matched_interest']!=b['matched_interest']:raise AssertionError('max aggregation changed')
        for x,y in zip(a['per_interest'],b['per_interest']):
            max_delta=max(max_delta,abs(x['score']-y['score']))
            if not math.isclose(x['score'],y['score'],rel_tol=tolerance,abs_tol=tolerance):
                raise AssertionError('numeric parity tolerance exceeded')
        if not math.isclose(a['score'],b['score'],rel_tol=tolerance,abs_tol=tolerance):raise AssertionError('max score differs')
    def order(results):
        return sorted(range(len(results)),key=lambda i:results[i]['score'] if results[i]['score'] is not None else float('-inf'),reverse=True)
    if order(left)!=order(right):raise AssertionError('ordering differs')
    return {'cases':len(CASES),'interests':len(INTERESTS),'max_absolute_delta':max_delta,
            'tolerance':tolerance,'order_equal':True,'invalid_metadata_equal':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--model-dir',type=Path,default=MODEL_DIR)
    args=parser.parse_args()
    local=MiniLMScorer(args.model_dir)
    remote=HTTPMiniLMScorer(args.url)
    print(json.dumps(compare(local.score_pair,remote.score_pair),sort_keys=True))

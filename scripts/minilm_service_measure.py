#!/usr/bin/env python3
"""Bounded synthetic API-only timing. No embedded model, DB, Docker or SSH access."""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from paper_agents.minilm_http import HTTPMiniLMScorer

SHORT='This study examines reliable incident diagnosis with logs, metrics and traces and evaluates recovery across controlled experiments. '
LONG=SHORT*350


def measure(client,clock=time.monotonic):
    started=clock()
    rows=[]
    # At most seven requests, no automatic retry; longest batch is within API32-pair cap.
    cases=[('first_request',SHORT,1),('short_1',SHORT,1),('short_2',SHORT,1),
           ('long_1',LONG,1),('long_2',LONG,1),('batch_1',SHORT,32),('batch_2',SHORT,32)]
    result={'kind':'service_only_synthetic_timing','complete':False,'requests':rows,
            'resource_metrics':'capture service cgroup/Docker metrics separately',
            'startup_time':'not measured; first_request is not model loading time'}
    for name,abstract,count in cases:
        if clock()-started>=60:
            result['stop_reason']='60_second_admission_budget'
            break
        before=clock()
        try:
            scores=client.score_pairs([dict(title='Synthetic reliability study',abstract=abstract,
                                           interest='incident diagnosis and recovery')]*count)
        except Exception as exc:
            result['stop_reason']=type(exc).__name__
            rows.append({'case':name,'pairs':count,'seconds':clock()-before,'ok':False})
            break
        rows.append({'case':name,'pairs':len(scores),'seconds':clock()-before,'ok':True})
    else:
        result['complete']=True
    result['elapsed_seconds']=clock()-started
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    args=parser.parse_args()
    report=measure(HTTPMiniLMScorer(args.url,timeout=10))
    print(json.dumps(report,sort_keys=True))
    raise SystemExit(0 if report['complete'] else 1)

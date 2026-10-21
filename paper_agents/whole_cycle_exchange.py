"""Guarded whole-cycle Mini to AWS exchange adapter."""
from __future__ import annotations
import json, os, pathlib, uuid, argparse
from .sync_exchange import apply_input_snapshot, encoded
from .sync_worker import request, results_for_cycle
from .sync_exchange import connect_db

PIPELINE_TABLES = ('workflow_cycles','scout_runs','curator_runs','curator_evaluations','recommendations','artifacts')

def baseline(db_path):
    with connect_db(db_path) as c:
        return {t: c.execute(f'SELECT COALESCE(MAX(id),0) FROM {t}').fetchone()[0] for t in PIPELINE_TABLES}

def pull(db_path, root, state, snapshot):
    state = pathlib.Path(state)
    if list(state.glob('pipeline-outbox*.json')): raise ValueError('pending outbox blocks snapshot replacement')
    apply_input_snapshot(db_path, snapshot, root)
    state.mkdir(parents=True, exist_ok=True)
    for name, value in (('baseline.json', baseline(db_path)), ('input-snapshot.json', snapshot)):
        tmp=state/(name+'.tmp'); tmp.write_bytes(encoded(value)); tmp.replace(state/name)

def export_delta(db_path, root, state):
    base=json.loads((pathlib.Path(state)/'baseline.json').read_text())
    with connect_db(db_path) as c:
        cycles=[dict(r) for r in c.execute('SELECT * FROM workflow_cycles WHERE id>? ORDER BY id',(base['workflow_cycles'],))]
        rows={t:[dict(r) for r in c.execute(f'SELECT * FROM {t} WHERE id>? ORDER BY id',(base[t],))] for t in PIPELINE_TABLES}
        cycle_ids=[r['id'] for r in cycles]
        keys=set()
        if cycle_ids:
            marks=','.join('?'*len(cycle_ids)); keys={r[0] for r in c.execute(f'SELECT DISTINCT p.canonical_key FROM papers p JOIN scout_candidates sc ON sc.paper_id=p.id JOIN scout_runs sr ON sr.id=sc.scout_run_id WHERE sr.workflow_cycle_id IN ({marks}) UNION SELECT DISTINCT p.canonical_key FROM papers p JOIN curator_evaluations e ON e.paper_id=p.id JOIN curator_runs cr ON cr.id=e.curator_run_id WHERE cr.workflow_cycle_id IN ({marks})',cycle_ids+cycle_ids)}
        papers=[dict(r) for r in c.execute('SELECT * FROM papers') if r['canonical_key'] in keys]
        sources=[dict(r) for r in c.execute('SELECT ps.*,p.canonical_key FROM paper_sources ps JOIN papers p ON p.id=ps.paper_id') if r['canonical_key'] in keys]
        artifacts=[dict(r) for r in c.execute("SELECT a.*,p.canonical_key FROM artifacts a JOIN papers p ON p.id=a.paper_id WHERE a.artifact_type='triage_summary'") if r['canonical_key'] in keys]
    results=[]
    for cycle in cycles: results.extend(results_for_cycle(db_path, root, cycle['id']))
    for row in artifacts:
        path=pathlib.Path(row['path']); path=path if path.is_absolute() else pathlib.Path(root)/path
        row['content']=json.loads(path.read_text()) if path.exists() else None
        if not isinstance(row['content'],dict): raise ValueError('triage summary artifact content must be an object')
    evaluations=[]; recommendations=[]
    with connect_db(db_path) as c:
        for cycle in cycles:
            key=f"mini-cycle-{cycle['id']}"
            evaluations += [dict(r)|{'cycle_key':key} for r in c.execute('SELECT e.*,p.canonical_key FROM curator_evaluations e JOIN curator_runs cr ON cr.id=e.curator_run_id JOIN papers p ON p.id=e.paper_id WHERE cr.workflow_cycle_id=?',(cycle['id'],))]
            recommendations += [dict(r)|{'cycle_key':key} for r in c.execute('SELECT r.*,p.canonical_key FROM recommendations r JOIN curator_runs cr ON cr.id=r.curator_run_id JOIN papers p ON p.id=r.paper_id WHERE cr.workflow_cycle_id=?',(cycle['id'],))]
    return {'schema':1,'normalized':{'papers':papers,'paper_sources':sources,'artifacts':artifacts,'evaluations':evaluations,'recommendations':recommendations},'results':[],'cycle_count':len(cycles)}

def push(db_path, root, state, url):
    state=pathlib.Path(state); outbox=state/'pipeline-outbox.json'
    if not outbox.exists():
        snapshot=json.loads((state/'input-snapshot.json').read_text())
        payload=export_delta(db_path, root, state)
        seq=int(json.loads((state/'sequence.json').read_text())['sequence'])+1 if (state/'sequence.json').exists() else 1
        payload.update(input_revision=snapshot['revision'],producer_id=os.environ.get('PAPER_AGENT_SYNC_PRODUCER','mini-production'),environment=os.environ.get('PAPER_AGENT_SYNC_ENVIRONMENT','production'),run_sequence=seq,batch_id=uuid.uuid4().hex)
        (state/'sequence.json').write_text(json.dumps({'sequence':seq})); outbox.write_bytes(encoded(payload))
    payload=json.loads(outbox.read_text()); key=payload['batch_id']
    receipt=request(url,'POST',encoded(payload),os.environ['PAPER_AGENT_SYNC_SHARED_SECRET'],'/sync/pipeline-results',key)
    if receipt.get('status')!='applied' or receipt.get('batch_key')!=key: raise ValueError('invalid pipeline receipt')
    outbox.rename(state/f'published-{key}.json'); return receipt

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument('command',choices=('pull','push')); p.add_argument('--db-path',required=True,type=pathlib.Path); p.add_argument('--root',required=True,type=pathlib.Path); p.add_argument('--state-dir',required=True,type=pathlib.Path); p.add_argument('--aws-url',required=True)
    a=p.parse_args(argv)
    if a.command=='pull': pull(a.db_path,a.root,a.state_dir,request(a.aws_url,'GET',b'',os.environ['PAPER_AGENT_SYNC_SHARED_SECRET'],'/sync/snapshot'))
    else: print(json.dumps(push(a.db_path,a.root,a.state_dir,a.aws_url),sort_keys=True))
    return 0

if __name__=='__main__': raise SystemExit(main())

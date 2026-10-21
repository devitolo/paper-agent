"""Guarded whole-cycle Mini to AWS exchange adapter."""
from __future__ import annotations
import json, os, pathlib, uuid
from .sync_exchange import apply_input_snapshot, encoded
from .sync_worker import request, results_for_cycle
from .sync_exchange import connect_db

PIPELINE_TABLES = ('workflow_cycles','scout_runs','curator_runs','curator_evaluations','recommendations','artifacts')

def baseline(db_path):
    with connect_db(db_path) as c:
        return {t: c.execute(f'SELECT COALESCE(MAX(id),0) FROM {t}').fetchone()[0] for t in PIPELINE_TABLES}

def pull(db_path, root, state, snapshot):
    apply_input_snapshot(db_path, snapshot, root)
    state = pathlib.Path(state); state.mkdir(parents=True, exist_ok=True)
    (state/'baseline.json').write_text(json.dumps(baseline(db_path), sort_keys=True))
    (state/'input-snapshot.json').write_bytes(encoded(snapshot))

def export_delta(db_path, root, state):
    base=json.loads((pathlib.Path(state)/'baseline.json').read_text())
    with connect_db(db_path) as c:
        cycles=[dict(r) for r in c.execute('SELECT * FROM workflow_cycles WHERE id>? ORDER BY id',(base['workflow_cycles'],))]
        rows={t:[dict(r) for r in c.execute(f'SELECT * FROM {t} WHERE id>? ORDER BY id',(base[t],))] for t in PIPELINE_TABLES}
    results=[]
    for cycle in cycles: results.extend(results_for_cycle(db_path, root, cycle['id']))
    return {'schema':1,'pipeline_delta':rows,'results':results,'cycle_count':len(cycles)}

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

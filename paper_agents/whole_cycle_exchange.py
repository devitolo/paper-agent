"""Disabled-by-default exchange around one complete Mini daily cycle.

AWS owns the serving database/config/profile. Mini retains only its source traversal
state, PDFs, secrets, logs and runtime identity. No profile proposal is published.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid

from .sync_backup import download_backup, validate_backup
from .sync_exchange import encoded, connect_db
from .sync_worker import atomic_json, lock, request

CURSOR_TABLES = tuple(f'{source}_{suffix}' for source in
    ('arxiv','openalex','semantic_scholar','core')
    for suffix in ('search_state',))
SECTIONS = ('papers','paper_sources','artifacts','evaluations','recommendations')


def baseline(db_path):
    with connect_db(db_path) as c:
        return {'workflow_cycles':c.execute('SELECT COALESCE(MAX(id),0) FROM workflow_cycles').fetchone()[0]}


def pull(db_path, root, state, backup):
    """Restore a validated compressed-transfer backup into the existing DB inode.

    Caller owns the scheduled-job lease. SQLite backup commits the staged DB
    atomically; WAL readers are coordinated by SQLite. Recovery marker blocks jobs
    if file installation/DB replacement or baseline publication is interrupted.
    """
    db_path, root, state, backup = map(Path,(db_path,root,state,backup))
    if (state/'pipeline-outbox.json').exists():
        raise ValueError('pending outbox blocks snapshot replacement; retry push first')
    if (state/'active-cycle.json').exists():
        raise ValueError('unfinished cycle requires recovery; snapshot replacement refused')
    if not validate_backup(backup):
        raise ValueError('invalid complete snapshot')
    if not db_path.is_file() or db_path.is_symlink():
        raise ValueError('existing intended Mini database required')
    manifest=json.loads((backup/'manifest.json').read_text())
    manifest_revision=manifest.get('snapshot_revision')
    revision=manifest_revision or hashlib.sha256((backup/'manifest.json').read_bytes()).hexdigest()
    state.mkdir(parents=True,exist_ok=True)
    cycle={'batch_id':uuid.uuid4().hex,'input_revision':revision,'stage':'restoring'}
    atomic_json(state/'active-cycle.json',cycle)
    with tempfile.TemporaryDirectory(dir=state,prefix='restore-') as directory:
        staged=Path(directory)/'paper_agent.db'
        shutil.copy2(backup/'paper_agent.db',staged)
        # Retain prior Mini DB for rollback and local page-disposition audit. Those
        # audit rows reference old scout IDs and must not enter the AWS snapshot.
        with closing(sqlite3.connect(db_path)) as current, closing(sqlite3.connect(state/('mini-before-'+cycle['batch_id']+'.db'))) as saved:
            current.backup(saved)
        with closing(sqlite3.connect(staged)) as target, closing(sqlite3.connect(db_path)) as current:
            # Preserve only Mini-owned traversal progress, using exact schema parity.
            for table in CURSOR_TABLES:
                schema=current.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()
                if not schema: continue
                dest=target.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()
                if dest!=schema: raise ValueError('cursor schema mismatch: '+table)
                values=current.execute('SELECT * FROM '+table).fetchall()
                target.execute('DELETE FROM '+table)
                if values:
                    target.executemany('INSERT INTO '+table+' VALUES('+','.join('?'*len(values[0]))+')',values)
            target.commit()
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or target.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('restored snapshot integrity failed')
            # Serving files only; no peer credentials, PDFs, runtime files or cursors.
            for name in manifest['files']:
                if name=='paper_agent.db': continue
                src=backup/name; dest=root/name
                if dest.is_symlink() or any(p.is_symlink() for p in dest.parents):
                    raise ValueError('aliased restore destination')
                dest.parent.mkdir(parents=True,exist_ok=True)
                tmp=dest.with_name(dest.name+'.sync-restore')
                shutil.copyfile(src,tmp); os.replace(tmp,dest)
            # SQLite's backup protocol provides atomic replacement, including WAL.
            target.backup(current)
    cycle.update(stage='running',baseline=baseline(db_path))
    atomic_json(state/'active-cycle.json',cycle)
    return {'status':'snapshot-restored','input_revision':revision}


def export_delta(db_path, root, state):
    root,state=Path(root),Path(state)
    cycle=json.loads((state/'active-cycle.json').read_text())
    if cycle['stage']!='running': raise ValueError('snapshot restoration incomplete')
    with connect_db(db_path) as c:
        c.row_factory=sqlite3.Row
        ids=[r[0] for r in c.execute('SELECT id FROM workflow_cycles WHERE id>? ORDER BY id',(cycle['baseline']['workflow_cycles'],))]
        normalized={s:[] for s in SECTIONS}
        if not ids: return {'schema':1,'normalized':normalized,'results':[]}
        marks=','.join('?' for _ in ids)
        keys={r[0] for r in c.execute(f'''SELECT p.canonical_key FROM scout_candidates sc
            JOIN scout_runs sr ON sr.id=sc.scout_run_id JOIN papers p ON p.id=sc.paper_id
            WHERE sr.workflow_cycle_id IN ({marks}) UNION SELECT p.canonical_key FROM curator_evaluations e
            JOIN curator_runs cr ON cr.id=e.curator_run_id JOIN papers p ON p.id=e.paper_id
            WHERE cr.workflow_cycle_id IN ({marks})''',ids+ids)}
        normalized['papers']=[{k:r[k] for k in r.keys() if k!='id'} for r in c.execute('SELECT * FROM papers') if r['canonical_key'] in keys]
        normalized['paper_sources']=[{k:r[k] for k in r.keys() if k not in ('id','paper_id')} for r in c.execute('SELECT ps.*,p.canonical_key FROM paper_sources ps JOIN papers p ON p.id=ps.paper_id') if r['canonical_key'] in keys]
        summary_seen=set()
        for row in c.execute("SELECT a.*,p.canonical_key FROM artifacts a JOIN papers p ON p.id=a.paper_id WHERE a.artifact_type='triage_summary'"):
            if row['canonical_key'] not in keys: continue
            path=Path(row['path']); path=path if path.is_absolute() else root/path
            if not path.resolve().is_relative_to(root.resolve()): raise ValueError('summary outside mounted app root')
            content=json.loads(path.read_text())
            if not isinstance(content,dict): raise ValueError('summary must contain an object')
            identity=(row['canonical_key'],hashlib.sha256(encoded(content)).hexdigest())
            if identity in summary_seen: continue
            summary_seen.add(identity)
            normalized['artifacts'].append({'canonical_key':row['canonical_key'],'artifact_type':'triage_summary','content':content,'model':row['model'],'metadata_json':row['metadata_json'],'summary_hash':hashlib.sha256(encoded(content)).hexdigest()})
        for section,table in (('evaluations','curator_evaluations'),('recommendations','recommendations')):
            for row in c.execute(f'SELECT r.*,p.canonical_key,cr.workflow_cycle_id FROM {table} r JOIN curator_runs cr ON cr.id=r.curator_run_id JOIN papers p ON p.id=r.paper_id WHERE cr.workflow_cycle_id IN ({marks}) ORDER BY r.id',ids):
                allowed=('score','rationale','matched_signals_json','quality_threshold_met') if section=='evaluations' else ('recommendation_order','rationale')
                normalized[section].append({k:row[k] for k in allowed}|{'canonical_key':row['canonical_key'],'cycle_key':f"{cycle['batch_id']}:cycle:{row['workflow_cycle_id']}",'run_key':f"{cycle['batch_id']}:curator:{row['curator_run_id']}"})
    return {'schema':1,'normalized':normalized,'results':[]}


def push(db_path, root, state, url, *, transport=request):
    state=Path(state); outbox=state/'pipeline-outbox.json'
    if not outbox.exists():
        cycle=json.loads((state/'active-cycle.json').read_text())
        payload=export_delta(db_path,root,state)
        seq=json.loads((state/'sequence.json').read_text())['sequence']+1 if (state/'sequence.json').exists() else 1
        payload.update(input_revision=cycle['input_revision'],producer_id=os.environ.get('PAPER_AGENT_SYNC_PRODUCER','mini-production'),environment=os.environ.get('PAPER_AGENT_SYNC_ENVIRONMENT','production'),run_sequence=seq,batch_id=cycle['batch_id'])
        # Persist identity and exact payload before any network request.
        atomic_json(outbox,payload)
    payload=json.loads(outbox.read_text()); key=payload['batch_id']
    receipt=transport(url,'POST',encoded(payload),os.environ['PAPER_AGENT_SYNC_SHARED_SECRET'],'/sync/pipeline-results',key)
    if receipt.get('status')!='applied' or receipt.get('batch_key')!=key: raise ValueError('invalid pipeline receipt')
    atomic_json(state/('receipt-'+key+'.json'),receipt)
    atomic_json(state/'sequence.json',{'sequence':payload['run_sequence']})
    # Clear active first: an interruption retains outbox and allows identical retry.
    (state/'active-cycle.json').unlink(missing_ok=True)
    outbox.rename(state/f'published-{key}.json')
    return receipt


def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=('pull','push'))
    p.add_argument('--db-path',required=True,type=Path); p.add_argument('--root',required=True,type=Path)
    p.add_argument('--state-dir',required=True,type=Path); p.add_argument('--aws-url',required=True)
    a=p.parse_args(argv)
    if os.environ.get('PAPER_AGENT_SYNC_ENABLED')!='1' or os.environ.get('PAPER_AGENT_SYNC_EXPLICIT_PRODUCTION')!='1':
        raise ValueError('explicit exchange activation required')
    if not all(v.is_absolute() for v in (a.db_path,a.root,a.state_dir)) or a.db_path.resolve()!=a.root.resolve()/'data/paper_agent.db':
        raise ValueError('exchange DB must match mounted pipeline data/paper_agent.db')
    from .migration_lifecycle import lease
    lifecycle=Path(os.environ.get('PAPER_AGENT_LIFECYCLE_DIR','/runtime-control'))
    with lock(a.state_dir), lease(lifecycle), lease(lifecycle,exclusive=True,name='.scheduled-job.lock'):
        if a.command=='pull':
            if (a.state_dir/'pipeline-outbox.json').exists() or (a.state_dir/'active-cycle.json').exists():
                raise ValueError('unfinished cycle blocks pull; recover/publish first')
            backup=download_backup(a.aws_url,a.state_dir/'snapshots',endpoint='/sync/backup')
            result=pull(a.db_path,a.root,a.state_dir,backup)
        else: result=push(a.db_path,a.root,a.state_dir,a.aws_url)
    print(json.dumps(result,sort_keys=True)); return 0

if __name__=='__main__': raise SystemExit(main())

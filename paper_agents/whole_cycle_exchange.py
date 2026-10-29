"""Disabled-by-default exchange around one complete Mini daily cycle.

AWS owns the serving database/config/profile. Mini retains only its source traversal
state, PDFs, secrets, logs and runtime identity. No profile proposal is published.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid
from urllib.error import HTTPError

from .sync_backup import download_backup, download_routine_snapshot, validate_backup
from .sync_exchange import apply_routine_snapshot, encoded, connect_db
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
    if not isinstance(manifest_revision, str) or len(manifest_revision) != 64 or any(ch not in '0123456789abcdef' for ch in manifest_revision):
        raise ValueError('snapshot revision required')
    revision=manifest_revision
    state.mkdir(parents=True,exist_ok=True)
    cycle={'batch_id':uuid.uuid4().hex,'input_revision':revision,'stage':'restoring','started_at':datetime.now(timezone.utc).isoformat()}
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


def pull_routine(db_path, root, state, snapshot):
    """Apply a validated JSON routine snapshot while preserving Mini cursors."""
    db_path, root, state = map(Path, (db_path, root, state))
    if (state/'pipeline-outbox.json').exists() or (state/'active-cycle.json').exists():
        raise ValueError('unfinished cycle blocks pull; recover/publish first')
    revision = snapshot['revision']; state.mkdir(parents=True, exist_ok=True)
    cycle = {'batch_id': uuid.uuid4().hex, 'input_revision': revision, 'stage': 'restoring', 'started_at': datetime.now(timezone.utc).isoformat()}
    atomic_json(state/'active-cycle.json', cycle)
    file_state = {name: (root / name).read_bytes() for name in ('config/topics.yaml', 'config/industry.json', 'data/profile.json') if (root / name).is_file()}
    saved_path = state / ('mini-before-' + cycle['batch_id'] + '.db')
    with closing(sqlite3.connect(db_path)) as current, closing(sqlite3.connect(saved_path)) as saved:
        current.backup(saved)
    try:
        apply_routine_snapshot(db_path, snapshot, root)
    except Exception:
        with closing(sqlite3.connect(saved_path)) as saved, closing(sqlite3.connect(db_path)) as current:
            saved.backup(current)
        for name, value in file_state.items():
            target = root / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(value)
        raise
    cycle.update(stage='running', baseline=baseline(db_path)); atomic_json(state/'active-cycle.json', cycle)
    return {'status': 'routine-snapshot-restored', 'input_revision': revision}


def export_delta(db_path, root, state):
    root,state=Path(root),Path(state)
    cycle=json.loads((state/'active-cycle.json').read_text())
    if cycle['stage']!='running': raise ValueError('snapshot restoration incomplete')
    with connect_db(db_path) as c:
        c.row_factory=sqlite3.Row
        ids=[r[0] for r in c.execute('SELECT id FROM workflow_cycles WHERE id>? ORDER BY id',(cycle['baseline']['workflow_cycles'],))]
        normalized={s:[] for s in SECTIONS}
        from .operational_evidence import collect
        operational=collect(c,root,cycle)
        if not ids: return {'schema':1,'normalized':normalized,'results':[],'operational':operational}
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
    return {'schema':1,'normalized':normalized,'results':[],'operational':operational}


def push(db_path, root, state, url, *, transport=request):
    state=Path(state); outbox=state/'pipeline-outbox.json'
    if not outbox.exists():
        cycle=json.loads((state/'active-cycle.json').read_text())
        payload=export_delta(db_path,root,state)
        seq=json.loads((state/'sequence.json').read_text())['sequence']+1 if (state/'sequence.json').exists() else 1
        payload.update(input_revision=cycle['input_revision'],producer_id=os.environ.get('PAPER_AGENT_SYNC_PRODUCER','mini-production'),environment=os.environ.get('PAPER_AGENT_SYNC_ENVIRONMENT','production'),run_sequence=seq,batch_id=cycle['batch_id'])
        # Persist identity and exact payload before any network request.
        atomic_json(outbox,payload)
    return deliver_outbox(state,url,outbox,clear_active=True,transport=transport)


def deliver_outbox(state,url,outbox,*,clear_active=False,transport=request):
    payload=json.loads(outbox.read_text()); key=payload['batch_id']
    receipt=transport(url,'POST',encoded(payload),os.environ['PAPER_AGENT_SYNC_SHARED_SECRET'],'/sync/pipeline-results',key)
    if receipt.get('status')!='applied' or receipt.get('batch_key')!=key: raise ValueError('invalid pipeline receipt')
    if payload.get('operational') is not None and receipt.get('operational_version')!=1:
        raise ValueError('AWS did not acknowledge operational evidence; outbox retained')
    atomic_json(state/('receipt-'+key+'.json'),receipt)
    atomic_json(state/'sequence.json',{'sequence':payload['run_sequence']})
    if clear_active:
        (state/'active-cycle.json').unlink(missing_ok=True)
    prefix='published-' if clear_active else 'health-published-'
    outbox.rename(state/(prefix+key+'.json'))
    return receipt


def publish_health(db_path,root,state,url,*,transport=request):
    """Publish actual retained seven-day facts only; never run or restore discovery."""
    from .operational_evidence import collect
    state=Path(state);state.mkdir(parents=True,exist_ok=True)
    if (state/'pipeline-outbox.json').exists() or (state/'active-cycle.json').exists():
        raise ValueError('active pipeline blocks operational publication')
    outbox=state/'health-outbox.json'
    if not outbox.exists():
        snapshot=transport(url,'GET',b'',os.environ['PAPER_AGENT_SYNC_SHARED_SECRET'],'/sync/input')
        since=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
        with connect_db(db_path) as c:
            c.row_factory=sqlite3.Row
            facts=collect(c,root,{'baseline':{'workflow_cycles':0},'started_at':since})
        sequence=json.loads((state/'sequence.json').read_text())['sequence'] if (state/'sequence.json').exists() else 0
        payload={'schema':1,'results':[],'normalized':{section:[] for section in SECTIONS},
                 'operational':facts,'input_revision':snapshot['revision'],
                 'producer_id':os.environ.get('PAPER_AGENT_SYNC_PRODUCER','mini-production'),
                 'environment':os.environ.get('PAPER_AGENT_SYNC_ENVIRONMENT','production'),
                 'run_sequence':sequence+1,'batch_id':'health-'+uuid.uuid4().hex}
        atomic_json(outbox,payload)
    return deliver_outbox(state,url,outbox,transport=transport)


def recover(state, reason='source failure'):
    """Finalize an interrupted source cycle without deleting a pending outbox."""
    state = Path(state)
    marker = state / 'active-cycle.json'
    if not marker.exists():
        atomic_json(state / 'recovery-status.json', {'status': 'no-op', 'reason': reason})
        return {'status': 'no-op'}
    cycle = json.loads(marker.read_text())
    batch = cycle.get('batch_id', uuid.uuid4().hex)
    suffix = 'pending-outbox' if (state / 'pipeline-outbox.json').exists() else 'abandoned'
    target = state / f'cycle-{suffix}-{batch}.json'
    marker.replace(target)
    result = {'status': 'recovered', 'cycle_file': target.name, 'outbox_pending': suffix == 'pending-outbox', 'reason': reason}
    atomic_json(state / 'recovery-status.json', result)
    return result


def quarantine_stale_outbox(state, error):
    """Preserve an outbox rejected for an input revision AWS no longer knows.

    A snapshot replacement can legitimately retire the revision that an
    interrupted batch used.  Keep the exact payload for audit/reconciliation,
    but remove it from the blocking retry slot so the next source can pull a
    fresh snapshot.  This is limited to the server's 422 response.
    """
    state = Path(state)
    outbox = state / 'pipeline-outbox.json'
    if not outbox.exists() or getattr(error, 'code', None) != 422:
        return None
    detail = str(getattr(error, 'reason', ''))
    try:
        detail += ' ' + error.read().decode('utf-8', 'replace')
    except (AttributeError, OSError):
        pass
    if 'unknown input revision' not in detail.lower():
        return None
    payload = json.loads(outbox.read_text())
    key = str(payload.get('batch_id', uuid.uuid4().hex))
    target = state / f'rejected-outbox-{key}.json'
    outbox.replace(target)
    result = {
        'status': 'stale-outbox-quarantined',
        'outbox_file': target.name,
        'batch_id': key,
        'input_revision': payload.get('input_revision', 'unknown'),
        'reason': 'AWS rejected the batch input revision (HTTP 422); payload preserved for reconciliation',
    }
    atomic_json(state / 'recovery-status.json', result)
    return result


def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=('pull','push','retry','recover','publish-health'))
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
        if a.command=='publish-health':
            result=publish_health(a.db_path,a.root,a.state_dir,a.aws_url)
        elif a.command=='recover':
            result=recover(a.state_dir, 'scheduled source failure')
        elif a.command=='retry':
            if (a.state_dir/'health-outbox.json').exists():
                publish_health(a.db_path,a.root,a.state_dir,a.aws_url)
            if not (a.state_dir/'pipeline-outbox.json').exists():
                result={'status':'no-pending-outbox'}
            else:
                try:
                    result=push(a.db_path,a.root,a.state_dir,a.aws_url)
                except HTTPError as error:
                    result=quarantine_stale_outbox(a.state_dir,error)
                    if result is None:
                        raise
        elif a.command=='pull':
            if any((a.state_dir/name).exists() for name in ('pipeline-outbox.json','active-cycle.json','health-outbox.json')):
                raise ValueError('unfinished cycle blocks pull; recover/publish first')
            if os.environ.get('PAPER_AGENT_ROUTINE_SYNC') == '1':
                result=pull_routine(a.db_path, a.root, a.state_dir, download_routine_snapshot(a.aws_url))
            else:
                backup=download_backup(a.aws_url,a.state_dir/'snapshots',endpoint='/sync/backup')
                try:
                    result=pull(a.db_path,a.root,a.state_dir,backup)
                except Exception:
                    shutil.rmtree(backup, ignore_errors=True)
                    raise
        else: result=push(a.db_path,a.root,a.state_dir,a.aws_url)
    print(json.dumps(result,sort_keys=True)); return 0

if __name__=='__main__': raise SystemExit(main())

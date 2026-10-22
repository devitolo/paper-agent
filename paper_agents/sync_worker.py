"""Isolated worker: verify backup -> pull/apply -> pipeline -> durable outbox.
A failed publish retries the saved payload; a crashed inference is never blindly rerun.
"""
from __future__ import annotations
import argparse, contextlib, fcntl, json, os, pathlib, time, urllib.request, urllib.parse, uuid, sqlite3
from .sync_exchange import MAX_BODY_BYTES, SOURCES, apply_input_snapshot, encoded, signature
from .sync_backup import validate_backup
from .sync_exchange import connect_db

def atomic_json(path,value):
    path=pathlib.Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('wb') as f:f.write(encoded(value));f.flush();os.fsync(f.fileno())
    tmp.replace(path)

def request(url,method,body,secret,path,key=''):
    parsed=urllib.parse.urlparse(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment or parsed.path not in ('','/'):
        raise ValueError('AWS URL must be an HTTPS origin')
    if len(body)>MAX_BODY_BYTES:raise ValueError('request too large')
    if secret!=os.environ.get('PAPER_AGENT_SYNC_SHARED_SECRET',''):raise ValueError('secret mismatch')
    ts=str(time.time());headers={'Content-Type':'application/json','X-Paper-Sync-Timestamp':ts,'X-Paper-Sync-Signature':signature(method,path,ts,body,key)}
    if key:headers['X-Paper-Sync-Idempotency']=key
    req=urllib.request.Request(url.rstrip('/')+path,data=body if method!='GET' else None,headers=headers,method=method)
    # Do not follow a redirect with authentication credentials.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    with urllib.request.build_opener(NoRedirect).open(req,timeout=60) as r:
        result=r.read(MAX_BODY_BYTES+1)
    if len(result)>MAX_BODY_BYTES:raise ValueError('response too large')
    return json.loads(result)

def pipeline_adapter(source,db_path,root,snapshot):
    """Call the existing pipeline with explicit isolated paths and source config."""
    root=pathlib.Path(root)
    if pathlib.Path.cwd().resolve()!=root.resolve():
        raise ValueError('pipeline adapter must run from isolated root for relative outputs')
    if source=='zenml':
        from .zenml_pilot import import_zenml_pilot
        output=import_zenml_pilot(db_path=db_path,config_path=root/'config/industry.json')
        cycle=output.get('workflow_cycle_id')
        if cycle is not None:
            with connect_db(db_path) as c:
                c.execute("UPDATE workflow_cycles SET metadata_json=json_set(metadata_json,'$.sync_worker_completed',1) WHERE id=?",(cycle,));c.commit()
        return results_for_cycle(db_path,root,cycle)
    from .topics import load_topic_config
    from .pipeline import run_daily_pipeline
    topics=[t.query for t in load_topic_config(root/'config/topics.yaml') if t.enabled and source in t.sources and t.cadence in ('daily','weekly')]
    if source=='core':
        from .topic_inventory import core_topic_inventory
        topics=core_topic_inventory()['active_topics']
    with connect_db(db_path) as c:before=c.execute('SELECT COALESCE(MAX(id),0) FROM workflow_cycles').fetchone()[0]
    output=run_daily_pipeline(topics=topics,db_path=db_path,source_name=source,scout_dir=root/'data/scout',pdf_dir=root/'data/papers',profile_path=root/'data/profile.json')
    cycle=output.get('workflow_cycle_id')
    if cycle is not None:
        with connect_db(db_path) as c:
            c.execute("UPDATE workflow_cycles SET metadata_json=json_set(metadata_json,'$.sync_worker_completed',1) WHERE id=?",(cycle,));c.commit()
    return results_for_cycle(db_path,root,cycle)

def results_for_cycle(db_path,root,cycle_id):
    if cycle_id is None:return []
    root=pathlib.Path(root);results=[]
    with connect_db(db_path) as c:
        c.row_factory=sqlite3.Row
        rows=c.execute('SELECT p.*,r.rationale,e.score,e.matched_signals_json FROM recommendations r JOIN curator_runs cr ON cr.id=r.curator_run_id JOIN papers p ON p.id=r.paper_id JOIN curator_evaluations e ON e.curator_run_id=cr.id AND e.paper_id=p.id WHERE cr.workflow_cycle_id=? ORDER BY r.recommendation_order',(cycle_id,)).fetchall()
        for row in rows:
            source=c.execute('SELECT * FROM paper_sources WHERE paper_id=? ORDER BY id LIMIT 1',(row['id'],)).fetchone()
            artifact=c.execute("SELECT * FROM artifacts WHERE paper_id=? AND artifact_type='triage_summary' ORDER BY id DESC LIMIT 1",(row['id'],)).fetchone()
            if not source or not artifact:raise ValueError('review-ready result missing source or summary')
            path=pathlib.Path(artifact['path']);path=path if path.is_absolute() else root/path
            results.append({k:row[k] for k in ('canonical_key','title','abstract','published','doi','arxiv_id','score','rationale')}|{'source':source['source'],'source_id':source['source_id'],'url':source['url'],'pdf_url':source['pdf_url'],'metadata':json.loads(source['metadata_json']),'summary':json.loads(path.read_text()),'model':artifact['model'],'provenance':json.loads(artifact['metadata_json']),'matched_signals':json.loads(row['matched_signals_json'])})
    return results

def propose_profile(db_path,state,root,snapshot,send,provider_fn=None):
    """Generate on Mini, persist before sending, accept only through AWS CAS."""
    from .feedback import apply_feedback_to_profile
    state=pathlib.Path(state);out=state/'profile-proposal.json'
    if out.exists():payload=json.loads(out.read_text())
    else:
        with connect_db(db_path) as c:
            output=apply_feedback_to_profile(c,dry_run=True,provider_fn=provider_fn);c.commit()
            if output['status']=='no_feedback':return snapshot
            if output['status']!='dry_run':raise ValueError('profile generation failed')
            remote=[]
            for lid in output['structured_feedback_ids']:
                row=c.execute("SELECT external_id FROM sync_input_row_map WHERE table_name='structured_feedback' AND local_id=?",(lid,)).fetchone()
                if not row:raise ValueError('profile consumed unmirrored feedback')
                remote.append('structured:'+str(row[0]))
        proposed=output['proposed_profile'];proposed['feedback_history']=json.loads(snapshot['profile']['profile_json']).get('feedback_history',[])
        payload={'schema':1,'proposal_id':uuid.uuid4().hex,'producer_id':os.environ.get('PAPER_AGENT_SYNC_PRODUCER','mini-nonprod'),'environment':'nonprod','base_profile_version':snapshot['profile']['version'],'input_revision':snapshot['revision'],'consumed_feedback_keys':remote,'profile':proposed,'change_summary':output['change_summary']}
        atomic_json(out,payload)
    receipt=send('/sync/profile-proposals',encoded(payload),payload['proposal_id'])
    if receipt.get('status')!='accepted' or receipt.get('proposal_id')!=payload['proposal_id']:raise ValueError('profile not accepted')
    atomic_json(state/('profile-receipt-'+payload['proposal_id']+'.json'),receipt);out.unlink()
    snapshot=send('/sync/input',method='GET');apply_input_snapshot(db_path,snapshot,root);atomic_json(state/'input-snapshot.json',snapshot)
    return snapshot

@contextlib.contextmanager
def lock(state):
    state.mkdir(parents=True,exist_ok=True)
    with (state/'worker.lock').open('a+') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield

def work(*,db_path,state_dir,root,aws_url,producer_id='mini-nonprod',environment='nonprod',source='arxiv',run=False,publish=False,backup_path=None,backup_dir=None,propose=False,transport=request,runner=pipeline_adapter,profile_provider=None):
    if environment!='nonprod':raise ValueError('only isolated nonprod execution enabled')
    if source not in SOURCES:raise ValueError('source runner not enabled')
    state=pathlib.Path(state_dir);root=pathlib.Path(root)
    if pathlib.Path(db_path).resolve()==pathlib.Path('data/paper_agent.db').resolve():raise ValueError('default production DB prohibited')
    with lock(state):
        secret=os.environ.get('PAPER_AGENT_SYNC_SHARED_SECRET','')
        def send(path,body=b'',key='',method='POST'):return transport(aws_url,method,body,secret,path,key)
        pending=sorted(state.glob('outbox-*.json'))
        if pending:
            if not publish:raise ValueError('pending results; publish before another inference run')
            for file in pending:
                payload=json.loads(file.read_text());key=payload['batch_id'];receipt=send('/sync/results',encoded(payload),key)
                if receipt.get('status')!='applied' or receipt.get('batch_key')!=key:raise ValueError('invalid publish receipt')
                atomic_json(state/('receipt-'+key+'.json'),receipt)
                file.rename(state/('published-'+key+'.json'))
            return {'status':'published','count':len(pending)}
        if publish and not run:return {'status':'nothing-to-publish'}
        if run and backup_dir:
            try:
                from .sync_backup import download_backup
                backup_path=download_backup(aws_url,pathlib.Path(backup_dir))
            except Exception as error:
                atomic_json(state/'backup-warning.json',{'status':'failed','message':str(error)})
        if run and (not backup_path or not validate_backup(pathlib.Path(backup_path))):
            atomic_json(state/'backup-warning.json',{'status':'failed-or-missing','message':'backup must be checked separately; continuing only after input sync succeeds'})
        existing_job=state/'active-job.json'
        snapshot=json.loads((state/'input-snapshot.json').read_text()) if existing_job.exists() else send('/sync/input',method='GET')
        apply_input_snapshot(db_path,snapshot,root)
        atomic_json(state/'input-snapshot.json',snapshot)
        if propose and not existing_job.exists():snapshot=propose_profile(db_path,state,root,snapshot,send,profile_provider)
        if not run:return {'status':'inputs-applied','revision':snapshot['revision']}
        jobfile=state/'active-job.json'
        if jobfile.exists():
            job=json.loads(jobfile.read_text())
            if job['stage']=='running':
                # Recovery requires a completed pipeline cycle; incomplete inference never repeats automatically.
                with connect_db(db_path) as c:
                    cycles=c.execute("SELECT id FROM workflow_cycles WHERE id>? AND json_extract(metadata_json,'$.sync_worker_completed')=1 ORDER BY id",(job['baseline_cycle'],)).fetchall()
                if len(cycles)!=1:raise ValueError('interrupted run needs explicit recovery; inference not repeated')
                results=results_for_cycle(db_path,root,cycles[0][0])
            else:results=job['results']
        else:
            sequencefile=state/'sequence.json';seq=json.loads(sequencefile.read_text())['sequence']+1 if sequencefile.exists() else 1
            atomic_json(sequencefile,{'sequence':seq})
            with connect_db(db_path) as c:baseline=c.execute('SELECT COALESCE(MAX(id),0) FROM workflow_cycles').fetchone()[0]
            job={'batch_id':uuid.uuid4().hex,'input_revision':snapshot['revision'],'producer_id':producer_id,'environment':environment,'run_sequence':seq,'source':source,'baseline_cycle':baseline,'stage':'running'};atomic_json(jobfile,job)
            results=runner(source,pathlib.Path(db_path),root,snapshot)
            job['results']=results;job['stage']='completed';atomic_json(jobfile,job)
        payload={k:job[k] for k in ('batch_id','input_revision','producer_id','environment','run_sequence')};payload.update(schema=1,results=results)
        atomic_json(state/('outbox-'+job['batch_id']+'.json'),payload);jobfile.unlink()
        if publish:
            key = job['batch_id']
            receipt = send('/sync/results', encoded(payload), key)
            if receipt.get('status') != 'applied' or receipt.get('batch_key') != key:
                raise ValueError('invalid publish receipt')
            atomic_json(state / ('receipt-' + key + '.json'), receipt)
            (state / ('outbox-' + key + '.json')).rename(state / ('published-' + key + '.json'))
            return {'status': 'published', 'count': 1}
        return {'status':'outbox-ready','batch_id':job['batch_id']}

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--db-path',type=pathlib.Path,required=True);p.add_argument('--state-dir',type=pathlib.Path,required=True);p.add_argument('--root',type=pathlib.Path,required=True);p.add_argument('--aws-url',required=True);p.add_argument('--producer-id',default='mini-nonprod');p.add_argument('--environment',default='nonprod');p.add_argument('--source',choices=SOURCES,default='arxiv');p.add_argument('--backup-path',type=pathlib.Path);p.add_argument('--backup-dir',type=pathlib.Path);p.add_argument('--run',action='store_true');p.add_argument('--publish',action='store_true');p.add_argument('--propose',action='store_true')
    args=vars(p.parse_args(argv));print(json.dumps(work(**args)));return 0
if __name__=='__main__':raise SystemExit(main())

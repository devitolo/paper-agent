"""Versioned, bounded exchange; never execute SQL supplied by a peer."""
from __future__ import annotations
import hashlib, hmac, json, math, os, time, sqlite3
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from email.message import Message
from pathlib import Path
from .db import connect_db as _connect_db, DEFAULT_DB_PATH
MAX_BODY_BYTES = 8 * 1024 * 1024
ROUTINE_SCHEMA = 2
ROUTINE_MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_TIMESTAMP_SKEW = 300
SOURCES = ('arxiv', 'openalex', 'semantic_scholar', 'core', 'zenml', 'ssrn')
USER_TABLES = ('raw_feedback','feedback_parse_attempts','structured_feedback','profile_versions','feedback_profile_applications','feedback_profile_apply_attempts','feedback','summary_field_feedback','summary_field_quality_signals')

@contextmanager
def connect_db(path):
    """Close exchange connections deterministically after commit or rollback."""
    connection = _connect_db(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()

def _digest(body): return hashlib.sha256(body).hexdigest()
def encoded(value): return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def _secret():
    value=os.environ.get('PAPER_AGENT_SYNC_SHARED_SECRET','')
    if len(value)<32: raise ValueError('sync secret must be at least 32 characters')
    return value.encode()
def signature(method,path,timestamp,body,idempotency=''):
    return hmac.new(_secret(),'\n'.join((method.upper(),path,timestamp,idempotency,_digest(body))).encode(),hashlib.sha256).hexdigest()
def verify_request(headers,body,method='GET',path='/sync/input'):
    if len(body)>MAX_BODY_BYTES:return False
    ts=headers.get('X-Paper-Sync-Timestamp','')
    try:
        age=abs(time.time()-float(ts))
        if not math.isfinite(age) or age>MAX_TIMESTAMP_SKEW:return False
        expected=signature(method,path,ts,body,headers.get('X-Paper-Sync-Idempotency',''))
    except (ValueError,TypeError): return False
    return hmac.compare_digest(headers.get('X-Paper-Sync-Signature',''),expected)
def _payload(body):
    if len(body)>MAX_BODY_BYTES:raise ValueError('body exceeds size limit')
    try:value=json.loads(body.decode(),parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    except (ValueError,UnicodeError) as e:raise ValueError('invalid UTF-8 JSON') from e
    if not isinstance(value,dict) or value.get('schema')!=1:raise ValueError('unsupported schema')
    return value

def _setup(c):
    c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE IF NOT EXISTS sync_producers(producer TEXT,environment TEXT,sequence INTEGER NOT NULL,PRIMARY KEY(producer,environment))')
    c.execute('CREATE TABLE IF NOT EXISTS sync_snapshot_revisions(revision TEXT PRIMARY KEY,content_json TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS sync_input_row_map(table_name TEXT,external_id INTEGER,local_id INTEGER,PRIMARY KEY(table_name,external_id))')
    c.execute('CREATE TABLE IF NOT EXISTS sync_profile_receipts(proposal_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,response_json TEXT NOT NULL)')

def _rows(c,table):return [dict(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY id')]
def build_input_snapshot(db_path=DEFAULT_DB_PATH,root=Path('.'), *, max_bytes=MAX_BODY_BYTES):
    root=Path(root)
    # Capture files before/after database read: refuse a concurrent config edit.
    def files():
        return {name:(root/name).read_text() for name in ('config/topics.yaml','config/industry.json')}
    config=files()
    with connect_db(db_path) as c:
        _setup(c);c.commit();c.execute('BEGIN IMMEDIATE')
        papers=_rows(c,'papers');sources=_rows(c,'paper_sources')
        history={t:_rows(c,t) for t in USER_TABLES}
        artifact_ids={r['artifact_id'] for t in ('summary_field_feedback','summary_field_quality_signals') for r in history[t]}
        artifacts=[]
        for row in _rows(c,'artifacts'):
            if row['id'] in artifact_ids:
                row['content']=None
                if row['artifact_type']=='triage_summary':
                    p=Path(row['path']);p=p if p.is_absolute() else root/p
                    row['content']=json.loads(p.read_text())
                artifacts.append(row)
        saved=[dict(r) for r in c.execute('SELECT * FROM saved_papers ORDER BY paper_id')]
        excluded=[dict(r) for r in c.execute('SELECT * FROM excluded_papers ORDER BY paper_id')]
        profile=next((r for r in reversed(history['profile_versions']) if r['active']),None)
        if profile is None:
            profile={'version':0,'profile_json':(root/'data/profile.json').read_text()}
        if config!=files():raise ValueError('configuration changed during snapshot')
        payload={'schema':1,'config':config,'profile':profile,'papers':papers,'paper_sources':sources,'history':history,'artifacts':artifacts,'saved':saved,'excluded':excluded}
        payload['revision']=_digest(encoded(payload))
        if max_bytes is not None and len(encoded(payload))>max_bytes:raise ValueError('snapshot too large; pagination required')
        c.execute('INSERT OR IGNORE INTO sync_snapshot_revisions VALUES(?,?)',(payload['revision'],encoded(payload).decode()));c.commit()
    return payload

def build_routine_snapshot(db_path=DEFAULT_DB_PATH, root=Path('.'), *, max_bytes=ROUTINE_MAX_EXPANDED_BYTES):
    """Build the bounded business-data snapshot served to routine Mini sync."""
    root = Path(root)
    def read_config():
        return {name: (root / name).read_text(encoding='utf-8')
                for name in ('config/topics.yaml', 'config/industry.json')}
    config = read_config()
    with connect_db(db_path) as c:
        c.execute('BEGIN')
        c.row_factory = sqlite3.Row
        papers = _rows(c, 'papers')
        sources = [dict(row) for row in c.execute(
            'SELECT ps.source,ps.source_id,ps.url,ps.pdf_url,ps.discovered_at,ps.metadata_json,p.canonical_key '
            'FROM paper_sources ps JOIN papers p ON p.id=ps.paper_id ORDER BY ps.id')]
        profile = c.execute('SELECT version,profile_json FROM profile_versions WHERE active=1 ORDER BY version DESC LIMIT 1').fetchone()
        profile = dict(profile) if profile is not None else {'version': 0, 'profile_json': (root / 'data/profile.json').read_text()}
        watermark = c.execute('SELECT COALESCE(MAX(id),0) FROM structured_feedback').fetchone()[0]
        feedback = [dict(row) for row in c.execute('''SELECT sf.id,sf.decision,sf.score,sf.observations_json,
            sf.preference_signals_json,sf.created_at,p.canonical_key,ps.source
            FROM structured_feedback sf LEFT JOIN papers p ON p.id=sf.paper_id
            LEFT JOIN paper_sources ps ON ps.paper_id=sf.paper_id
            WHERE (ps.id IS NULL OR ps.id=(SELECT MIN(id) FROM paper_sources WHERE paper_id=sf.paper_id))
              AND (sf.id IN (SELECT id FROM structured_feedback ORDER BY id DESC LIMIT 12)
                   OR NOT EXISTS (SELECT 1 FROM feedback_profile_applications fpa WHERE fpa.structured_feedback_id=sf.id))
            ORDER BY sf.id''')]
        saved = [dict(row) for row in c.execute('SELECT p.canonical_key,s.created_at FROM saved_papers s JOIN papers p ON p.id=s.paper_id')]
        excluded = [dict(row) for row in c.execute('SELECT p.canonical_key,e.created_at FROM excluded_papers e JOIN papers p ON p.id=e.paper_id')]
    if config != read_config():
        raise ValueError('configuration changed during routine snapshot')
    payload = {'schema': ROUTINE_SCHEMA, 'kind': 'routine_snapshot', 'config': config,
               'profile': profile, 'papers': papers, 'paper_sources': sources,
               'feedback': feedback, 'feedback_watermark': watermark,
               'saved': saved, 'excluded': excluded}
    payload['revision'] = _digest(encoded(payload))
    if max_bytes is not None and len(encoded(payload)) > max_bytes: raise ValueError('routine snapshot too large')
    return payload

def register_routine_revision(db_path, snapshot):
    if not isinstance(snapshot, dict) or type(snapshot.get('schema')) is not int or snapshot.get('schema') != ROUTINE_SCHEMA:
        raise ValueError('unsupported routine snapshot')
    with connect_db(db_path) as c:
        _setup(c)
        marker = {'schema': ROUTINE_SCHEMA, 'kind': 'routine_revision', 'revision': snapshot['revision'], 'feedback_watermark': snapshot.get('feedback_watermark', 0)}
        c.execute('INSERT OR REPLACE INTO sync_snapshot_revisions VALUES(?,?)', (snapshot['revision'], encoded(marker).decode()))

def register_backup_snapshot(db_path, backup, snapshot=None):
    manifest_path=Path(backup)/'manifest.json'
    manifest=json.loads(manifest_path.read_text())
    revision=manifest.get('snapshot_revision')
    if not isinstance(revision, str) or len(revision) != 64 or any(ch not in '0123456789abcdef' for ch in revision):
        raise ValueError('backup snapshot revision required')
    if snapshot is None:
        snapshot=build_input_snapshot(Path(backup)/'paper_agent.db', Path(backup), max_bytes=None)
    if not isinstance(snapshot, dict) or snapshot.get('revision') != revision:
        raise ValueError('backup snapshot revision mismatch')
    with connect_db(db_path) as c:
        _setup(c)
        c.execute('INSERT OR REPLACE INTO sync_snapshot_revisions VALUES(?,?)',(revision,encoded(snapshot).decode()))
    return revision

def _paper(c,item):
    key=item.get('canonical_key');title=item.get('title')
    if not isinstance(key,str) or not key or len(key)>500 or not isinstance(title,str) or not title:raise ValueError('paper identity/title required')
    now=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    fields=(key,title,item.get('abstract'),item.get('published'),item.get('updated'),item.get('doi'),item.get('arxiv_id'),item.get('authors_json','[]'),item.get('categories_json','[]'),item.get('first_discovered_at') or now,item.get('last_discovered_at') or now)
    c.execute('INSERT INTO papers(canonical_key,title,abstract,published,updated,doi,arxiv_id,authors_json,categories_json,first_discovered_at,last_discovered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(canonical_key) DO UPDATE SET title=excluded.title,abstract=excluded.abstract,published=excluded.published,updated=excluded.updated,doi=excluded.doi,arxiv_id=excluded.arxiv_id,authors_json=excluded.authors_json,categories_json=excluded.categories_json,last_discovered_at=excluded.last_discovered_at',(fields))
    return c.execute('SELECT id FROM papers WHERE canonical_key=?',(key,)).fetchone()[0]
def _source(c,pid,item):
    source=item.get('source');sid=item.get('source_id')
    if source not in SOURCES or not isinstance(sid,str) or not sid:raise ValueError('invalid source identity')
    old=c.execute('SELECT paper_id FROM paper_sources WHERE source=? AND source_id=?',(source,sid)).fetchone()
    if old and old[0]!=pid:raise ValueError('source alias belongs to a different paper')
    metadata=item.get('metadata_json',encoded(item.get('metadata',{})).decode())
    c.execute('INSERT INTO paper_sources(paper_id,source,source_id,url,pdf_url,metadata_json) VALUES(?,?,?,?,?,?) ON CONFLICT(source,source_id) DO UPDATE SET url=excluded.url,pdf_url=excluded.pdf_url,metadata_json=excluded.metadata_json',(pid,source,sid,item.get('url'),item.get('pdf_url'),metadata))

def validate_normalized(value):
    sections={'papers','paper_sources','artifacts','evaluations','recommendations'}
    if not isinstance(value,dict) or set(value)!=sections or any(not isinstance(v,list) or len(v)>50000 for v in value.values()):
        raise ValueError('invalid normalized sections')
    def string(row,key,empty=False):
        v=row.get(key)
        if not isinstance(v,str) or (not v and not empty): raise ValueError('invalid '+key)
        return v
    def json_field(row,key,kind):
        try: v=json.loads(row.get(key,'[]' if kind is list else '{}'))
        except (TypeError,ValueError): raise ValueError('invalid '+key) from None
        if not isinstance(v,kind): raise ValueError('invalid '+key)
    papers=set()
    for r in value['papers']:
        if not isinstance(r,dict): raise ValueError('invalid paper')
        k=string(r,'canonical_key'); string(r,'title')
        if k in papers: raise ValueError('duplicate paper')
        papers.add(k)
        for name in ('abstract','published','updated','doi','arxiv_id','first_discovered_at','last_discovered_at'):
            if r.get(name) is not None and not isinstance(r[name],str): raise ValueError('invalid paper '+name)
        json_field(r,'authors_json',list); json_field(r,'categories_json',list)
    seen=set()
    for section in ('paper_sources','artifacts','evaluations','recommendations'):
        for r in value[section]:
            if not isinstance(r,dict) or r.get('canonical_key') not in papers: raise ValueError('missing paper dependency')
            if section=='paper_sources':
                if r.get('source') not in SOURCES: raise ValueError('invalid source')
                identity=('source',r['source'],string(r,'source_id')); json_field(r,'metadata_json',dict)
                for field in ('url','pdf_url','discovered_at'):
                    if r.get(field) is not None and not isinstance(r[field],str): raise ValueError('invalid source '+field)
            elif section=='artifacts':
                if r.get('artifact_type')!='triage_summary' or not isinstance(r.get('content'),dict): raise ValueError('invalid summary content')
                json_field(r,'metadata_json',dict)
                if r.get('model') is not None and not isinstance(r['model'],str): raise ValueError('invalid summary model')
                digest=_digest(encoded(r['content']))
                if r.get('summary_hash',digest)!=digest: raise ValueError('summary hash mismatch')
                identity=('artifact',r['canonical_key'],digest)
            else:
                string(r,'cycle_key'); run=string(r,'run_key'); string(r,'rationale',empty=True)
                from .operational_evidence import stamp
                for clock in ('cycle_created_at','run_created_at','created_at'):
                    if r.get(clock) is not None and stamp(r[clock])>datetime.now(timezone.utc)+timedelta(minutes=5):
                        raise ValueError('future normalized run timestamp')
                identity=(section,run,r['canonical_key'])
                if section=='evaluations':
                    if isinstance(r.get('score'),bool) or not isinstance(r.get('score'),(int,float)) or not math.isfinite(r['score']): raise ValueError('invalid score')
                    if type(r.get('quality_threshold_met',0)) is not int or r.get('quality_threshold_met',0) not in (0,1): raise ValueError('invalid threshold')
                    json_field(r,'matched_signals_json',list)
                else:
                    order=r.get('recommendation_order')
                    if type(order) is not int or not 1<=order<=3: raise ValueError('invalid recommendation order')
                    slot=('order',run,order)
                    if slot in seen: raise ValueError('conflicting recommendation order')
                    seen.add(slot)
            if identity in seen: raise ValueError('duplicate normalized row')
            seen.add(identity)
    runs={}
    evaluations={(r['run_key'],r['canonical_key']) for r in value['evaluations']}
    for r in value['evaluations']+value['recommendations']:
        if runs.setdefault(r['run_key'],r['cycle_key'])!=r['cycle_key']: raise ValueError('conflicting run identity')
    if any((r['run_key'],r['canonical_key']) not in evaluations for r in value['recommendations']):
        raise ValueError('recommendation missing evaluation')


def _apply_normalized(c, normalized, root=Path('.')):
    papers={r['canonical_key']:_paper(c,r) for r in normalized['papers']}
    for r in normalized['paper_sources']: _source(c,papers[r['canonical_key']],r)
    cycles={}; runs={}
    for r in normalized['evaluations']:
        key=r['cycle_key']; run=r['run_key']
        if key not in cycles:
            cycles[key]=c.execute("INSERT INTO workflow_cycles(state,mode,metadata_json,created_at) VALUES('completed','imported',?,COALESCE(?,datetime('now')))",(encoded({'sync_cycle_key':key}).decode(),r.get('cycle_created_at'))).lastrowid
        if run not in runs:
            runs[run]=c.execute("INSERT INTO curator_runs(workflow_cycle_id,metadata_json,created_at) VALUES(?,?,COALESCE(?,datetime('now')))",(cycles[key],encoded({'sync_run_key':run}).decode(),r.get('run_created_at'))).lastrowid
        c.execute('INSERT INTO curator_evaluations(curator_run_id,paper_id,score,rationale,matched_signals_json,quality_threshold_met) VALUES(?,?,?,?,?,?)',(runs[run],papers[r['canonical_key']],r['score'],r['rationale'],r.get('matched_signals_json','[]'),r.get('quality_threshold_met',0)))
    for r in normalized['recommendations']:
        c.execute("INSERT INTO recommendations(curator_run_id,paper_id,recommendation_order,rationale,created_at) VALUES(?,?,?,?,COALESCE(?,datetime('now')))",(runs[r['run_key']],papers[r['canonical_key']],r['recommendation_order'],r['rationale'],r.get('created_at')))
    for r in normalized['artifacts']:
        digest=_digest(encoded(r['content'])); path=Path('data/sync-summaries')/(digest+'.json')
        target=Path(root)/path
        if any(p.is_symlink() for p in (target,*target.parents)): raise ValueError('aliased summary destination')
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() and target.read_bytes()!=encoded(r['content']): raise ValueError('summary hash collision')
        if not target.exists():
            import tempfile
            with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as f:
                f.write(encoded(r['content'])); temporary=Path(f.name)
            temporary.replace(target)
        c.execute('INSERT INTO artifacts(paper_id,artifact_type,path,model,metadata_json) VALUES(?,?,?,?,?) ON CONFLICT(paper_id,artifact_type,path) DO UPDATE SET model=excluded.model,metadata_json=excluded.metadata_json',(papers[r['canonical_key']],'triage_summary',str(path),r.get('model'),r.get('metadata_json','{}')))

def apply_input_snapshot(db_path,snapshot,root):
    """Mirror AWS-owned history into isolated Mini DB, remapping every foreign key."""
    import tempfile
    from .db import init_db
    root=Path(root);init_db(db_path)
    payload=dict(snapshot);revision=payload.pop('revision',None)
    if revision!=_digest(encoded(payload)):raise ValueError('snapshot revision mismatch')
    config=payload.get('config',{})
    if set(config)!= {'config/topics.yaml','config/industry.json'}:raise ValueError('incomplete configuration')
    from .topics import parse_topics_yaml,validate_topics
    validate_topics(parse_topics_yaml(config['config/topics.yaml']));json.loads(config['config/industry.json'])
    profile=json.loads(payload['profile']['profile_json'])
    with connect_db(db_path) as c:
        _setup(c);c.commit();c.execute('BEGIN IMMEDIATE')
        pmap={r['id']:_paper(c,r) for r in payload['papers']}
        for r in payload['paper_sources']:_source(c,pmap[r['paper_id']],r)
        def mapped(table,remote):
            if remote is None:return None
            if table=='papers':return pmap[remote]
            row=c.execute('SELECT local_id FROM sync_input_row_map WHERE table_name=? AND external_id=?',(table,remote)).fetchone()
            if not row:raise ValueError('missing snapshot foreign-key dependency: '+table)
            return row[0]
        def upsert(table,r):
            remote=r['id'];fields=dict(r);fields.pop('id')
            for fk in c.execute('PRAGMA foreign_key_list('+table+')'):
                column=fk['from'];target=fk['table']
                # Pipeline recommendation IDs are intentionally not mirrored.
                if target=='recommendations':fields[column]=None
                elif column in fields:fields[column]=mapped(target,fields[column])
            local=c.execute('SELECT local_id FROM sync_input_row_map WHERE table_name=? AND external_id=?',(table,remote)).fetchone()
            allowed_cols={r[1] for r in c.execute('PRAGMA table_info('+table+')')}
            if not set(fields)<=allowed_cols:raise ValueError('unknown snapshot columns')
            cols=list(fields)
            if local:
                c.execute('UPDATE '+table+' SET '+','.join(k+'=?' for k in cols)+' WHERE id=?',(*[fields[k] for k in cols],local[0]));lid=local[0]
            else:
                lid=c.execute('INSERT INTO '+table+'('+','.join(cols)+') VALUES('+','.join('?' for _ in cols)+')',tuple(fields[k] for k in cols)).lastrowid
                c.execute('INSERT INTO sync_input_row_map VALUES(?,?,?)',(table,remote,lid))
            return lid
        for r in payload['artifacts']:
            r=dict(r);content=r.pop('content');r['path']='data/sync-input-summaries/'+str(r['id'])+'-'+_digest(encoded(content or {}))+'.json'
            p=root/r['path'];p.parent.mkdir(parents=True,exist_ok=True)
            if content is not None:p.write_bytes(encoded(content))
            upsert('artifacts',r)
        for table in USER_TABLES:
            rows=payload['history'][table]
            if table=='profile_versions':c.execute('UPDATE profile_versions SET active=0')
            for r in rows:upsert(table,r)
            if table in ('summary_field_feedback','summary_field_quality_signals'):
                live={r['id'] for r in rows}
                for remote,lid in c.execute('SELECT external_id,local_id FROM sync_input_row_map WHERE table_name=?',(table,)).fetchall():
                    if remote not in live:
                        c.execute('DELETE FROM '+table+' WHERE id=?',(lid,));c.execute('DELETE FROM sync_input_row_map WHERE table_name=? AND external_id=?',(table,remote))
        if not payload['history']['profile_versions']:
            c.execute('INSERT INTO profile_versions(version,profile_json,active) VALUES(0,?,1) ON CONFLICT(version) DO UPDATE SET profile_json=excluded.profile_json,active=1',(encoded(profile).decode(),))
        for table,key in (('saved_papers','saved'),('excluded_papers','excluded')):
            c.execute('DELETE FROM '+table)
            for r in payload[key]:c.execute('INSERT INTO '+table+'(paper_id,created_at) VALUES(?,?)',(pmap[r['paper_id']],r['created_at']))
        if c.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('snapshot foreign keys invalid')
        # Files are written atomically before commit; any failure aborts run. Worker always reapplies snapshot before inference.
        for name,text in {**config,'data/profile.json':encoded(profile).decode()}.items():
            target=root/name;target.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as f:f.write(text.encode());tmp=Path(f.name)
            tmp.replace(target)
        c.execute('INSERT OR REPLACE INTO sync_snapshot_revisions VALUES(?,?)',(revision,encoded(snapshot).decode()));c.commit()
    return revision

def receive_result_batch(db_path,body,headers,root=Path('.')):
    payload=_payload(body);key=headers.get('X-Paper-Sync-Idempotency','').strip()
    if not key or len(key)>200:raise ValueError('idempotency key required')
    producer=payload.get('producer_id');environment=payload.get('environment');seq=payload.get('run_sequence')
    expected=os.environ.get('PAPER_AGENT_SYNC_ENVIRONMENT','nonprod');allowed=os.environ.get('PAPER_AGENT_SYNC_PRODUCER','mini-nonprod')
    if producer!=allowed or environment!=expected or type(seq)!=int or seq<1:raise ValueError('producer/environment/sequence rejected')
    results=payload.get('results')
    if not isinstance(results,list) or len(results)>100:raise ValueError('results must be bounded recommendations')
    if 'pipeline_delta' in payload: raise ValueError('unsupported raw pipeline delta')
    normalized=payload.get('normalized',{'papers':[],'paper_sources':[],'artifacts':[],'evaluations':[],'recommendations':[]})
    if not isinstance(normalized,dict) or set(normalized)!={'papers','paper_sources','artifacts','evaluations','recommendations'} or any(not isinstance(normalized[k],list) for k in normalized):raise ValueError('invalid normalized sections')
    validate_normalized(normalized)
    from .operational_evidence import validate as validate_operational
    validate_operational(payload.get("operational"))
    root=Path(root);content_hash=_digest(body)
    with connect_db(db_path) as c:
        _setup(c);c.commit();c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT payload_hash,status FROM sync_result_batches WHERE batch_key=?',(key,)).fetchone()
        if row:
            if row[0]!=content_hash:raise ValueError('idempotency key reused for changed payload')
            return {'status':row[1],'batch_key':key,'replayed':True,**({'operational_version':1} if payload.get('operational') is not None else {})}
        if not c.execute('SELECT 1 FROM sync_snapshot_revisions WHERE revision=?',(payload.get('input_revision'),)).fetchone():raise ValueError('unknown input revision')
        last=c.execute('SELECT sequence FROM sync_producers WHERE producer=? AND environment=?',(producer,environment)).fetchone()
        if last and seq<=last[0]:raise ValueError('stale result sequence')
        c.execute('INSERT INTO sync_result_batches(batch_key,input_revision,payload_hash,payload_json,status) VALUES(?,?,?,?,?)',(key,payload['input_revision'],content_hash,body.decode(),'received'))
        _apply_normalized(c, normalized, root)
        cycle=None; run=None
        if results:
            cycle=c.execute("INSERT INTO workflow_cycles(state,mode,metadata_json) VALUES('completed','sync',?)",(encoded({'batch_key':key,'input_revision':payload['input_revision'],'producer_id':producer,'run_sequence':seq}).decode(),)).lastrowid
            run=c.execute('INSERT INTO curator_runs(workflow_cycle_id,metadata_json) VALUES(?,?)',(cycle,encoded({'imported':True}).decode())).lastrowid
        seen=set()
        for index,item in enumerate(results):
            order=index%3+1
            if index and order==1:
                run=c.execute('INSERT INTO curator_runs(workflow_cycle_id,metadata_json) VALUES(?,?)',(cycle,encoded({'imported':True}).decode())).lastrowid
            if not isinstance(item,dict):raise ValueError('invalid result')
            pid=_paper(c,item)
            if pid in seen:raise ValueError('duplicate result paper')
            seen.add(pid);_source(c,pid,item)
            score=item.get('score');rationale=item.get('rationale')
            if isinstance(score,bool) or not isinstance(score,(int,float)) or not math.isfinite(score) or not isinstance(rationale,str):raise ValueError('score and rationale required')
            c.execute('INSERT INTO curator_evaluations(curator_run_id,paper_id,score,rationale,matched_signals_json,quality_threshold_met) VALUES(?,?,?,?,?,1)',(run,pid,score,rationale,encoded(item.get('matched_signals',[])).decode()))
            c.execute('INSERT INTO recommendations(curator_run_id,paper_id,recommendation_order,rationale) VALUES(?,?,?,?)',(run,pid,order,rationale))
            summary=item.get('summary')
            if not isinstance(summary,dict) or not isinstance(summary.get('merged',summary),dict):raise ValueError('summary payload required')
            provenance=item.get('provenance',{});model=item.get('model')
            if not isinstance(provenance,dict) or (model is not None and not isinstance(model,str)):raise ValueError('invalid model/provenance')
            shash=_digest(encoded({'summary':summary,'provenance':provenance,'model':model}))
            path=Path('data/sync-summaries')/(shash+'.json');target=root/path;target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists() and target.read_bytes()!=encoded(summary):raise ValueError('immutable summary collision')
            if not target.exists():
                tmp=target.with_suffix('.tmp');tmp.write_bytes(encoded(summary));tmp.replace(target)
            old=c.execute('SELECT id FROM sync_summary_versions WHERE paper_id=? AND summary_hash=?',(pid,shash)).fetchone()
            if not old:
                version=c.execute('SELECT COALESCE(MAX(version),0)+1 FROM sync_summary_versions WHERE paper_id=?',(pid,)).fetchone()[0]
                c.execute('INSERT INTO sync_summary_versions(paper_id,summary_hash,version,content_json,provenance_json) VALUES(?,?,?,?,?)',(pid,shash,version,encoded(summary).decode(),encoded(provenance).decode()))
            # New artifact identity even when revisiting an older summary. Old feedback always remains attached to old artifact.
            artifact_path=Path('data/sync-summaries')/(shash+'-'+_digest(key.encode())+'.json')
            artifact_target=root/artifact_path
            artifact_target.write_bytes(encoded(summary))
            metadata={**provenance,'summary_hash':shash,'input_revision':payload['input_revision']}
            c.execute('INSERT INTO artifacts(paper_id,artifact_type,path,model,metadata_json) VALUES(?,?,?,?,?)',(pid,'triage_summary',str(artifact_path),model,encoded(metadata).decode()))
        c.execute('INSERT INTO sync_producers VALUES(?,?,?) ON CONFLICT(producer,environment) DO UPDATE SET sequence=excluded.sequence',(producer,environment,seq))
        c.execute("UPDATE sync_result_batches SET status='applied',applied_at=datetime('now') WHERE batch_key=?",(key,));c.commit()
    return {'status':'applied','batch_key':key,'replayed':False,**({'operational_version':1} if payload.get('operational') is not None else {})}

def receive_profile_proposal(db_path,body,headers):
    p=_payload(body)
    if p.get('producer_id')!=os.environ.get('PAPER_AGENT_SYNC_PRODUCER','mini-nonprod') or p.get('environment')!=os.environ.get('PAPER_AGENT_SYNC_ENVIRONMENT','nonprod'):raise ValueError('profile producer/environment rejected')
    key=headers.get('X-Paper-Sync-Idempotency','');digest=_digest(body)
    if not key or p.get('proposal_id')!=key:raise ValueError('proposal/idempotency mismatch')
    base=p.get('base_profile_version');keys=p.get('consumed_feedback_keys');profile=p.get('profile')
    if isinstance(profile,dict) and any(not isinstance(profile.get(k),list) for k in ('interests','positive_signals','negative_signals','feedback_history')):raise ValueError('profile requires interests/signals/history arrays')
    if type(base)!=int or base<0 or not isinstance(profile,dict) or not isinstance(keys,list) or not keys or len(keys)!=len(set(keys)):raise ValueError('invalid profile proposal')
    with connect_db(db_path) as c:
        _setup(c);c.commit();c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT payload_hash,response_json FROM sync_profile_receipts WHERE proposal_id=?',(key,)).fetchone()
        if old:
            if old[0]!=digest:raise ValueError('changed profile proposal replay')
            return json.loads(old[1])
        if not c.execute('SELECT 1 FROM sync_snapshot_revisions WHERE revision=?',(p.get('input_revision'),)).fetchone():raise ValueError('unknown profile input revision')
        current=c.execute('SELECT version FROM profile_versions WHERE active=1 ORDER BY version DESC LIMIT 1').fetchone()
        if (current[0] if current else 0)!=base:raise ValueError('profile base-version conflict')
        ids=[]
        for feedback_key in keys:
            if not isinstance(feedback_key,str) or not feedback_key.startswith('structured:'):raise ValueError('invalid feedback key')
            fid=int(feedback_key.split(':')[1]);ids.append(fid)
            if not c.execute('SELECT 1 FROM structured_feedback WHERE id=?',(fid,)).fetchone():raise ValueError('feedback missing')
            if c.execute('SELECT 1 FROM feedback_profile_applications WHERE structured_feedback_id=?',(fid,)).fetchone():raise ValueError('feedback already consumed')
        snapshot=json.loads(c.execute('SELECT content_json FROM sync_snapshot_revisions WHERE revision=?',(p['input_revision'],)).fetchone()[0])
        if snapshot['profile']['version']!=base:raise ValueError('profile snapshot base mismatch')
        allowed={r['id'] for r in snapshot['history']['structured_feedback']}
        if not set(ids)<=allowed:raise ValueError('feedback absent from proposal snapshot')
        c.execute('UPDATE profile_versions SET active=0')
        next_version=c.execute('SELECT COALESCE(MAX(version),0)+1 FROM profile_versions').fetchone()[0]
        pid=c.execute('INSERT INTO profile_versions(version,profile_json,source_structured_feedback_id,change_summary,active) VALUES(?,?,?,?,1)',(next_version,encoded(profile).decode(),ids[-1],p.get('change_summary','sync proposal'))).lastrowid
        for fid in ids:c.execute('INSERT INTO feedback_profile_applications(structured_feedback_id,profile_version_id) VALUES(?,?)',(fid,pid))
        response={'status':'accepted','proposal_id':key,'profile_version':next_version}
        c.execute('INSERT INTO sync_profile_receipts VALUES(?,?,?)',(key,digest,encoded(response).decode()));c.commit()
        return response

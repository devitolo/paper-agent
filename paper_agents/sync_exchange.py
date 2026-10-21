"""Versioned, bounded exchange; never execute SQL supplied by a peer."""
from __future__ import annotations
import hashlib, hmac, json, math, os, time, sqlite3
from contextlib import contextmanager
from email.message import Message
from pathlib import Path
from .db import connect_db as _connect_db, DEFAULT_DB_PATH
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_TIMESTAMP_SKEW = 300
SOURCES = ('arxiv', 'openalex', 'semantic_scholar', 'core', 'zenml')
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
def build_input_snapshot(db_path=DEFAULT_DB_PATH,root=Path('.')):
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
        if len(encoded(payload))>MAX_BODY_BYTES:raise ValueError('snapshot too large; pagination required')
        c.execute('INSERT OR IGNORE INTO sync_snapshot_revisions VALUES(?,?)',(payload['revision'],encoded(payload).decode()));c.commit()
    return payload

def _paper(c,item):
    key=item.get('canonical_key');title=item.get('title')
    if not isinstance(key,str) or not key or len(key)>500 or not isinstance(title,str) or not title:raise ValueError('paper identity/title required')
    c.execute('INSERT INTO papers(canonical_key,title,abstract,published,doi,arxiv_id) VALUES(?,?,?,?,?,?) ON CONFLICT(canonical_key) DO UPDATE SET title=excluded.title,abstract=excluded.abstract,published=excluded.published,doi=excluded.doi,arxiv_id=excluded.arxiv_id',(key,title,item.get('abstract'),item.get('published'),item.get('doi'),item.get('arxiv_id')))
    return c.execute('SELECT id FROM papers WHERE canonical_key=?',(key,)).fetchone()[0]
def _source(c,pid,item):
    source=item.get('source');sid=item.get('source_id')
    if source not in SOURCES or not isinstance(sid,str) or not sid:raise ValueError('invalid source identity')
    old=c.execute('SELECT paper_id FROM paper_sources WHERE source=? AND source_id=?',(source,sid)).fetchone()
    if old and old[0]!=pid:raise ValueError('source alias belongs to a different paper')
    metadata=item.get('metadata_json',encoded(item.get('metadata',{})).decode())
    c.execute('INSERT INTO paper_sources(paper_id,source,source_id,url,pdf_url,metadata_json) VALUES(?,?,?,?,?,?) ON CONFLICT(source,source_id) DO UPDATE SET url=excluded.url,pdf_url=excluded.pdf_url,metadata_json=excluded.metadata_json',(pid,source,sid,item.get('url'),item.get('pdf_url'),metadata))

def _apply_normalized(c, normalized):
    papers={}
    for item in normalized.get('papers',[]):
        pid=_paper(c,item); papers[item['canonical_key']]=pid
    for item in normalized.get('paper_sources',[]):
        item=dict(item); item['paper_id']=papers[item['canonical_key']]; _source(c,item['paper_id'],item)
    for item in normalized.get('artifacts',[]):
        item=dict(item); pid=papers[item['canonical_key']]; content=item.pop('content',None); item.pop('canonical_key',None); item.pop('id',None); item['paper_id']=pid
        if content is not None:
            path=Path('data/sync-summaries')/(_digest(encoded(content))+'.json'); target=Path('.')/path; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(encoded(content)); item['path']=str(path)
        cols={r[1] for r in c.execute('PRAGMA table_info(artifacts)')}; item={k:v for k,v in item.items() if k in cols}
        c.execute('INSERT OR IGNORE INTO artifacts('+','.join(item)+') VALUES('+','.join('?'*len(item))+')',tuple(item.values()))

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
    delta=payload.get('pipeline_delta',{})
    if not isinstance(delta,dict) or any(k not in {'workflow_cycles','scout_runs','curator_runs','curator_evaluations','recommendations','artifacts'} for k in delta):raise ValueError('invalid pipeline delta')
    root=Path(root);content_hash=_digest(body)
    with connect_db(db_path) as c:
        _setup(c);c.commit();c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT payload_hash,status FROM sync_result_batches WHERE batch_key=?',(key,)).fetchone()
        if row:
            if row[0]!=content_hash:raise ValueError('idempotency key reused for changed payload')
            return {'status':row[1],'batch_key':key,'replayed':True}
        if not c.execute('SELECT 1 FROM sync_snapshot_revisions WHERE revision=?',(payload.get('input_revision'),)).fetchone():raise ValueError('unknown input revision')
        last=c.execute('SELECT sequence FROM sync_producers WHERE producer=? AND environment=?',(producer,environment)).fetchone()
        if last and seq<=last[0]:raise ValueError('stale result sequence')
        c.execute('INSERT INTO sync_result_batches(batch_key,input_revision,payload_hash,payload_json,status) VALUES(?,?,?,?,?)',(key,payload['input_revision'],content_hash,body.decode(),'received'))
        normalized=payload.get('normalized',{})
        if not isinstance(normalized,dict): raise ValueError('invalid normalized pipeline payload')
        _apply_normalized(c, normalized)
        cycle=c.execute("INSERT INTO workflow_cycles(state,mode,metadata_json) VALUES('completed','sync',?)",(encoded({'batch_key':key,'input_revision':payload['input_revision'],'producer_id':producer,'run_sequence':seq,'pipeline_delta_counts':{k:len(v) for k,v in delta.items()}}).decode(),)).lastrowid
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
    return {'status':'applied','batch_key':key,'replayed':False}

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

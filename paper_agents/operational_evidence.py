"""Bounded operational facts carried by the existing authenticated result batch.

No schema migration: immutable payload_json is already stored atomically with its
AWS receipt. Raw messages, provider diagnostics, paths and credentials never cross.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCES = {'arxiv','openalex','semantic_scholar','core','ssrn','zenml'}
JOBS = SOURCES | {'semantic','profile','backup'}
STATUSES = {'started','completed','failed','skipped_busy'}
MAX_ROWS = 200
MAX_BYTES = 65536
PACIFIC = ZoneInfo('America/Los_Angeles')


def stamp(value):
    if not isinstance(value,str) or len(value)>64:
        raise ValueError('invalid operational timestamp')
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError as exc:
        raise ValueError('invalid operational timestamp') from exc
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def validate(value):
    if value is None:
        return None  # Older producers remain supported; missing facts are not zero.
    if (not isinstance(value,dict) or set(value)!={'version','events','scout_runs'}
            or type(value['version']) is not int or value['version']!=1
            or len(json.dumps(value).encode())>MAX_BYTES):
        raise ValueError('invalid operational evidence')
    for name in ('events','scout_runs'):
        if not isinstance(value[name],list) or len(value[name])>MAX_ROWS:
            raise ValueError('unbounded operational evidence')
    for row in value['events']:
        if not isinstance(row,dict) or set(row)!={'recorded_at','job','status'}:
            raise ValueError('invalid operational event fields')
        if not isinstance(row['job'],str) or not isinstance(row['status'],str) or row['job'] not in JOBS or row['status'] not in STATUSES:
            raise ValueError('invalid operational event')
        if stamp(row['recorded_at'])>datetime.now(timezone.utc)+timedelta(minutes=5):
            raise ValueError('future operational event')
    seen=set()
    for row in value['scout_runs']:
        if not isinstance(row,dict) or set(row)!={'run_id','source','started_at','completed_at','candidate_count','eligible_count','excluded_count'}:
            raise ValueError('invalid operational Scout fields')
        if not isinstance(row['source'],str) or row['source'] not in SOURCES or type(row['run_id']) is not int or row['run_id']<1 or row['run_id'] in seen:
            raise ValueError('invalid operational Scout identity')
        seen.add(row['run_id'])
        start=stamp(row['started_at'])
        if start>datetime.now(timezone.utc)+timedelta(minutes=5):
            raise ValueError('future operational Scout run')
        if row['completed_at'] is not None and (stamp(row['completed_at'])<start or stamp(row['completed_at'])>datetime.now(timezone.utc)+timedelta(minutes=5)):
            raise ValueError('operational completion precedes start')
        for key in ('candidate_count','eligible_count','excluded_count'):
            if type(row[key]) is not int or not 0<=row[key]<=1000000:
                raise ValueError('invalid operational count')
        if row['eligible_count']+row['excluded_count']!=row['candidate_count']:
            raise ValueError('inconsistent operational counts')
    return value


def collect(connection, root, cycle):
    """Called inside export's read transaction; select only the current cycle delta."""
    from paper_agents.job_events import recent
    baseline=cycle['baseline']['workflow_cycles']
    runs=[dict(row) for row in connection.execute('''SELECT sr.id AS run_id,sr.source,sr.started_at,sr.completed_at,
        COUNT(sc.id) AS candidate_count,
        COALESCE(SUM(CASE WHEN sc.id IS NOT NULL AND sc.excluded=0 THEN 1 ELSE 0 END),0) AS eligible_count,
        COALESCE(SUM(CASE WHEN sc.excluded=1 THEN 1 ELSE 0 END),0) AS excluded_count
        FROM scout_runs sr LEFT JOIN scout_candidates sc ON sc.scout_run_id=sr.id
        WHERE sr.workflow_cycle_id>? GROUP BY sr.id ORDER BY sr.id LIMIT ?''',(baseline,MAX_ROWS+1))]
    if len(runs)>MAX_ROWS:
        raise ValueError('too many operational Scout runs')
    lower=cycle.get('started_at') or (min((r['started_at'] for r in runs),default=None))
    events=[]
    if lower:
        start=stamp(lower)
        for row in reversed(recent(Path(root)/'data/job-events.jsonl',limit=MAX_ROWS)):
            if row['job'] in JOBS and stamp(row['recorded_at'])>=start:
                events.append({k:row[k] for k in ('recorded_at','job','status')})
    return validate({'version':1,'events':events,'scout_runs':runs})


def batches(connection, *, days=21):
    # Extract only the bounded subsection: never load hundreds of business payloads.
    return [dict(row) for row in connection.execute('''SELECT batch_key,input_revision,status,created_at,applied_at,
        json_extract(payload_json,'$.operational') AS operational,
        json_extract(payload_json,'$.producer_id') AS producer_id,
        json_extract(payload_json,'$.environment') AS environment
        FROM sync_result_batches WHERE created_at>=datetime('now',?)
        OR id=(SELECT MAX(id) FROM sync_result_batches)
        ORDER BY id DESC LIMIT 200''',(f'-{int(days)} days',))]


def project(summary, rows, *, native_scout=()):
    """Separate processing observations from the AWS-controlled delivery receipt."""
    source=summary['range']['source']
    source=None if source=='all' else source
    cutoff=datetime.now(timezone.utc)-timedelta(days=summary['range']['days'])
    events=[]; scout=[]; receipts=[]
    seen_events=set(); seen_runs=set()
    native_keys=set()
    for row in native_scout:
        try: native_keys.add((row['id'],row['source'],stamp(row['started_at'])))
        except (ValueError,TypeError): pass
    for batch in rows:
        try:
            facts=json.loads(batch['operational']) if batch['operational'] else None
            validate(facts)
        except (ValueError,TypeError,KeyError):
            facts=None
        fact_sources=({r['source'] for r in facts['scout_runs']} | {'semantic_scholar' if r['job']=='semantic' else r['job'] for r in facts['events']}) if facts else set()
        receipts.append(batch)  # Sync acceptance is global, independent of chart filtering.
        if source and source not in fact_sources:
            continue
        if facts is None or batch['status']!='applied':
            continue
        for event in facts['events']:
            canonical='semantic_scholar' if event['job']=='semantic' else event['job']
            identity=(batch['producer_id'],batch['environment'],event['recorded_at'],event['job'],event['status'])
            if (not source or canonical==source) and stamp(event['recorded_at'])>=cutoff and identity not in seen_events:
                seen_events.add(identity)
                events.append({**event,'delivery':'accepted','batch_key':batch['batch_key'],
                               'message':'Processing event supplied by Mini; batch accepted by AWS.'})
        for run in facts['scout_runs']:
            identity=(batch['producer_id'],batch['environment'],run['source'],run['run_id'],run['started_at'])
            if (not source or run['source']==source) and stamp(run['started_at'])>=cutoff and identity not in seen_runs and (run['run_id'],run['source'],stamp(run['started_at'])) not in native_keys:
                seen_runs.add(identity)
                scout.append(run)
    summary['sync_receipts']=receipts
    summary['operational_evidence_available']=any(r['operational'] for r in receipts)
    if events:
        latest=max(events,key=lambda r:stamp(r['recorded_at']))
        summary['latest_processing_observation']=latest
        summary['top']['latest_run_time']=latest['recorded_at']
        summary['top']['latest_run_age_hours']=round((datetime.now(timezone.utc)-stamp(latest['recorded_at'])).total_seconds()/3600,1)
        summary['top']['latest_workflow_state']=latest['job']+' '+latest['status']
        summary['job_events']=sorted(events,key=lambda r:stamp(r['recorded_at']),reverse=True)[:MAX_ROWS]
    # Existing native Scout rows and new imported operational rows are distinct:
    # normalized imports never create Scout rows, and baseline excludes old cycles.
    grouped=defaultdict(lambda:{'run_count':0,'candidate_count':0,'eligible_count':0,'excluded_count':0})
    for run in scout:
        key=(stamp(run['started_at']).astimezone(PACIFIC).date().isoformat(),run['source'])
        grouped[key]['run_count']+=1
        for metric in ('candidate_count','eligible_count','excluded_count'):
            grouped[key][metric]+=run[metric]
    summary['daily']['scout'].extend({'day':day,'source':src,**counts} for (day,src),counts in grouped.items())
    if scout:
        totals=defaultdict(lambda:{'candidate_count':0,'eligible_count':0,'excluded_count':0,'run_count':0})
        for row in summary['daily']['scout']:
            for key in totals[row['source']]: totals[row['source']][key]+=int(row.get(key) or 0)
        summary['source_breakdown']['funnel']=[{'source':src,**counts} for src,counts in totals.items()]
        summary['available_sources']=sorted(set(summary['available_sources'])|{r['source'] for r in scout})
    return summary


def sync_snapshot(summary):
    rows=summary.get('sync_receipts',[])
    if not rows:
        return None
    row=rows[0]
    state='completed' if row['status']=='applied' else row['status']
    when=row['applied_at'] or row['created_at']
    seconds=max(0,(datetime.now(timezone.utc)-stamp(when)).total_seconds())
    return {'state':'stale' if state=='completed' and seconds>48*3600 else state,
            'time':when,'revision':row['input_revision'],'age':f'{seconds/3600:.1f}h',
            'size':'N/A','failure':'none' if state=='completed' else 'See AWS batch receipt',
            'skipped':'N/A','installed':row['input_revision']}

#!/usr/bin/env python3
"""Exact-digest synthetic acceptance on a GitHub-hosted Docker daemon only.

Creates UUID-scoped disposable volumes. Never uses host production paths, ports,
credentials, app containers, or external network during runtime acceptance.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

IMAGE=re.compile(r'ghcr\.io/devitolo/paper-agent@sha256:[0-9a-f]{64}')
ROOT=Path(__file__).resolve().parents[1]
SEED=r'''
import json,os,sqlite3
from pathlib import Path
from paper_agents.db import init_db
# Synthetic state is created only here, in newly allocated acceptance volumes.
os.environ['PAPER_AGENT_SERVING_ONLY']='0'
os.environ['PAPER_AGENT_STARTUP_MODE']='fresh'
init_db(Path('/app/data/paper_agent.db'),Path('/app/sql/schema.sql'))
Path('/app/config/topics.yaml').write_text('topics: []\n')
Path('/app/config/industry.json').write_text(json.dumps({'enabled':False,'include_signals':[],'exclude_signals':[]}))
Path('/app/data/profile.json').write_text(json.dumps({'interests':[],'positive_signals':[],'negative_signals':[],'feedback_history':[]}))
with sqlite3.connect('/app/data/paper_agent.db') as db:
    db.execute("INSERT INTO papers(canonical_key,title) VALUES('aws-acceptance','Synthetic acceptance paper')")
    db.execute("INSERT INTO raw_feedback(paper_id,content,content_hash) VALUES(1,'Synthetic retained feedback','synthetic-retained')")
for root in ('/app/data','/app/config','/runtime-control'):
    for current,dirs,files in os.walk(root):
        os.chown(current,10001,10001)
        for name in files:os.chown(os.path.join(current,name),10001,10001)
'''
PUBLIC_HEALTH=r'''
import importlib.util,json,urllib.request
from pathlib import Path
assert importlib.util.find_spec('torch') is None
assert not Path('/app/data/MINI_NONPROD_SYNTHETIC').exists()
for path in ('/ready','/health','/runtime','/','/topics'):
    with urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=5) as response:
        assert response.status==200
        value=response.read(2*1024*1024+1)
        assert len(value)<=2*1024*1024
        if path in ('/ready','/runtime'):
            result=json.loads(value);assert result['ready'] is True
            if path=='/runtime':assert result['mode']=='serving-only'
'''
WRITE_FEEDBACK=r'''
import urllib.request,urllib.parse
form=urllib.parse.urlencode({'paper_id':'1','action':'feedback','notes':'Decision: keep\nScore: 5\nReason: Synthetic AWS acceptance feedback.','return_to':'/'}).encode()
with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/feedback',data=form),timeout=10) as response:
    assert response.status==200
'''
VERIFY_STATE=r'''
import sqlite3
from pathlib import Path
with sqlite3.connect('/app/data/paper_agent.db') as db:
    assert db.execute('SELECT COUNT(*) FROM papers').fetchone()[0]==1
    assert db.execute('SELECT COUNT(*) FROM raw_feedback').fetchone()[0]==2
    assert db.execute("SELECT content FROM raw_feedback WHERE content_hash='synthetic-retained'").fetchone()[0]=='Synthetic retained feedback'
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
assert not Path('/app/data/package-version.json').exists()
'''


def run(args,timeout=120):
    result=subprocess.run(args,check=False,capture_output=True,text=True,timeout=timeout)
    if result.returncode:raise RuntimeError('Synthetic image acceptance command failed')
    return result.stdout


def accept(image,revision,bundle,output, *,command=run,sleep=time.sleep):
    if not IMAGE.fullmatch(image) or not re.fullmatch('[0-9a-f]{40}',revision) or not re.fullmatch('[0-9a-f]{64}',bundle):
        raise ValueError('exact image/source identity required')
    command(['docker','pull',image],timeout=300)
    inspected=json.loads(command(['docker','image','inspect',image]))[0]
    labels=inspected['Config'].get('Labels',{})
    if inspected.get('Os')!='linux' or inspected.get('Architecture')!='amd64' or labels.get('org.projectpaper.runtime')!='cloud-production' or labels.get('org.opencontainers.image.revision')!=revision or labels.get('org.projectpaper.source-bundle-sha256')!=bundle:
        raise ValueError('image platform/runtime/source mismatch')
    spec=importlib.util.spec_from_file_location('aws_controller',ROOT/'scripts/aws_deploy.py')
    controller=importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)
    name='paper-aws-accept-'+uuid.uuid4().hex
    volumes=[]; containers=[]
    try:
        for kind in ('data','config','control'):
            volume=name+'-'+kind
            command(['docker','volume','create','--label','org.projectpaper.purpose=aws-image-acceptance',volume])
            volumes.append(volume)
        mounts=[]
        for volume,path in zip(volumes,('/app/data','/app/config','/runtime-control')):
            mounts+=['--mount',f'type=volume,src={volume},dst={path}']
        containers.append(name+'-seed')
        command(['docker','run','--name',name+'-seed','--rm','--network=none','--user=0:0',*mounts,'--entrypoint','python',image,'-c',SEED])
        # Run exact controller copied-state validation on the newly created state.
        containers.append(name+'-validator')
        command(['docker','run','--name',name+'-validator','--rm','--network=none','--read-only',*[v+',readonly' if v.startswith('type=volume,') else v for v in mounts],'--entrypoint','python',image,'-c',controller.READ_ONLY_VALIDATOR])
        # Synthetic acceptance auth only; command output is captured, never logged.
        # Random value is acceptance-only; no repository/production secret used.
        import secrets
        secret=secrets.token_hex(32)
        containers.append(name)
        command(['docker','run','-d','--name',name,'--network=none','--cap-drop=ALL',
            '--security-opt=no-new-privileges','--env','PAPER_AGENT_DEFAULT_SOURCES=arxiv',
            '--env','PAPER_AGENT_SYNC_SHARED_SECRET='+secret,*mounts,image])
        for attempt in range(60):
            try:
                command(['docker','exec',name,'python','-c',PUBLIC_HEALTH])
                observed=json.loads(command(['docker','inspect',name]))[0]
                if observed['State'].get('Health',{}).get('Status')!='healthy' or observed['State'].get('OOMKilled') or observed.get('RestartCount',0)!=0:
                    raise RuntimeError('container not stably healthy')
                break
            except RuntimeError:
                if attempt==59:raise
                sleep(2)
        command(['docker','exec',name,'python','-c',controller.SYNC_HEALTH_PROBE])
        command(['docker','exec',name,'python','-c',WRITE_FEEDBACK])
        command(['docker','exec',name,'python','-c',VERIFY_STATE])
        command(['docker','restart',name])
        for attempt in range(60):
            try:
                command(['docker','exec',name,'python','-c',PUBLIC_HEALTH])
                observed=json.loads(command(['docker','inspect',name]))[0]
                if observed['State'].get('Health',{}).get('Status')!='healthy' or observed['State'].get('OOMKilled') or observed.get('RestartCount',0)!=0:
                    raise RuntimeError('container not stably healthy')
                break
            except RuntimeError:
                if attempt==59:raise
                sleep(2)
        command(['docker','exec',name,'python','-c',VERIFY_STATE])
        state=json.loads(command(['docker','inspect',name]))[0]
        if state['State'].get('OOMKilled') or state.get('RestartCount',0)!=0:
            raise RuntimeError('acceptance OOM or unexpected restart')
        record={'schema':1,'status':'synthetic-accepted','image':image,'revision':revision,'bundle':bundle,
            'platform':'linux/amd64','checks':['copied-state-readonly','ready','health','runtime','ui','signed-sync-probes','feedback-persistence','restart-persistence','no-embedded-model'],
            'production_approved':False}
        output=Path(output);output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')
        return record
    finally:
        # UUID ownership is local to this invocation; never prune or remove other state.
        failures=[]
        for owned in reversed(containers):
            try:
                if command(['docker','ps','-aq','--filter','name=^/'+owned+'$']).strip():
                    command(['docker','rm','-f',owned])
            except Exception:failures.append('container')
        for volume in reversed(volumes):
            try:command(['docker','volume','rm',volume])
            except Exception:failures.append('volume')
        if failures:
            raise RuntimeError('Acceptance cleanup incomplete; inspect only this invocation UUID')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True);parser.add_argument('--revision',required=True)
    parser.add_argument('--bundle',required=True);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();accept(args.image,args.revision,args.bundle,args.output)
    print('Synthetic exact-image AWS serving acceptance passed; production remains unapproved.')

if __name__=='__main__':main()

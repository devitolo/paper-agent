"""Bounded, read-only AWS container projection and stdin exporter. No Docker API."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

SCHEMA = 'project-paper.aws-container-status.v1'
SERVICES = ('app', 'observability-collector')
MAX_BYTES = 8192
UNITS = {'B':1, 'kB':1000, 'KB':1000, 'MB':1000**2, 'GB':1000**3,
         'KiB':1024, 'MiB':1024**2, 'GiB':1024**3, 'TiB':1024**4}


def exact(value, fields):
    if not isinstance(value,dict) or set(value)!=set(fields):raise ValueError('invalid fields')


def number(value, maximum, integer=False):
    if value is None:return
    if isinstance(value,bool) or not isinstance(value,int if integer else (int,float)) or not 0<=value<=maximum or not math.isfinite(value):
        raise ValueError('invalid metric')


def validate(value, *, now=None):
    exact(value,('schema','environment','observed_at','items'))
    if value['schema']!=SCHEMA or value['environment']!='aws-production':raise ValueError('invalid identity')
    timestamp=value['observed_at']
    if not isinstance(timestamp,str) or len(timestamp)>40:raise ValueError('invalid time')
    instant=datetime.fromisoformat(timestamp.replace('Z','+00:00'))
    if instant.utcoffset() is None or instant.utcoffset().total_seconds()!=0:raise ValueError('UTC required')
    age=((now or datetime.now(timezone.utc))-instant).total_seconds()
    if age < -300:raise ValueError('future observation')
    items=value['items']
    if not isinstance(items,list) or len(items)!=len(SERVICES):raise ValueError('invalid inventory')
    for service,item in zip(SERVICES,items):
        exact(item,('service','status','health','image_digest','image_id','cpu_percent','memory_bytes','memory_limit_bytes'))
        if item['service']!=service:raise ValueError('invalid service')
        if not isinstance(item['status'],str) or item['status'] not in ('created','running','paused','restarting','removing','exited','dead','unavailable'):raise ValueError('invalid state')
        if not isinstance(item['health'],str) or item['health'] not in ('healthy','unhealthy','starting','no-healthcheck','unavailable'):raise ValueError('invalid health')
        for key in ('image_digest','image_id'):
            if item[key] is not None and (not isinstance(item[key],str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',item[key])):raise ValueError('invalid digest')
        number(item['cpu_percent'],6400)
        for key in ('memory_bytes','memory_limit_bytes'):number(item[key],2**63-1,True)
        if item['memory_bytes'] is not None and item['memory_limit_bytes'] is not None and item['memory_bytes']>item['memory_limit_bytes']:raise ValueError('invalid memory capacity')
        if item['status']!='running' and any(item[key] is not None for key in ('cpu_percent','memory_bytes','memory_limit_bytes')):raise ValueError('invalid stopped metric')
    if age>2700:return {'freshness':'stale','observed_at':timestamp,'items':None}
    return {**value,'freshness':'fresh'}


def load(path, *, now=None):
    try:
        with Path(path).open('rb') as stream:raw=stream.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise ValueError('oversize input')
        return validate(json.loads(raw),now=now)
    except (OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError):
        return {'freshness':'unavailable','observed_at':None,'items':None}


def memory_bytes(value):
    match=re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)',value.strip())
    if not match or match[2] not in UNITS:raise ValueError('invalid memory unit')
    measured=float(match[1])*UNITS[match[2]]
    if not math.isfinite(measured) or measured>2**63-1:raise ValueError('invalid memory')
    return int(measured)


def from_stream(stream):
    raw=stream.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES:raise ValueError('oversize input')
    lines=raw.splitlines()
    if len(lines)!=len(SERVICES)*3:raise ValueError('invalid input count')
    items=[]
    for index,service in enumerate(SERVICES):
        inspected,image,stats=[json.loads(line) for line in lines[index*3:index*3+3]]
        if any(not isinstance(item,dict) for item in (inspected,image,stats)):raise ValueError('invalid input shape')
        state=inspected.get('status','unavailable')
        digests=image.get('digests') or []
        if not isinstance(digests,list):raise ValueError('invalid digests')
        if image.get('image_id')!=inspected.get('image_id'):digests=[]
        digest=next((item.split('@',1)[1] for item in digests if isinstance(item,str) and re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}',item)),None)
        cpu=memory=limit=None
        if state=='running' and stats:
            try:
                cpu=float(stats['cpu'].removesuffix('%'))
                used,capacity=stats['memory'].split('/')
                memory,limit=memory_bytes(used),memory_bytes(capacity)
            except (KeyError,ValueError,TypeError,AttributeError):cpu=memory=limit=None
        items.append({'service':service,'status':state,'health':inspected.get('health','unavailable'),
                      'image_digest':digest,'image_id':inspected.get('image_id'),
                      'cpu_percent':cpu,'memory_bytes':memory,'memory_limit_bytes':limit})
    value={'schema':SCHEMA,'environment':'aws-production','observed_at':datetime.now(timezone.utc).isoformat(),'items':items}
    validate(value)
    return value


def publish(path,value):
    target=Path(path)
    fd,temporary=tempfile.mkstemp(dir=target.parent,prefix='.container-status-')
    try:
        with os.fdopen(fd,'w') as stream:
            os.fchmod(stream.fileno(),0o644);json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,target)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('/export/container-status.json'))
    args=parser.parse_args()
    try:
        publish(args.output,from_stream(sys.stdin.buffer))
        print('aws_container_snapshot=written')
    except (OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError):
        print('aws_container_snapshot=unavailable',file=sys.stderr)
        raise SystemExit(1)

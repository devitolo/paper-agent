#!/usr/bin/python3
"""One-time Mini wrapper-only migration. No permission, cron, key or data changes."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time

WRAPPER='usr/local/sbin/project-paper-observability-collect-publish'
CONTROL='var/lib/project-paper-observability/mini-production-control'
ENV='etc/project-paper-observability/mini-production/mini-production.env'
CRON='etc/cron.d/project-paper-observability-mini-production'
OLD_SHA='effd533529bb0f0db5fadfd212a88079ac36c43b08f068861c1bba68772ff20d'
NEW_SHA='bb721c206f8f705e8de6e24efbbac24a73ed7e585807e5787604e38366062614'


def digest(data):return hashlib.sha256(data).hexdigest()
def trusted(path,directory=False):
    for item in (*reversed(path.parents),path):
        info=item.lstat()
        if info.st_uid!=0 or info.st_mode&0o022 or stat.S_ISLNK(info.st_mode):raise ValueError('untrusted path')
        if item!=path or directory:
            if not stat.S_ISDIR(info.st_mode):raise ValueError('directory required')
        elif not stat.S_ISREG(info.st_mode):raise ValueError('regular file required')


def atomic(path,data,mode):
    fd,temporary=tempfile.mkstemp(dir=path.parent,prefix='.single-image-')
    try:
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),mode);stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        parent=os.open(path.parent,os.O_DIRECTORY)
        try:os.fsync(parent)
        finally:os.close(parent)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def probe(image):
    environment={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin'}
    identity=subprocess.check_output(['/usr/bin/docker','image','inspect','--format','{{.Os}}/{{.Architecture}}',image],env=environment,text=True,timeout=10).strip()
    if identity!='linux/amd64':raise ValueError('qualified AMD64 image must already be deployed')
    name='paper-observability-wrapper-probe-'+str(os.getpid())
    try:
        subprocess.run(['/usr/bin/docker','run','--rm','--pull=never','--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--user=10002:10002','--memory=64m','--pids-limit=16','--name',name,'--entrypoint=/opt/project-paper-observability/publish.sh',image,'--help'],env=environment,check=True,timeout=15,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    finally:
        subprocess.run(['/usr/bin/docker','rm','-f',name],env=environment,timeout=5,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def install(root,source,*,check_image=probe):
    root,source=Path(root),Path(source)
    fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size>32768:raise ValueError('invalid wrapper source')
        with os.fdopen(os.dup(fd),'rb') as stream:new=stream.read(32769)
    finally:os.close(fd)
    if digest(new)!=NEW_SHA:raise ValueError('reviewed wrapper digest mismatch')
    descriptors=[]
    try:
        for name in ('project-paper-observability-deploy.lock','project-paper-observability.lock'):
            lock=root/'run/lock'/name;parent=lock.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=0 or (parent.st_mode&0o022 and not parent.st_mode&stat.S_ISVTX):raise ValueError('untrusted lock parent')
            fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);descriptors.append(fd)
            info=os.fstat(fd)
            if info.st_uid!=0 or not stat.S_ISREG(info.st_mode) or info.st_mode&0o022:raise ValueError('untrusted lock')
            deadline=time.monotonic()+180
            while True:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>deadline:raise ValueError('deployment busy')
                    time.sleep(1)
        control=root/CONTROL;wrapper=root/WRAPPER;env=root/ENV;cron=root/CRON
        for path in (wrapper,env,cron):trusted(path)
        trusted(control,True)
        if stat.S_IMODE(wrapper.stat().st_mode)!=0o755:raise ValueError('unexpected wrapper mode')
        before=wrapper.read_bytes();cron_before=cron.read_bytes()
        if digest(before) not in (OLD_SHA,NEW_SHA):raise ValueError('unexpected current wrapper revision')
        if cron_before.count(b'*/15 * * * * root ')!=1 or cron_before.count(b'/usr/local/sbin/project-paper-observability-collect-publish')!=1:raise ValueError('existing combined cron required')
        values=[line.split('=',1)[1] for line in env.read_text().splitlines() if line.startswith('PROJECT_PAPER_OBSERVABILITY_IMAGE=')]
        if len(values)!=1 or not re.fullmatch(r'ghcr\.io/devitolo/paper-agent/observability:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}',values[0]):raise ValueError('immutable configured image required')
        image=values[0];check_image(image)
        if digest(before)==NEW_SHA:return {'result':'already_ready','image':image}
        backup=Path(tempfile.mkdtemp(dir=control,prefix='single-image-wrapper-'))
        parent=os.open(control,os.O_DIRECTORY)
        try:os.fsync(parent)
        finally:os.close(parent)
        atomic(backup/'wrapper.before',before,0o600)
        try:
            atomic(wrapper,new,0o755)
            if wrapper.read_bytes()!=new or cron.read_bytes()!=cron_before:raise ValueError('verification failed')
            receipt={'result':'ready','image':image,'previous_sha256':digest(before),'wrapper_sha256':NEW_SHA,'cron_sha256':digest(cron_before),'jobs_run':False}
            atomic(backup/'receipt.json',json.dumps(receipt,sort_keys=True).encode(),0o600)
            return {**receipt,'rollback_wrapper':str(backup/'wrapper.before')}
        except Exception:
            atomic(wrapper,before,0o755)
            raise
    finally:
        for fd in reversed(descriptors):os.close(fd)


if __name__=='__main__':
    import sys
    if os.geteuid()!=0 or len(sys.argv)!=1:raise SystemExit('run locally with sudo; no arguments')
    try:print(json.dumps(install(Path('/'),Path(__file__).resolve().parent/'collect-publish.sh'),sort_keys=True))
    except Exception as error:
        print('single_image_wrapper_ready=no error='+str(error));raise SystemExit(1)

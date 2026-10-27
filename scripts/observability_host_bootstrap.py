#!/usr/bin/python3 -I
"""One reviewed, local-root bootstrap; no jobs, keys, app data or cron changes."""
import sys
if __name__ == '__main__' and not sys.flags.isolated:
    raise SystemExit('run the reviewed bootstrap with /usr/bin/python3 -I')
import fcntl
import hashlib
import json
import os
import pwd
import re
from pathlib import Path
import stat
import subprocess
import tempfile
import time

CONTROL = 'var/lib/project-paper-observability/mini-production-control'
INBOX = 'var/lib/project-paper-observability/mini-production-incoming'
FILES = {
    'package-install.py': ('usr/local/sbin/project-paper-observability-package-install', 0o755),
    'gh': ('usr/local/libexec/project-paper-observability/gh', 0o755),
    'collect-publish.sh': ('usr/local/sbin/project-paper-observability-collect-publish', 0o755),
    'project-paper-observability-deploy': ('usr/local/sbin/project-paper-observability-deploy', 0o755),
    'run-container.sh': ('usr/local/sbin/project-paper-observability-container', 0o755),
    'sudoers': ('etc/sudoers.d/project-paper-observability-package', 0o440),
}
OLD_HASHES = {
    'collect-publish.sh': '0218b4ae926ab1b1738e21c729c6a80aaedd1f79622f6875409bf7dc5ddcb2ce',
    'project-paper-observability-deploy': 'fcb7b7c60a45f5b64b4294468c6b667a8e571612bbdf209d2109e8ce447ff0db',
    'run-container.sh': 'bc46b711c3c872048020a85673ddda67bcc974792372e90d25b5955a7d0c7ec7',
}
# Filled from reviewed source and the checksum/provenance-verified official CLI.
SOURCE_HASHES = {}  # Filled only by the reviewed bootstrap-bundle builder.


def sha(data):
    return hashlib.sha256(data).hexdigest()


def trusted(path, directory=False):
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise ValueError('untrusted root path')
        if item != path or directory:
            if not stat.S_ISDIR(info.st_mode):raise ValueError('directory required')
        elif not stat.S_ISREG(info.st_mode):raise ValueError('regular file required')


def sync(path):
    fd=os.open(path,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def atomic(path, data, mode):
    fd,temp=tempfile.mkstemp(prefix='.bootstrap-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),mode);stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path);sync(path.parent)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def restore(root, control):
    pending=control/'HOST-BOOTSTRAP-PENDING.json';trusted(pending)
    audit=json.loads(pending.read_bytes())
    if set(audit)!={'transaction','before'}:raise ValueError('invalid recovery record')
    if not isinstance(audit['transaction'],str) or not audit['transaction'].startswith('host-bootstrap-') or '/' in audit['transaction']:
        raise ValueError('invalid recovery transaction')
    transaction=control/audit['transaction'];trusted(transaction,True)
    if set(audit['before'])!={item[0] for item in FILES.values()}:raise ValueError('invalid recovery paths')
    snapshots=[]
    for index,(name, (relative,mode)) in enumerate(FILES.items()):
        entry=audit['before'][relative];destination=root/relative
        trusted(destination.parent,True)
        if destination.exists() or destination.is_symlink():trusted(destination)
        if set(entry)!={'sha256','mode'}:raise ValueError('invalid recovery fields')
        if entry['mode'] is None:
            if name in OLD_HASHES or entry['sha256'] is not None:raise ValueError('invalid absent recovery file')
            snapshots.append((destination,None,None));continue
        if entry['mode']!=mode:raise ValueError('unexpected backup mode')
        if entry['sha256'] not in {SOURCE_HASHES[name],OLD_HASHES.get(name)}:
            raise ValueError('unexpected bootstrap backup revision')
        previous=transaction/(str(index)+'.before');trusted(previous)
        data=previous.read_bytes()
        if sha(data)!=entry['sha256']:raise ValueError('corrupt recovery backup')
        snapshots.append((destination,data,mode))
    # Disable runner authority first; restore old wrapper before old collector,
    # and old image helper last. No recovery interruption exposes a mixed pipeline.
    order={FILES[name][0]:index for index,name in enumerate(('sudoers','package-install.py','gh','collect-publish.sh','run-container.sh','project-paper-observability-deploy'))}
    snapshots.sort(key=lambda item:order[str(item[0].relative_to(root))])
    for destination,data,mode in snapshots:
        if data is None:destination.unlink(missing_ok=True);sync(destination.parent)
        else:atomic(destination,data,mode)
    atomic(transaction/'result.json',b'{"result":"rolled_back"}',0o600)
    pending.unlink();sync(control)


def validate_existing_config(root):
    path=root/'etc/project-paper-observability/mini-production/mini-production.env'
    trusted(path)
    if stat.S_IMODE(path.stat().st_mode)!=0o640:
        raise ValueError('unexpected image config mode')
    with path.open('rb') as stream:data=stream.read(4097)
    if len(data)>4096 or b'\r' in data or not data.endswith(b'\n'):raise ValueError('unexpected image config size/encoding')
    values={}
    for line in data.decode('ascii').splitlines():
        key,sep,value=line.partition('=')
        if not sep or key in values:raise ValueError('invalid image config fields')
        values[key]=value
    expected={
        'PROJECT_PAPER_RESOURCE_ROOT':'/var/lib/project-paper-observability/mini-production',
        'PROJECT_PAPER_RESOURCE_EXPORT_DIR':'/var/lib/project-paper-observability/mini-production-export',
        'PROJECT_PAPER_RESOURCE_ENVIRONMENT':'mini-production',
        'PROJECT_PAPER_RESOURCE_HOST':'mini-production',
        'PROJECT_PAPER_RESOURCE_ENV_FILE':'/etc/project-paper-observability/mini-production/mini-production.env',
    }
    if set(values)!=set(expected)|{'PROJECT_PAPER_OBSERVABILITY_IMAGE'} or any(values[key]!=value for key,value in expected.items()):
        raise ValueError('unexpected image config target/paths')
    if not re.fullmatch(r'ghcr\.io/devitolo/paper-agent/observability:observability-mini@sha256:[0-9a-f]{64}',values['PROJECT_PAPER_OBSERVABILITY_IMAGE']):
        raise ValueError('unexpected image reference')


def bootstrap(root, source, *, qualify=None):
    """Internal roots support disposable tests; CLI has no arguments or overrides."""
    root,source=Path(root),Path(source)
    control=root/CONTROL;trusted(control,True)
    if set(SOURCE_HASHES) != set(FILES):
        raise ValueError('generate and review a pinned bootstrap bundle first')
    runner_gid=pwd.getpwnam('devitolo').pw_gid
    contents={}
    for name in FILES:
        path=source/name
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>100*1024*1024:raise ValueError('invalid source file')
            with os.fdopen(os.dup(fd),'rb') as stream:data=stream.read(100*1024*1024+1)
        finally:os.close(fd)
        if sha(data)!=SOURCE_HASHES[name]:raise ValueError('bootstrap source digest mismatch')
        contents[name]=data
    descriptors=[]
    try:
        # Wait for a natural short collector cycle; no forced jobs are run.
        for name in ('project-paper-observability-deploy.lock','project-paper-observability.lock'):
            path=root/'run/lock'/name
            parent=path.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=0 or (parent.st_mode&0o022 and not parent.st_mode&stat.S_ISVTX):
                raise ValueError('untrusted lock directory')
            fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);descriptors.append(fd)
            info=os.fstat(fd)
            if info.st_uid!=0 or not stat.S_ISREG(info.st_mode) or info.st_mode&0o022:raise ValueError('untrusted lock')
            deadline=time.monotonic()+180
            while True:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>deadline:raise ValueError('deployment busy')
                    time.sleep(1)
        if (control/'HOST-PACKAGE-PENDING.json').exists() or (control/'HOST-PACKAGE-PENDING.json').is_symlink():
            raise ValueError('package recovery required first')
        if (control/'HOST-BOOTSTRAP-PENDING.json').exists() or (control/'HOST-BOOTSTRAP-PENDING.json').is_symlink():
            restore(root,control)
        if qualify is None:validate_existing_config(root)
        cron=root/'etc/cron.d/project-paper-observability-mini-production';trusted(cron)
        cron_before=cron.read_bytes()
        if cron_before.count(b'*/15 * * * * root ')!=1 or cron_before.count(b'/usr/local/sbin/project-paper-observability-collect-publish')!=1:
            raise ValueError('expected combined cron missing')
        # Build new root directories only under independently trusted ancestors.
        for directory in (root/'usr/local/libexec',root/'usr/local/libexec/project-paper-observability'):
            trusted(directory.parent,True)
            if not directory.exists() and not directory.is_symlink():directory.mkdir(mode=0o755);sync(directory.parent)
            trusted(directory,True)
        inbox=root/INBOX;trusted(inbox.parent,True)
        if inbox.exists() or inbox.is_symlink():
            info=inbox.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_gid!=runner_gid or stat.S_IMODE(info.st_mode)!=0o1770:
                raise ValueError('unexpected inbox ownership/mode')
        else:
            inbox.mkdir(mode=0o700);os.chown(inbox,0,runner_gid);inbox.chmod(0o1770);sync(inbox.parent)
        transaction=Path(tempfile.mkdtemp(prefix='host-bootstrap-',dir=control));sync(control)
        before={}
        for index,(name,(relative,mode)) in enumerate(FILES.items()):
            destination=root/relative;trusted(destination.parent,True)
            if destination.exists() or destination.is_symlink():
                trusted(destination)
                data=destination.read_bytes();actual=sha(data)
                if stat.S_IMODE(destination.stat().st_mode)!=mode:raise ValueError('unexpected installed mode')
                if actual not in {SOURCE_HASHES[name],OLD_HASHES.get(name)}:raise ValueError('unexpected installed revision')
                before[relative]={'sha256':actual,'mode':mode}
                atomic(transaction/(str(index)+'.before'),data,0o600)
            else:
                if name in OLD_HASHES:raise ValueError('existing entrypoint missing')
                before[relative]={'sha256':None,'mode':None}
        # Validate staged sources before any active file replacements.
        for name,data in contents.items():atomic(transaction/name,data,FILES[name][1])
        if qualify is not None:qualify(transaction)
        else:
            environment={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','HOME':str(transaction),'GH_CONFIG_DIR':str(transaction)}
            subprocess.run([str(transaction/'gh'),'--version'],env=environment,check=True,timeout=10,stdout=subprocess.DEVNULL)
            compile(contents['package-install.py'],'package-install.py','exec')
            for name in ('collect-publish.sh','project-paper-observability-deploy','run-container.sh'):
                subprocess.run(['/bin/bash','-n',str(transaction/name)],check=True,timeout=10)
            subprocess.run(['/usr/sbin/visudo','-cf',str(transaction/'sudoers')],check=True,timeout=10,stdout=subprocess.DEVNULL)
        audit={'transaction':transaction.name,'before':before}
        atomic(control/'HOST-BOOTSTRAP-PENDING.json',json.dumps(audit,sort_keys=True).encode(),0o600)
        try:
            # Gate the image path first, then the collector. Before the second
            # gate no payload has changed: cron can only run the complete old
            # pipeline. Thereafter every old/new pipeline is blocked until commit.
            gate=b'#!/bin/sh\necho host_bootstrap_recovery_pending >&2\nexit 75\n'
            atomic(root/FILES['project-paper-observability-deploy'][0],gate,0o755)
            atomic(root/FILES['run-container.sh'][0],gate,0o755)
            # sudo policy is LAST: no new runner authority before code/guards exist.
            for name,(relative,mode) in FILES.items():atomic(root/relative,contents[name],mode)
            if qualify is None:
                subprocess.run(['/usr/sbin/visudo','-cf',str(root/'etc/sudoers')],check=True,timeout=10,stdout=subprocess.DEVNULL)
            if cron.read_bytes()!=cron_before:raise ValueError('cron changed concurrently')
            for name,(relative,mode) in FILES.items():
                destination=root/relative;trusted(destination)
                if sha(destination.read_bytes())!=SOURCE_HASHES[name] or stat.S_IMODE(destination.stat().st_mode)!=mode:
                    raise ValueError('installed verification failed')
            receipt={'result':'ready','transaction':transaction.name,'files':SOURCE_HASHES,'cron_sha256':sha(cron_before),'jobs_run':False}
            atomic(transaction/'result.json',json.dumps(receipt,sort_keys=True).encode(),0o600)
            atomic(control/'HOST-BOOTSTRAP-RECEIPT.json',json.dumps(receipt,sort_keys=True).encode(),0o600)
            (control/'HOST-BOOTSTRAP-PENDING.json').unlink();sync(control)
            return receipt
        except Exception:
            restore(root,control)
            raise
    finally:
        for fd in reversed(descriptors):os.close(fd)


if __name__=='__main__':
    import sys
    if os.geteuid()!=0 or len(sys.argv)!=1:raise SystemExit('run locally with sudo; no arguments')
    try:
        result=bootstrap(Path('/'),Path(__file__).resolve().parent)
        print('host_package_upgrade_ready=yes jobs_run=no transaction='+result['transaction'])
    except Exception as error:
        print('host_package_upgrade_ready=no error='+str(error))
        raise SystemExit(1)

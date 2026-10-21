#!/usr/bin/env python3
"""Explicit Mini production -> independent nonprod snapshot refresh. No schedules.

Run on the Mini as devitolo. Default is a read-only plan. --apply creates a new
snapshot and versioned volumes, retaining the old nonprod volumes for rollback.
The only production mounts are read-only. Production containers are never stopped.
"""
import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.request

ROOT = Path('/home/devitolo/workspace/paper-mini-nonprod')
PROD = 'paper-mini-production-app-1'
SOURCE = {'data': 'paper-mini-rehearsal-production-data',
          'config': 'paper-mini-rehearsal-production-config',
          'control': 'paper-mini-rehearsal-production-control'}
ROLES = {'ui': ('paper-mini-nonprod', 'app'),
         'pipeline': ('paper-mini-nonprod-pipeline', 'pipeline')}
PATHS = {'data': '/app/data', 'config': '/app/config', 'logs': '/app/logs',
         'control': '/runtime-control', 'generated': '/app/generated'}
NETWORK = 'paper-mini-nonprod-internal'
PROXY = 'paper-mini-nonprod-ingress-proxy-1'
URL = 'http://192.168.1.110:18080'
MANAGED = 'mini-nonprod-refresh-v1'


def require(value, message):
    if not value:
        raise RuntimeError(message)


def checked_path(path):
    p = Path(path)
    require(p.is_absolute() and p.is_relative_to(ROOT), 'Path outside nonprod operator root')
    require(not any(x.is_symlink() for x in (p, *p.parents)), 'Aliased operator path')
    return p


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    tmp = path.with_name(path.name + '.next')
    with tmp.open('x') as f:
        json.dump(value, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    tmp.chmod(0o600)
    tmp.replace(path)


def run(*args, timeout=60, input=None):
    # Do not allow the invoking shell to override Compose interpolation/identity.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('PAPER_', 'COMPOSE_', 'DOCKER_'))}
    result = subprocess.run(args, text=True, input=input, capture_output=True,
                            timeout=timeout, env=env)
    require(result.returncode == 0, 'Command failed: ' + args[0] + ': ' + result.stderr[-1600:])
    return result.stdout


def docker(*args, **kwargs):
    return run('docker', *args, **kwargs)


def inspect(name):
    return json.loads(docker('inspect', name))[0]


def identity(c):
    return {'id': c['Id'], 'image': c['Config']['Image'], 'running': c['State']['Running'],
            'oom': c['State']['OOMKilled'], 'restarts': c['RestartCount'],
            'mounts': sorted([(m['Type'], m.get('Name'), m['Source'], m['Destination'], m['RW'])
                              for m in c['Mounts']]),
            'networks': {n: v['NetworkID'] for n, v in c['NetworkSettings']['Networks'].items()}}


def volume_map(c):
    require(all(m['Type'] == 'volume' for m in c['Mounts']), 'Nonprod bind mount refused')
    return {m['Destination']: m['Name'] for m in c['Mounts']}


def valid_volume(role, kind, name, labels):
    project, _ = ROLES[role]
    legacy = project + '-' + ('runtime-control' if kind == 'control' else kind)
    if name == legacy:
        return True
    return (kind in {'data', 'config'} and
            re.fullmatch('paper-mini-nonprod-' + role + '-' + kind + r'-r\d{8}t\d{12}z', name) is not None
            and labels.get('org.projectpaper.managed-by') == MANAGED
            and labels.get('org.projectpaper.role') == role
            and labels.get('org.projectpaper.environment') == 'mini-nonproduction')


def old_stack(c):
    labels = c['Config']['Labels']
    env = checked_path(labels['com.docker.compose.project.environment_file'])
    files = [checked_path(x) for x in labels['com.docker.compose.project.config_files'].split(',')]
    require(env.is_file() and all(p.is_file() for p in files), 'Rollback Compose files missing')
    return {'project': labels['com.docker.compose.project'], 'env': str(env),
            'files': [str(p) for p in files],
            'hashes': {str(p): sha(p) for p in [env, *files]}}


def compose(stack, *args):
    argv = ['docker', 'compose', '--project-name', stack['project'], '--env-file', stack['env']]
    for path in stack['files']:
        argv += ['-f', path]
    return run(*argv, *args, timeout=120)


def role_config(role, image, volumes, refresh_id):
    project, service = ROLES[role]
    env = {'PAPER_APP_IMAGE': image, 'PAPER_AGENT_RUNTIME_ROLE': role,
           'PAPER_AGENT_PACKAGED': '1', 'PAPER_AGENT_SERVING_ONLY': '1',
           'PAPER_AGENT_STARTUP_MODE': 'serving-snapshot',
           'PAPER_AGENT_LIFECYCLE_DIR': '/runtime-control',
           'PAPER_GENERATED_OUTPUT_DIR': '/app/generated',
           'PAPER_DEPLOYMENT_ENV': 'non-production', 'PAPER_NON_PRODUCTION': '1',
           'PAPER_UI_ENV_LABEL': 'MINI NON-PROD — PRODUCTION SNAPSHOT',
           'PAPER_AGENT_SYNC_ENVIRONMENT': 'mini-nonprod',
           'PAPER_AGENT_SYNC_PRODUCER': 'mini-nonprod-disabled',
           'PAPER_AGENT_SYNC_SHARED_SECRET': '', 'PAPER_AGENT_MINILM_BACKEND': 'http',
           'PAPER_AGENT_MINILM_URL': 'http://127.0.0.1:9',
           'PAPER_AGENT_OLLAMA_URL': 'http://127.0.0.1:9',
           'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
           'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'}
    for flag in ('SCHEDULER_ENABLED', 'GEMINI_ENABLED', 'TELEMETRY', 'MINILM_ENABLED'):
        env['PAPER_AGENT_' + flag] = '0'
    return {'name': project, 'services': {service: {
        'image': image, 'platform': 'linux/amd64', 'pull_policy': 'never', 'restart': 'no',
        'user': '10001:10001', 'read_only': True, 'cap_drop': ['ALL'],
        'security_opt': ['no-new-privileges:true'], 'mem_limit': '512m', 'memswap_limit': '512m',
        'cpus': 1.0, 'pids_limit': 128, 'stop_grace_period': '20s',
        'environment': env, 'healthcheck': {'disable': True},
        'labels': {'org.projectpaper.environment': 'mini-nonproduction',
                   'org.projectpaper.state': 'production-snapshot',
                   'org.projectpaper.refresh': refresh_id, 'org.projectpaper.role': role},
        'tmpfs': ['/tmp:rw,noexec,nosuid,size=16m,mode=1777'],
        'volumes': [k + ':' + p for k, p in PATHS.items()], 'networks': ['isolated'],
        'logging': {'driver': 'json-file', 'options': {'max-size': '1m', 'max-file': '2'}}}},
        'networks': {'isolated': {'external': True, 'name': NETWORK}},
        'volumes': {k: {'external': True, 'name': v} for k, v in volumes.items()}}


FENCE_CODE = r'''
import fcntl,os,stat,time
from pathlib import Path
fds=[]
for name in ('/runtime-control/.scheduled-job.lock','/app/data/scout.lock'):
 p=Path(name);s=p.lstat()
 assert stat.S_ISREG(s.st_mode) and s.st_nlink==1 and not p.is_symlink()
 fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW);v=os.fstat(fd)
 assert (s.st_dev,s.st_ino)==(v.st_dev,v.st_ino)
 fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);fds.append(fd)
Path('/tmp/ready').write_text('ready')
deadline=time.monotonic()+360
while not Path('/tmp/release').exists():
 assert time.monotonic()<deadline,'Fence maximum duration exceeded'
 assert all(os.fstat(fd).st_nlink==1 for fd in fds)
 time.sleep(.2)
'''

SCAN_CODE = r'''
import os,json
from pathlib import Path
s=Path('/runtime-control/.runtime.lock').stat();holders=[]
for p in Path('/proc').iterdir():
 if not p.name.isdigit() or int(p.name) in (1,os.getpid()):continue
 try:
  for fd in (p/'fd').iterdir():
   try:
    z=fd.stat()
    if (z.st_dev,z.st_ino)==(s.st_dev,s.st_ino):holders.append(int(p.name))
   except OSError:pass
 except OSError:pass
print(json.dumps(sorted(set(holders))))
'''

CAPTURE_CODE = r'''
import hashlib,json,sqlite3,sys
from pathlib import Path
from paper_agents.nonprod_snapshot import capture
def logical(path):
 db=sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,timeout=5)
 db.execute('BEGIN'); result={}
 for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
  rows=[]
  for row in db.execute('SELECT * FROM "'+name.replace('"','""')+'"'):
   data=[{'bytes':x.hex()} if isinstance(x,bytes) else x for x in row]
   rows.append(hashlib.sha256(json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()).hexdigest())
  result[name]={'count':len(rows),'sha256':hashlib.sha256(''.join(sorted(rows)).encode()).hexdigest()}
 db.close();return result
before=logical('/source/data/paper_agent.db')
value=capture('/source','/snapshot/new',sys.argv[1],sys.argv[2])
assert before==logical('/source/data/paper_agent.db')==logical('/snapshot/new/data/paper_agent.db'),'Production DB changed during capture'
Path('/snapshot/source-logical.json').write_text(json.dumps(before,indent=2))
print(json.dumps({k:value[k] for k in ('snapshot_id','captured_at','counts','copied_bytes','source_revision','source_image')}))
'''

BACKUP_CODE = r'''
import hashlib,json,os,shutil,sqlite3,stat,sys
from pathlib import Path
role=sys.argv[1];out=Path('/snapshot/backups')/role;out.mkdir(parents=True)
files={};total=0
for folder in ('data','config'):
 for p in sorted((Path('/old')/folder).rglob('*')):
  s=p.lstat();assert not p.is_symlink() and (stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode))
  if not p.is_file():continue
  assert s.st_nlink==1;total+=s.st_size;assert total<4*1024**3
  h=hashlib.sha256()
  with p.open('rb') as f:
   for block in iter(lambda:f.read(1024**2),b''):h.update(block)
  files[str(p.relative_to('/old'))]={'bytes':s.st_size,'sha256':h.hexdigest()}
src=sqlite3.connect('file:/old/data/paper_agent.db?mode=ro',uri=True,timeout=5)
dst=sqlite3.connect(out/'paper_agent.db');src.backup(dst)
assert dst.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
assert not dst.execute('PRAGMA foreign_key_check').fetchall()
dst.close();src.close()
shutil.copytree('/old/config',out/'config')
for name in ('profile.json','NONPROD_SNAPSHOT.json','MINI_NONPROD_SYNTHETIC'):
 p=Path('/old/data')/name
 if p.exists():shutil.copyfile(p,out/name)
value={'retained_volume_files':files,'consistent_database_sha256':hashlib.sha256((out/'paper_agent.db').read_bytes()).hexdigest()}
(out/'manifest.json').write_text(json.dumps(value,indent=2));print(json.dumps({'files':len(files),'bytes':total,'consistent_database_sha256':value['consistent_database_sha256']}))
'''


class Operator:
    def __init__(self, args):
        self.args = args
        self.id = dt.datetime.now(dt.timezone.utc).strftime('r%Y%m%dt%H%M%S%fz')
        self.out = ROOT / 'refreshes' / self.id
        self.report = {'status': 'preflight', 'refresh_id': self.id}
        self.fence = None
        self.helpers = []
        self.stopped = []
        self.switched = []
        self.newstacks = {}
        self.old = {}
        self.oldstacks = {}
        self.proxy_touched = False
        self.locks = []

    def emit(self, event):
        print(json.dumps({'event': event, 'refresh_id': self.id}), flush=True)

    def helper(self, suffix, mounts, code, *args, root=False, timeout=180, detached=False):
        name = 'paper-mini-nonprod-refresh-' + self.id + '-' + suffix
        self.helpers.append(name)
        cmd = ['run', '-d' if detached else '--rm', '--name', name, '--pull', 'never',
               '--label', 'org.projectpaper.managed-by=' + MANAGED,
               '--network', 'none', '--read-only', '--user', '0:0' if root else '10001:10001',
               '--memory', '512m', '--memory-swap', '512m', '--cpus', '1', '--pids-limit', '64',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
               '--tmpfs', '/tmp:rw,noexec,nosuid,size=32m,mode=1777']
        if root:
            cmd += ['--cap-add', 'CHOWN', '--cap-add', 'DAC_READ_SEARCH']
        for volume, path, readonly in mounts:
            require(volume in self.allowed_volumes, 'Unapproved helper volume')
            require(readonly or volume in self.created, 'Only new controller volumes may be written')
            cmd += ['--mount', 'type=volume,src=' + volume + ',dst=' + path + (',readonly' if readonly else '')]
        cmd += ['--entrypoint', 'python', self.image, '-u', '-c', code, *args]
        return docker(*cmd, timeout=timeout).strip()

    def volume(self, name, role):
        require(name not in self.existing_volumes and name.startswith('paper-mini-nonprod-'), 'Volume exists or wrong namespace')
        docker('volume', 'create', '--label', 'org.projectpaper.managed-by=' + MANAGED,
               '--label', 'org.projectpaper.role=' + role,
               '--label', 'org.projectpaper.environment=mini-nonproduction',
               '--label', 'org.projectpaper.refresh=' + self.id, name)
        self.created.append(name)
        self.allowed_volumes.add(name)
        v = json.loads(docker('volume', 'inspect', name))[0]
        require(v['Driver'] == 'local' and not v.get('Options'), 'New volume not plain local storage')

    def preflight(self):
        checked_path(ROOT)
        require(Path.home() == Path('/home/devitolo') and socket.gethostname() == 'clarkmini', 'Wrong execution host/user')
        interfaces = json.loads(run('ip', '-j', '-4', 'address', 'show'))
        require(any(a.get('local') == '192.168.1.110' and a.get('prefixlen') == 24
                    for i in interfaces for a in i['addr_info']), 'Expected Mini LAN missing')
        require(shutil.disk_usage(ROOT).free > 5 * 1024**3, 'Less than 5 GiB free')
        mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        require(int(mem['MemAvailable'].split()[0]) > 1536 * 1024, 'Insufficient available memory')
        self.prod = inspect(PROD)
        require(self.prod['State']['Running'] and self.prod['State'].get('Health', {}).get('Status') == 'healthy', 'Production not healthy')
        labels = self.prod['Config']['Labels']
        require(labels['com.docker.compose.project'] == 'paper-mini-production' and labels['com.docker.compose.service'] == 'app', 'Wrong production identity')
        require('PAPER_AGENT_RUNTIME_ROLE=pipeline' in self.prod['Config']['Env'] and not self.prod['HostConfig']['PortBindings'], 'Production role changed')
        mounts = {m['Destination']: m for m in self.prod['Mounts']}
        for kind, destination in [('data', '/app/data'), ('config', '/app/config'), ('control', '/runtime-control')]:
            require(mounts[destination]['Type'] == 'volume' and mounts[destination]['Name'] == SOURCE[kind], 'Production source volume mismatch')
        self.source_image = self.prod['Config']['Image']
        self.source_revision = labels['org.opencontainers.image.revision']
        require(re.fullmatch(r'ghcr.io/devitolo/paper-agent@sha256:[a-f0-9]{64}', self.source_image), 'Production image not immutable')
        require(re.fullmatch(r'[a-f0-9]{40}', self.source_revision), 'Production source revision invalid')
        self.cron = hashlib.sha256(run('crontab', '-l').encode()).hexdigest()
        self.existing_volumes = set(docker('volume', 'ls', '--format', '{{.Name}}').splitlines())
        self.created = []
        self.allowed_volumes = set(SOURCE.values())
        used = set(SOURCE.values())
        for role, (project, service) in ROLES.items():
            c = inspect(project + '-' + service + '-1')
            require(c['State']['Running'], 'Expected nonprod role not running')
            labels = c['Config']['Labels']
            require(labels['com.docker.compose.project'] == project and labels['com.docker.compose.service'] == service, 'Nonprod identity mismatch')
            require(labels.get('org.projectpaper.environment') == 'mini-nonproduction', 'Missing NONPROD identity')
            m = volume_map(c)
            require(set(m) == set(PATHS.values()), 'Unexpected nonprod mounts')
            for kind, path in PATHS.items():
                v = json.loads(docker('volume', 'inspect', m[path]))[0]
                require(valid_volume(role, kind, m[path], v.get('Labels') or {}), 'Unknown nonprod volume namespace')
                require(v['Driver'] == 'local' and not v.get('Options') and m[path] not in used, 'Shared or remapped role volume refused')
                used.add(m[path]); self.allowed_volumes.add(m[path])
            require(set(c['NetworkSettings']['Networks']) <= {NETWORK, 'paper-mini-inference'}, 'Unexpected nonprod network')
            for name in c['NetworkSettings']['Networks']:
                require(json.loads(docker('network', 'inspect', name))[0]['Internal'], 'Nonprod egress enabled')
            self.old[role] = c
            self.oldstacks[role] = old_stack(c)
        ids = docker('ps', '-aq', '--filter', 'name=^/' + PROXY + '$').split()
        self.oldproxy = inspect(ids[0]) if ids else None
        if self.oldproxy:
            require(self.oldproxy['Config']['Labels'].get('com.docker.compose.project') == 'paper-mini-nonprod-ingress', 'Proxy identity mismatch')
            self.proxy_oldstack = old_stack(self.oldproxy)
        else:
            with socket.socket() as s:
                s.bind(('192.168.1.110', 18080))
        managed = {c['Id'] for c in self.old.values()} | ({self.oldproxy['Id']} if self.oldproxy else set())
        all_ids = docker('ps', '-aq').split()
        self.others = {c['Id']: identity(c) for c in json.loads(docker('inspect', *all_ids)) if c['Id'] not in managed}
        current = checked_path(ROOT / 'current-refresh.json')
        defaults = json.loads(current.read_text()) if current.exists() else {}
        self.image = self.args.image or defaults.get('image')
        self.revision = self.args.revision or defaults.get('revision')
        self.bundle = self.args.source_bundle or defaults.get('source_bundle')
        require(re.fullmatch(r'ghcr.io/devitolo/paper-agent@sha256:[a-f0-9]{64}', self.image or ''), 'Supply approved immutable release image')
        require(re.fullmatch(r'[a-f0-9]{40}', self.revision or '') and re.fullmatch(r'[a-f0-9]{64}', self.bundle or ''), 'Supply approved release revision/bundle')
        img = json.loads(docker('image', 'inspect', self.image))[0]
        require(self.image in img['RepoDigests'] and img['Architecture'] == 'amd64' and img['Os'] == 'linux', 'Wrong local image identity/platform')
        require(img['Config']['Labels']['org.opencontainers.image.revision'] == self.revision and
                img['Config']['Labels']['org.projectpaper.source-bundle-sha256'] == self.bundle, 'Image source mismatch')
        self.report.update(image=self.image, revision=self.revision, source_bundle=self.bundle,
                           source_image=self.source_image, source_revision=self.source_revision,
                           previous={r: identity(c) for r, c in self.old.items()},
                           rollback_stacks=self.oldstacks, cron_sha256=self.cron,
                           production=identity(self.prod))

    def acquire(self):
        for path, mode in [(ROOT / 'refresh.lock', 'a'),
                           (Path('/home/devitolo/paper-mini-rehearsal/production-cutover/deploy.lock'), 'r')]:
            require(not path.is_symlink(), 'Aliased operation lock')
            handle = path.open(mode)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.locks.append(handle)

    def fence_alive(self):
        require(self.fence and inspect(self.fence)['State']['Running'], 'Production writer fence lost')

    def release_fence(self):
        if self.fence:
            if inspect(self.fence)['State']['Running']:
                docker('exec', self.fence, 'python', '-c', "from pathlib import Path;Path('/tmp/release').touch()")
                require(docker('wait', self.fence, timeout=10).strip() == '0', 'Fence exited abnormally')
            docker('rm', self.fence)
            self.fence = None

    def capture(self):
        self.snapshot = 'paper-mini-nonprod-snapshot-' + self.id
        self.volume(self.snapshot, 'snapshot')
        self.helper('init', [(self.snapshot, '/snapshot', False)],
                    "import os;os.chmod('/snapshot',0o700);os.chown('/snapshot',10001,10001)", root=True)
        self.fence = self.helper('fence', [(SOURCE['data'], '/app/data', True),
                                  (SOURCE['control'], '/runtime-control', True)], FENCE_CODE, detached=True)
        until = time.monotonic() + 8
        while True:
            self.fence_alive()
            try:
                docker('exec', self.fence, 'python', '-c', "from pathlib import Path;assert Path('/tmp/ready').read_text()=='ready'")
                break
            except RuntimeError:
                require(time.monotonic() < until, 'Unable to acquire production writer locks')
                time.sleep(.2)
        until = time.monotonic() + 60
        while json.loads(docker('exec', PROD, 'python', '-c', SCAN_CODE)):
            self.fence_alive()
            require(time.monotonic() < until, 'Production writer did not drain; retry later')
            time.sleep(2)
        self.fence_alive()
        result = self.helper('capture', [(SOURCE['data'], '/source/data', True),
                             (SOURCE['config'], '/source/config', True),
                             (self.snapshot, '/snapshot', False)], CAPTURE_CODE,
                             self.source_revision, self.source_image, timeout=240)
        self.fence_alive()
        self.report['snapshot'] = json.loads(result)
        require(self.report['snapshot']['counts'].get('recommendations', 0) > 0, 'Source has no review queue recommendations')
        self.release_fence()
        self.report['fence_released'] = True
        self.emit('read_only_production_snapshot_verified')

    def stage(self):
        env = self.out / 'app.env'
        env.write_text('PAPER_MINI_NONPROD_IMAGE=' + self.image + '\n'); env.chmod(0o600)
        self.newvolumes = {}
        for role, (project, service) in ROLES.items():
            old = volume_map(self.old[role])
            volumes = {k: old[p] for k, p in PATHS.items()}
            for kind in ('data', 'config'):
                volumes[kind] = 'paper-mini-nonprod-' + role + '-' + kind + '-' + self.id
                self.volume(volumes[kind], role)
            require(set(volumes.values()).isdisjoint(SOURCE.values()), 'Production destination refused')
            result = self.helper('install-' + role,
                [(self.snapshot, '/snapshot', True), (volumes['data'], '/destination/data', False),
                 (volumes['config'], '/destination/config', False)],
                "from paper_agents.nonprod_snapshot import install;import json,sys;v=install('/snapshot/new','/destination',sys.argv[1]);print(json.dumps({'snapshot_id':v['snapshot_id'],'installation_id':v['installation_id']}))",
                role, root=True, timeout=240)
            self.newvolumes[role] = volumes
            self.report.setdefault('installations', {})[role] = json.loads(result)
            verified = self.helper('verify-uid-' + role,
                [(volumes['data'], '/destination/data', True),
                 (volumes['config'], '/destination/config', True)],
                "from paper_agents.nonprod_snapshot import verify;import json;v=verify('/destination');print(json.dumps({'snapshot_id':v['snapshot_id']}))")
            require(json.loads(verified)['snapshot_id'] == self.report['snapshot']['snapshot_id'], 'App UID cannot verify installed snapshot')
            path = self.out / (role + '.compose.json')
            write_json(path, role_config(role, self.image, volumes, self.id))
            self.newstacks[role] = {'project': project, 'env': str(env), 'files': [str(path)]}
            rendered = json.loads(compose(self.newstacks[role], 'config', '--format', 'json'))
            require(set(rendered['services']) == {service}, 'Unexpected services')
        # Extract the reviewed ingress file from the exact Actions image, not host main.
        ingress = self.helper('ingress-source', [],
            "from pathlib import Path;print(Path('/app/deploy/mini-nonprod/compose.ingress.yml').read_text())")
        path = self.out / 'ingress.compose.yml'; path.write_text(ingress); path.chmod(0o600)
        self.proxy_stack = {'project': 'paper-mini-nonprod-ingress', 'env': str(env), 'files': [str(path)]}
        config = json.loads(compose(self.proxy_stack, 'config', '--format', 'json'))
        proxy = config['services']['proxy']
        require(set(config['services']) == {'proxy'} and proxy['image'] == self.image and not proxy.get('volumes') and not proxy.get('environment'), 'Unexpected ingress state/credentials')
        require(len(proxy['ports']) == 1 and proxy['ports'][0]['host_ip'] == '192.168.1.110' and str(proxy['ports'][0]['published']) == '18080' and proxy['ports'][0]['target'] == 8080, 'Unsafe ingress binding')
        self.report.update(new_volumes=self.newvolumes, snapshot_volume=self.snapshot,
                           new_stacks=self.newstacks, ingress_stack=self.proxy_stack)
        write_json(self.out / 'result.json', self.report)
        self.emit('independent_snapshot_volumes_staged')

    def validate_container(self, role, c):
        expected = {p: self.newvolumes[role][k] for k, p in PATHS.items()}
        require(volume_map(c) == expected and c['Config']['Image'] == self.image, 'Wrong new role image/mounts')
        require(set(c['NetworkSettings']['Networks']) == {NETWORK} and not c['HostConfig']['PortBindings'], 'Role network/port isolation failure')
        expected_env = role_config(role, self.image, self.newvolumes[role], self.id)['services'][ROLES[role][1]]['environment']
        actual = dict(x.split('=', 1) for x in c['Config']['Env'])
        require(all(actual.get(k) == v for k, v in expected_env.items()), 'New role environment mismatch')
        require(c['HostConfig']['ReadonlyRootfs'] and not c['HostConfig']['Privileged'] and c['Config']['User'] == '10001:10001', 'Role privilege mismatch')

    def switch(self):
        if self.oldproxy and self.oldproxy['State']['Running']:
            self.proxy_touched = True
            docker('stop', '--time', '10', self.oldproxy['Id'])
        for role, c in self.old.items():
            self.stopped.append(role)
            docker('stop', '--time', '20', c['Id'], timeout=35)
            require(inspect(c['Id'])['State']['ExitCode'] == 0, 'Nonprod did not stop cleanly')
            m = volume_map(c)
            backup = self.helper('backup-' + role, [(m['/app/data'], '/old/data', True),
                                 (m['/app/config'], '/old/config', True),
                                 (self.snapshot, '/snapshot', False)], BACKUP_CODE, role, timeout=180)
            self.report.setdefault('pre_refresh_backups', {})[role] = json.loads(backup)
        write_json(self.out / 'result.json', self.report)
        for role, (_, service) in ROLES.items():
            self.switched.append(role)
            compose(self.newstacks[role], 'create', '--no-build', '--pull', 'never', '--force-recreate', service)
            cid = compose(self.newstacks[role], 'ps', '-aq', service).strip()
            self.validate_container(role, inspect(cid))
            docker('start', cid)
        self.proxy_touched = True
        compose(self.proxy_stack, 'create', '--no-build', '--pull', 'never', '--force-recreate', 'proxy')
        proxy = inspect(PROXY)
        require(not proxy['Mounts'] and proxy['Config']['Image'] == self.image, 'Ingress mount/image mismatch')
        require(proxy['HostConfig']['PortBindings'] == {'8080/tcp': [{'HostIp': '192.168.1.110', 'HostPort': '18080'}]}, 'Ingress publication mismatch')
        require(set(proxy['NetworkSettings']['Networks']) == {NETWORK, 'paper-mini-nonprod-ingress_ingress'}, 'Ingress network mismatch')
        docker('start', PROXY)
        self.emit('nonprod_roles_and_private_ingress_started')

    def verify(self):
        deadline = time.monotonic() + 90
        while True:
            try:
                ready = json.loads(docker('exec', 'paper-mini-nonprod-pipeline-pipeline-1', 'python', '-m', 'paper_agents.package_runtime', 'check-pipeline', timeout=15))
                require(ready.get('ready') and ready.get('role') == 'pipeline', 'Pipeline not ready')
                with urllib.request.urlopen(URL + '/ready', timeout=10) as response:
                    require(response.status == 200, 'Private readiness failed')
                break
            except Exception:
                require(time.monotonic() < deadline, 'Private access/readiness deadline')
                time.sleep(2)
        with urllib.request.urlopen(URL, timeout=20) as response:
            html = response.read(8 * 1024**2).decode()
        require('NON-PROD' in html and 'NON-PRODUCTION' in html and 'PRODUCTION SNAPSHOT' in html, 'Visible snapshot watermark missing')
        for role, (project, service) in ROLES.items():
            c = inspect(project + '-' + service + '-1')
            self.validate_container(role, c)
            require(c['State']['Running'] and not c['State']['OOMKilled'] and not c['RestartCount'], 'New role process failure')
            proof = json.loads(docker('exec', c['Id'], 'python', '-c',
                "import json,sqlite3;from pathlib import Path;p=json.loads(Path('/app/data/NONPROD_SNAPSHOT.json').read_text());d=sqlite3.connect('file:/app/data/paper_agent.db?mode=ro',uri=True);assert d.execute('PRAGMA integrity_check').fetchall()==[('ok',)];assert not d.execute('PRAGMA foreign_key_check').fetchall();print(json.dumps({'snapshot_id':p['snapshot_id'],'destination_role':p['destination_role'],'papers':d.execute('SELECT count(*) FROM papers').fetchone()[0],'recommendations':d.execute('SELECT count(*) FROM recommendations').fetchone()[0]}))"))
            require(proof['snapshot_id'] == self.report['snapshot']['snapshot_id'] and proof['destination_role'] == role and
                    proof['papers'] == self.report['snapshot']['counts']['papers'] and
                    proof['recommendations'] == self.report['snapshot']['counts']['recommendations'], 'Copied role counts/provenance mismatch')
            self.report.setdefault('verified_roles', {})[role] = {'identity': identity(c), 'data': proof}
        require({cid: identity(inspect(cid)) for cid in self.others} == self.others, 'Unrelated/production container drift')
        require(hashlib.sha256(run('crontab', '-l').encode()).hexdigest() == self.cron, 'Production cron changed')
        for stack in self.oldstacks.values():
            require(all(sha(p) == h for p, h in stack['hashes'].items()), 'Historical Compose changed')
        self.report.update(url=URL, host_http_verified=True, mac_http_verification='required separately',
                           production_containers_unchanged=True, cron_unchanged=True,
                           no_aws_access=True, schedules='disabled', providers='disabled', sync='disabled')

    def rollback(self):
        if self.proxy_touched:
            ids = docker('ps', '-aq', '--filter', 'name=^/' + PROXY + '$').split()
            if ids and inspect(ids[0])['State']['Running']:
                docker('stop', '--time', '10', ids[0])
        for role in reversed(self.stopped):
            project, service = ROLES[role]
            if role in self.switched:
                ids = docker('ps', '-aq', '--filter', 'name=^/' + project + '-' + service + '-1$').split()
                if ids and inspect(ids[0])['State']['Running']:
                    docker('stop', '--time', '20', ids[0], timeout=35)
                stack = self.oldstacks[role]
                require(all(sha(p) == h for p, h in stack['hashes'].items()), 'Rollback configuration drift')
                compose(stack, 'create', '--no-build', '--pull', 'never', '--force-recreate', service)
            restored = inspect(project + '-' + service + '-1')
            require(restored['Config']['Image'] == self.old[role]['Config']['Image'] and volume_map(restored) == volume_map(self.old[role]), 'Rollback image/volume mismatch')
            docker('start', restored['Id'])
        if self.proxy_touched and self.oldproxy:
            compose(self.proxy_oldstack, 'create', '--no-build', '--pull', 'never', '--force-recreate', 'proxy')
            if self.oldproxy['State']['Running']:
                docker('start', PROXY)
        self.report['rollback'] = 'previous nonprod images/volumes restored; all staged volumes retained'

    def execute(self):
        self.preflight()
        if not self.args.apply:
            print(json.dumps({'mode': 'read-only plan', 'source': SOURCE, 'roles': ROLES,
                              'image': self.image, 'url': URL, 'replace_nonprod_test_data': True,
                              'production_mutations': False, 'next': 'rerun with --apply'}, indent=2))
            return
        self.acquire()
        # Recheck under both operation locks to close the inspection/deployment race.
        self.preflight()
        (ROOT / 'refreshes').mkdir(mode=0o700, exist_ok=True)
        self.out.mkdir(mode=0o700)
        success = False
        try:
            self.capture()
            self.stage()
            self.switch()
            self.verify()
            self.report['status'] = 'deployed_host_verified'
            write_json(ROOT / 'current-refresh.json', {'refresh_id': self.id, 'image': self.image,
                       'revision': self.revision, 'source_bundle': self.bundle, 'result': str(self.out / 'result.json')})
            success = True
        except BaseException as exc:
            self.report.update(status='failed', error=str(exc))
            try:
                self.rollback()
            except BaseException as recovery:
                self.report.update(status='needs_operator_attention', rollback_error=str(recovery))
        finally:
            try:
                self.release_fence()
                self.report['fence_released'] = True
            except BaseException as exc:
                self.report['fence_cleanup_error'] = str(exc)
                success = False
            # Only owned helper containers; no role/container/volume pruning.
            for name in self.helpers:
                try:
                    ids = docker('ps', '-aq', '--filter', 'name=^/' + name + '$').split()
                    if ids:
                        c = inspect(ids[0])
                        require(c['Config']['Labels'].get('org.projectpaper.managed-by') == MANAGED, 'Helper identity mismatch')
                        if c['State']['Running']:
                            docker('stop', '--time', '3', ids[0])
                        docker('rm', ids[0])
                except BaseException as exc:
                    self.report.setdefault('helper_cleanup_errors', {})[name] = str(exc)
                    success = False
            self.report.update(retained_created_volumes=self.created,
                               finished_utc=dt.datetime.now(dt.timezone.utc).isoformat())
            if not success and self.report['status'] == 'deployed_host_verified':
                self.report['status'] = 'deployed_cleanup_needs_attention'
            try:
                write_json(self.out / 'result.json', self.report)
            finally:
                for lock in self.locks:
                    lock.close()
        self.emit(self.report['status'])
        print(json.dumps({'result': str(self.out / 'result.json'), 'url': URL if success else None}))
        require(success, 'Refresh failed; inspect saved report/rollback status')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--image')
    parser.add_argument('--revision')
    parser.add_argument('--source-bundle')
    args = parser.parse_args()
    os.umask(0o077)
    def interrupted(*_args):
        raise RuntimeError('Operator interrupted')
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupted)
    Operator(args).execute()


if __name__ == '__main__':
    main()

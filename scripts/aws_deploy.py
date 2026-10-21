#!/usr/bin/python3 -I
"""Installed root-owned AWS release controller. Standard library only; never sourced.

Install reviewed bytes as /usr/local/sbin/project-paper-aws-deploy. Configuration,
release approvals, fencing and installation are separate operator gates.
"""
import argparse
from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import syslog
import tempfile
import urllib.parse
import urllib.request
import time
import uuid

CONFIG = Path('/etc/project-paper/aws-deploy.json')
STATE = Path('/var/lib/project-paper-deploy')
UNIT = 'project-paper-aws-deploy.service'
IMAGE = re.compile(r'ghcr\.io/devitolo/paper-agent@sha256:[0-9a-f]{64}')
TERMINAL = {'succeeded', 'rolled_back', 'failed', 'incident'}
UNTOUCHED = {'queued', 'preflight', 'pulling'}
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root',
       'LANG': 'C.UTF-8', 'DOCKER_HOST': 'unix:///var/run/docker.sock'}


# Executed only inside the app container; credentials never reach the runner.
SYNC_HEALTH_PROBE = r'''
import json, time, urllib.error, urllib.request
from paper_agents.sync_exchange import signature
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
for method, path, body in (("GET","/sync/input",b""),("POST","/sync/pipeline-results",b"{}")):
    url="http://127.0.0.1:8000"+path
    data=body if method=="POST" else None
    # Verify unauthenticated traffic cannot use even the read-only probe.
    try:
        with opener.open(urllib.request.Request(url, data=data, method=method,
                headers={"X-Paper-Sync-Probe":"1"}), timeout=5):
            raise RuntimeError("sync probe accepted unsigned request")
    except urllib.error.HTTPError as error:
        try:
            if error.code!=401: raise RuntimeError("sync probe auth status mismatch")
        finally: error.close()
    timestamp=str(time.time())
    headers={"Content-Type":"application/json","X-Paper-Sync-Probe":"1",
        "X-Paper-Sync-Timestamp":timestamp,
        "X-Paper-Sync-Signature":signature(method,path,timestamp,body)}
    with opener.open(urllib.request.Request(url,data=data,method=method,headers=headers),timeout=5) as response:
        assert response.status==200 and response.url==url
        result=response.read(4097)
        assert len(result)<=4096
        assert json.loads(result)=={"schema":1,"status":"ready","route":path,"read_only":True}
'''


# Runs from the root-owned controller against read-only copied-state mounts in
# each approved image. Uses existing application schema helpers; never migrates.
READ_ONLY_VALIDATOR = r'''
from contextlib import closing
import json, sqlite3
from pathlib import Path
from paper_agents.import_state import schema, trusted_schemas
from paper_agents.sync_exchange import _setup
from paper_agents.sync_backup import _safe
from paper_agents.topics import load_topic_config
root=Path("/app")
for relative in ("data/paper_agent.db","data/profile.json","config/topics.yaml","config/industry.json"):
    path=_safe(root,relative)
    assert path.is_file(), "required state missing"
profile=json.loads((root/"data/profile.json").read_text())
assert isinstance(profile,dict) and all(isinstance(profile.get(k),list) for k in
    ("interests","positive_signals","negative_signals","feedback_history")), "invalid profile"
load_topic_config(root/"config/topics.yaml")
assert isinstance(json.loads((root/"config/industry.json").read_text()),dict), "invalid industry config"
with closing(sqlite3.connect(":memory:")) as metadata_db:
    _setup(metadata_db)
    optional={(r[0],r[1]):r for r in schema(metadata_db)}
with closing(sqlite3.connect((root/"data/paper_agent.db").as_uri()+"?mode=ro",uri=True)) as db:
    assert db.execute("PRAGMA integrity_check").fetchall()==[("ok",)], "integrity failed"
    assert not db.execute("PRAGMA foreign_key_check").fetchall(), "foreign keys failed"
    actual={(r[0],r[1]):r for r in schema(db)}
    valid=False
    for expected_rows in trusted_schemas():
        required={(r[0],r[1]):r for r in expected_rows}
        if all(actual.get(k)==v for k,v in required.items()) and all(
                k in required or optional.get(k)==v for k,v in actual.items()):
            valid=True
            break
    assert valid, "unapproved schema"
    for kind,stored in db.execute("SELECT artifact_type,path FROM artifacts"):
        if "summary" in kind:
            assert stored.startswith("data/") and _safe(root,stored).is_file(), "summary missing"
'''


class Blocked(RuntimeError):
    """Only constant, non-sensitive error codes may cross the CLI boundary."""


def require(condition, code):
    if not condition:
        raise Blocked(code)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def protected(path, directory=False):
    p = Path(path)
    require(p.is_absolute(), 'path_not_absolute')
    for item in [p, *p.parents]:
        s = item.lstat()
        require(not stat.S_ISLNK(s.st_mode) and s.st_uid == 0
                and not s.st_mode & 0o022, 'untrusted_host_path')
    require(p.is_dir() if directory else p.is_file(), 'host_path_missing')
    return p


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.new')
    with temporary.open('w') as out:
        os.chmod(temporary, 0o600)
        json.dump(value, out, sort_keys=True)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked():
    protected(STATE, directory=True)
    fd = os.open(STATE / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked('deployment_busy') from None
        yield
    finally:
        os.close(fd)


def load_config():
    c = json.loads(protected(CONFIG).read_text())
    require(c.get('enabled') is True and c.get('security_review')
            and c.get('resource_rehearsal'), 'operator_gates_unapproved')
    for key in ('minimum_available_bytes', 'minimum_free_swap_bytes',
                'minimum_free_disk_bytes', 'minimum_free_inodes',
                'maximum_memory_psi_avg10', 'sample_seconds', 'oom_lookback_seconds',
                'command_timeout_seconds', 'fence_timeout_seconds',
                'health_timeout_seconds', 'health_stability_seconds',
                'validator_memory_bytes', 'validator_memory_swap_bytes',
                'validator_cpus', 'validator_pids'):
        require(type(c.get(key)) in (int, float) and c[key] > 0, 'resource_gate_unset')
    require(c['sample_seconds'] <= 60 and c['command_timeout_seconds'] <= 180
            and c['fence_timeout_seconds'] <= 120 and c['health_timeout_seconds'] <= 180
            and c['health_stability_seconds'] <= 60, 'unbounded_timeout')
    require(c['project'] == 'project-paper-prod' and c['service'] == 'app'
            and c['container'] == 'project-paper-prod-app-1', 'wrong_production_identity')
    protected(c['project_dir'], directory=True)
    protected(c['backup_root'], directory=True)
    protected(c['image_env_file'])
    require(c['compose_files'] and c['expected_mounts'] and c['storage_roots'], 'host_contract_missing')
    for path in [*c['compose_files'], c['env_file'], c['fence_adapter'], c['registry_auth_adapter']]:
        protected(path)
        require(c['protected_files'].get(path) == digest(path), 'host_config_drift')
    require(c['fence_sha256'] == digest(c['fence_adapter']), 'fence_adapter_drift')
    for mount in c['expected_mounts']:
        require(set(mount) == {'Type', 'Name', 'Source', 'Destination', 'RW'}
                and mount['Type'] == 'volume', 'unapproved_mount_contract')
        require(Path(mount['Source']).is_absolute(), 'mount_source_not_absolute')
    url = urllib.parse.urlparse(c['private_health_url'])
    require(url.scheme == 'https' and url.hostname and url.hostname.endswith('.ts.net')
            and not url.username and not url.password and url.path in ('', '/')
            and not url.query and not url.fragment and url.port in (None, 443), 'private_https_origin_invalid')
    require(isinstance(c.get('secret_mounts'), list), 'secret_mount_contract_missing')
    for mount in c['secret_mounts']:
        require(set(mount) == {'Type', 'Name', 'Source', 'Destination', 'RW'}
                and mount['Type'] == 'bind' and mount['Name'] is None
                and mount['RW'] is False, 'unapproved_secret_mount')
        path = str(protected(mount['Source']))
        require(c['protected_files'].get(path) == digest(path), 'secret_source_drift')
    return c


def release(c, image, revision, bundle, run_id, run_attempt):
    require(IMAGE.fullmatch(image) is not None and re.fullmatch('[0-9a-f]{40}', revision)
            and re.fullmatch('[0-9a-f]{64}', bundle), 'invalid_release_identity')
    r = c['releases'].get(image, {})
    require(r.get('revision') == revision and r.get('bundle') == bundle
            and r.get('schema_compatible') is True and r.get('rehearsal_evidence')
            and type(r.get('storage_budget_bytes')) is int and r['storage_budget_bytes'] > 0,
            'release_not_approved')
    now = time.time()
    require(re.fullmatch('[0-9]+', run_id) and re.fullmatch('[0-9]+', run_attempt)
            and r.get('run_id') == run_id and r.get('run_attempt') == run_attempt
            and r.get('approved_by') and re.fullmatch('[0-9a-f]{32}', r.get('approval_id', ''))
            and type(r.get('approved_at')) is int and type(r.get('expires_at')) is int
            and r['approved_at'] <= now < r['expires_at'], 'approval_expired_or_wrong_run')
    return r


def run(args, timeout=120, env=None, input_text=None):
    try:
        p = subprocess.run(args, check=False, capture_output=True, text=True,
                           timeout=timeout, env=env or ENV, cwd='/', input=input_text)
    except (subprocess.TimeoutExpired, OSError):
        raise Blocked('host_command_unavailable_or_timed_out') from None
    require(p.returncode == 0, 'host_command_failed')
    return p.stdout


def schema_signature(path):
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as db:
        require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
                and not db.execute('PRAGMA foreign_key_check').fetchall(), 'backup_database_invalid')
        rows = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def manifest(root):
    files = {}
    for p in sorted(Path(root).rglob('*')):
        require(not p.is_symlink(), 'backup_symlink_rejected')
        if p.is_file():
            files[str(p.relative_to(root))] = digest(p)
        else:
            require(p.is_dir(), 'backup_special_file_rejected')
    return files


def snapshot(data, config, destination):
    """Caller must hold and independently verify the durable all-writer fence."""
    data, config, destination = map(Path, (data, config, destination))
    require(not destination.exists() and (data / 'paper_agent.db').is_file(), 'backup_destination_invalid')
    # Check before copying: copytree must not follow symlinks out of approved volumes.
    manifest(data)
    manifest(config)
    destination.mkdir(mode=0o700)
    shutil.copytree(data, destination / 'data', ignore=shutil.ignore_patterns('paper_agent.db', 'paper_agent.db-wal', 'paper_agent.db-shm'))
    shutil.copytree(config, destination / 'config')
    with closing(sqlite3.connect((data / 'paper_agent.db').as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(destination / 'data/paper_agent.db')) as target:
            source.backup(target)
            target.execute('PRAGMA journal_mode=DELETE')
    schema = schema_signature(destination / 'data/paper_agent.db')
    files = manifest(destination)
    require(manifest(destination) == files, 'backup_checksum_failed')
    # Sync copied content, not just its manifest, before marking backup complete.
    for p in destination.rglob('*'):
        if p.is_file():
            with p.open('rb') as f:
                os.fsync(f.fileno())
    for p in [*sorted((p for p in destination.rglob('*') if p.is_dir()), reverse=True), destination, destination.parent]:
        fd = os.open(p, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    atomic_json(destination.parent / (destination.name + '.manifest.json'), {'files': files, 'schema': schema})
    return schema


def state_identity(root):
    files = manifest(root)
    files.pop('data/paper_agent.db', None)
    h = hashlib.sha256(json.dumps(files, sort_keys=True).encode())
    with closing(sqlite3.connect((Path(root) / 'data/paper_agent.db').as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA cache_size=-1024')
        db.execute('PRAGMA temp_store=FILE')
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        for (name,) in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            columns = [row[1] for row in db.execute('PRAGMA table_info(' + quoted + ')')]
            order = ','.join('"' + column.replace('"', '""') + '"' for column in columns)
            h.update(repr(name).encode())
            for row in db.execute('SELECT * FROM ' + quoted + ' ORDER BY ' + order):
                h.update(repr(row).encode())
                h.update(b'\n')
    return h.hexdigest()


class Host:
    def __init__(self, config, transaction):
        self.c, self.t = config, transaction
        self.dir = STATE / transaction['id']
        self.dir.mkdir(mode=0o700, exist_ok=True)
        self.registry_env = None
        # A previous worker may have died before its credentials finally-block.
        for stale in self.dir.glob('registry-auth-*'):
            require(stale.is_dir() and not stale.is_symlink(), 'invalid_auth_staging')
            shutil.rmtree(stale)
        (self.dir / 'docker-empty').mkdir(mode=0o700, exist_ok=True)

    def save(self, phase):
        self.t['phase'] = phase
        atomic_json(self.dir / 'transaction.json', self.t)
        atomic_json(STATE / 'current.json', self.t)

    def command(self, args, timeout=None):
        env = self.registry_env or {**ENV, 'DOCKER_CONFIG': str(self.dir / 'docker-empty')}
        return run(args, timeout or self.c['command_timeout_seconds'], env)

    @contextmanager
    def registry_auth(self):
        adapter = protected(self.c['registry_auth_adapter'])
        require(digest(adapter) == self.c['protected_files'][str(adapter)], 'registry_adapter_drift')
        credentials = json.loads(self.command([str(adapter), 'pull-token']))
        require(re.fullmatch('[A-Za-z0-9_-]{1,64}', credentials.get('username', ''))
                and isinstance(credentials.get('password'), str)
                and 1 <= len(credentials['password']) <= 4096
                and type(credentials.get('expires_at')) is int
                and time.time() < credentials['expires_at'] <= time.time() + 900,
                'registry_credentials_invalid')
        with tempfile.TemporaryDirectory(prefix='registry-auth-', dir=self.dir) as directory:
            self.registry_env = {**ENV, 'DOCKER_CONFIG': directory}
            try:
                run(['docker', 'login', 'ghcr.io', '--username', credentials['username'], '--password-stdin'],
                    self.c['command_timeout_seconds'], self.registry_env, credentials['password'])
                yield
            finally:
                self.registry_env = None
                credentials.clear()

    def incident(self):
        self.t['phase'] = 'incident'
        try:
            self.save('incident')
        except Exception:
            pass
        try:
            path = STATE / ('incident-' + self.t['id'] + '-' + str(time.time_ns()))
            with path.open('x') as out:
                out.write('Unresolved deployment transaction ' + self.t['id'] + '\n')
                out.flush()
                os.fsync(out.fileno())
        except Exception:
            pass
        try:
            syslog.syslog(syslog.LOG_CRIT, 'Project Paper deployment incident: ' + self.t['id'])
        except Exception:
            pass

    def private_health(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        for endpoint in ('/ready', '/runtime'):
            with opener.open(self.c['private_health_url'].rstrip('/') + endpoint, timeout=5) as response:
                require(response.status == 200, 'private_https_health_failed')
                payload = json.loads(response.read(1024 * 1024))
                require(payload.get('ready') is True, 'private_https_not_ready')
                if endpoint == '/runtime':
                    require(payload.get('mode') == 'serving-only', 'private_https_wrong_mode')

    def compose(self, image, *args):
        command = ['docker', 'compose', '--project-directory', self.c['project_dir'],
                   '--project-name', self.c['project'], '--env-file', self.c['env_file'],
                   '--env-file', self.c['image_env_file']]
        for path in self.c['compose_files']:
            command += ['-f', path]
        return run(command + list(args), self.c['command_timeout_seconds'],
                   {**ENV, 'DOCKER_CONFIG': str(self.dir / 'docker-empty'), 'PAPER_APP_IMAGE': image})

    def selected_image(self):
        text = protected(self.c['image_env_file']).read_text()
        require(text.startswith('PAPER_APP_IMAGE=') and text.endswith('\n')
                and text.count('\n') == 1, 'invalid_canonical_image_file')
        image = text.strip().split('=', 1)[1]
        require(IMAGE.fullmatch(image), 'canonical_image_not_immutable')
        return image

    def select(self, image):
        path = protected(self.c['image_env_file'])
        temp = path.with_suffix('.new')
        with temp.open('w') as out:
            os.chmod(temp, 0o600)
            out.write('PAPER_APP_IMAGE=' + image + '\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def inspect(self):
        values = json.loads(self.command(['docker', 'inspect', self.c['container']]))
        require(len(values) == 1, 'container_identity_missing')
        obj = values[0]
        labels = obj['Config']['Labels']
        require(labels.get('com.docker.compose.project') == self.c['project']
                and labels.get('com.docker.compose.service') == self.c['service'], 'container_identity_drift')
        mounts = [{k: m.get(k) for k in ('Type', 'Name', 'Source', 'Destination', 'RW')} for m in obj['Mounts']]
        require(sorted(mounts, key=lambda m: m['Destination']) ==
                sorted(self.c['expected_mounts'] + self.c['secret_mounts'], key=lambda m: m['Destination']), 'volume_identity_drift')
        # Environment contains secrets; persist only a digest of the invariant contract.
        env = sorted(v for v in obj['Config']['Env'] if not v.startswith('PAPER_APP_IMAGE='))
        require('PAPER_AGENT_SERVING_ONLY=1' in env and 'PAPER_AGENT_STARTUP_MODE=serving' in env
                and 'PAPER_AGENT_PACKAGED=1' in env, 'not_serving_runtime')
        contract = {'env': env, 'mounts': mounts, 'ports': obj['HostConfig']['PortBindings'],
                    'restart': obj['HostConfig']['RestartPolicy'], 'memory': obj['HostConfig']['Memory'],
                    'swap': obj['HostConfig']['MemorySwap'], 'user': obj['Config']['User'],
                    'entrypoint': obj['Config']['Entrypoint'], 'cmd': obj['Config']['Cmd']}
        signature = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
        if self.t.get('contract'):
            require(signature == self.t['contract'], 'runtime_or_secret_identity_drift')
        else:
            self.t['contract'] = signature
        return obj

    def validate_compose(self, image):
        model = json.loads(self.compose(image, 'config', '--format', 'json'))
        require(set(model['services']) == {self.c['service']}, 'unexpected_compose_services')
        service = model['services'][self.c['service']]
        require(service['image'] == image and 'build' not in service, 'compose_image_or_build_invalid')
        expected = {m['Name'] for m in self.c['expected_mounts']}
        require({v['name'] for v in model['volumes'].values()} == expected
                and all(v.get('external') is True for v in model['volumes'].values()), 'volumes_not_existing_external')
        for name in expected:
            self.command(['docker', 'volume', 'inspect', name])
        volumes = service.get('volumes', [])
        desired = sorted((model['volumes'][v['source']]['name'], v['target'], not v.get('read_only', False))
                         for v in volumes if v['type'] == 'volume')
        secrets = sorted((v['source'], v['target'], not v.get('read_only', False))
                         for v in volumes if v['type'] == 'bind')
        # Compose top-level secrets also materialize as read-only bind mounts.
        for secret in service.get('secrets', []):
            source = model.get('secrets', {}).get(secret['source'], {}).get('file')
            require(source is not None, 'unsupported_compose_secret')
            target = secret.get('target', secret['source'])
            if not target.startswith('/'):
                target = '/run/secrets/' + target
            secrets.append((source, target, False))
        require(len(desired) + sum(v['type'] == 'bind' for v in volumes) == len(volumes)
                and desired == sorted((m['Name'], m['Destination'], m['RW']) for m in self.c['expected_mounts'])
                and sorted(secrets) == sorted((m['Source'], m['Destination'], m['RW']) for m in self.c['secret_mounts']),
                'compose_mount_drift')
        # Candidate must change only the image reference and PAPER_APP_IMAGE.
        service['image'] = '<release>'
        service.get('environment', {}).pop('PAPER_APP_IMAGE', None)
        signature = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()
        if self.t.get('compose_contract'):
            require(signature == self.t['compose_contract'], 'compose_contract_drift')
        else:
            self.t['compose_contract'] = signature

    def resource_sample(self):
        mem = {line.split(':')[0]: int(line.split()[1]) * 1024
               for line in Path('/proc/meminfo').read_text().splitlines()}
        vm = dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())
        psi = Path('/proc/pressure/memory').read_text()
        pressure = max(float(v) for v in re.findall(r'avg10=([0-9.]+)', psi))
        require(mem['MemAvailable'] >= self.c['minimum_available_bytes']
                and mem['SwapFree'] >= self.c['minimum_free_swap_bytes']
                and pressure <= self.c['maximum_memory_psi_avg10'], 'memory_or_swap_pressure')
        require(mem['SwapTotal'] > 0, 'swap_unavailable')
        return {'available': mem['MemAvailable'], 'swap_free': mem['SwapFree'], 'pressure': pressure,
                'oom': int(vm['oom_kill']), 'swapin': int(vm['pswpin']), 'swapout': int(vm['pswpout'])}

    def preflight(self):
        require(digest(CONFIG) == self.t['config_hash'], 'host_config_changed')
        for path, expected in self.c['protected_files'].items():
            require(digest(protected(path)) == expected, 'host_config_drift')
        first = self.resource_sample()
        time.sleep(self.c['sample_seconds'])
        second = self.resource_sample()
        require(all(first[k] == second[k] for k in ('oom', 'swapin', 'swapout'))
                and second['swap_free'] >= first['swap_free'], 'rising_swap_or_oom')
        if 'initial_oom' in self.t:
            require(second['oom'] == self.t['initial_oom'], 'oom_during_deployment')
        self.t.setdefault('initial_oom', second['oom'])
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        require(boot == self.t.setdefault('boot_id', boot), 'host_rebooted')
        journal = self.command(['journalctl', '-k', '-b', '--since',
                                str(-int(self.c['oom_lookback_seconds'])) + ' seconds', '--no-pager', '--output=json'])
        # Missing journal access is not evidence that there were no OOM events.
        baseline = self.command(['journalctl', '-k', '-b', '-n', '1', '--no-pager', '--output=json'])
        require(baseline.strip(), 'kernel_history_unavailable')
        require(not re.search(r'out of memory|oom-kill|killed process', journal, re.I), 'recent_oom')
        docker_root = json.loads(self.command(['docker', 'info', '--format', '{{json .DockerRootDir}}']))
        roots = {docker_root, self.c['project_dir'], self.c['backup_root'],
                 *self.c['storage_roots'], *(m['Source'] for m in self.c['expected_mounts'])}
        budget = self.c['releases'][self.t['image']]['storage_budget_bytes']
        for root in roots:
            s = os.statvfs(root)
            require(s.f_bavail * s.f_frsize >= self.c['minimum_free_disk_bytes'] + budget
                    and s.f_favail >= self.c['minimum_free_inodes'], 'insufficient_storage')
        self.command(['docker', 'system', 'df', '--format', '{{json .}}'])
        atomic_json(self.dir / ('resources-' + str(time.time_ns()) + '.json'), second)

    def image(self, reference, revision=None, bundle=None):
        obj = json.loads(self.command(['docker', 'image', 'inspect', reference]))[0]
        require(obj['Os'] == 'linux' and obj['Architecture'] == 'amd64'
                and reference in obj.get('RepoDigests', []), 'image_platform_or_digest_mismatch')
        labels = obj['Config']['Labels']
        require(labels.get('org.projectpaper.runtime') == 'cloud-production', 'wrong_image_runtime')
        if revision:
            require(labels.get('org.opencontainers.image.revision') == revision
                    and labels.get('org.opencontainers.image.source-bundle-sha256') == bundle,
                    'image_source_mismatch')
        return obj['Id']

    def fence(self, action):
        require(digest(protected(self.c['fence_adapter'])) == self.c['fence_sha256'], 'fence_adapter_drift')
        result = json.loads(self.command([self.c['fence_adapter'], action, self.t['id']],
                                         self.c['fence_timeout_seconds']))
        if action != 'release':
            require(result.get('transaction') == self.t['id'] and result.get('fenced') is True
                    and result.get('durable') is True and result.get('active_writers') == 0
                    and result.get('unknown_writers') is False, 'writes_not_proven_fenced')
        else:
            require(result.get('transaction') == self.t['id'] and result.get('released') is True,
                    'fence_release_unconfirmed')

    def backup(self, name):
        self.fence('status')
        mounts = {m['Destination']: m['Source'] for m in self.c['expected_mounts']}
        path = Path(self.c['backup_root']) / (self.t['id'] + '-' + name)
        signature = snapshot(mounts['/app/data'], mounts['/app/config'], path)
        self.fence('status')
        return path, signature

    def compatible(self, image, copied):
        # Approved image executes only against the copied state, read-only, no network,
        # no secrets environment, no host socket and no model/provider calls.
        name = 'paper-aws-validation-' + self.t['id']
        self.cleanup_validator()
        try:
            self.command(['docker', 'run', '--name', name, '--rm', '--pull=never', '--network=none', '--read-only',
                      '--user=0:0', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                      '--memory', str(self.c['validator_memory_bytes']),
                      '--memory-swap', str(self.c['validator_memory_swap_bytes']),
                      '--cpus', str(self.c['validator_cpus']), '--pids-limit', str(self.c['validator_pids']),
                      '--mount', 'type=bind,src=' + str(copied / 'data') + ',dst=/app/data,readonly',
                      '--mount', 'type=bind,src=' + str(copied / 'config') + ',dst=/app/config,readonly',
                      '--entrypoint', 'python', image, '-c',
                      READ_ONLY_VALIDATOR])
        finally:
            self.cleanup_validator()

    def cleanup_validator(self):
        name = 'paper-aws-validation-' + self.t['id']
        found = self.command(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$'])
        if found.strip():
            self.command(['docker', 'rm', '-f', name])

    def healthy(self, image):
        expected = self.image(image)
        deadline = time.monotonic() + self.c['health_timeout_seconds']
        stable = None
        while time.monotonic() < deadline:
            obj = self.inspect()
            state = obj['State']
            require(not state['OOMKilled'] and obj['RestartCount'] == 0, 'app_oom_or_restart')
            if obj['Image'] == expected and state['Running'] and state.get('Health', {}).get('Status') == 'healthy':
                # Read endpoint bodies only in the container; never print private UI/config.
                try:
                    self.command(['docker', 'exec', self.c['container'], 'python', '-c',
                        'import json,urllib.request; '
                        'paths=("/ready","/","/topics?mode=agent","/health","/runtime"); '
                        '\nfor path in paths:\n'
                        ' with urllib.request.urlopen("http://127.0.0.1:8000"+path,timeout=5) as r:\n'
                        '  assert r.status==200 and r.url=="http://127.0.0.1:8000"+path\n'
                        '  b=r.read()\n'
                        '  if path in ("/ready","/runtime"):\n'
                        '   p=json.loads(b); assert p.get("ready") is True\n'
                        '   if path=="/runtime": assert p.get("mode")=="serving-only"\n'
                        'from paper_agents.gemini_runtime import readiness_error\n'
                        'assert readiness_error() is None\n'])
                    # Old retained images need only their already qualified public
                    # health contract; the candidate must also pass signed probes.
                    if image == self.t['image']:
                        self.command(['docker', 'exec', self.c['container'], 'python', '-c', SYNC_HEALTH_PROBE])
                    stable = stable or time.monotonic()
                    if time.monotonic() - stable >= self.c['health_stability_seconds']:
                        self.private_health()
                        return
                except (Blocked, OSError, ValueError):
                    stable = None
            else:
                stable = None
            time.sleep(2)
        raise Blocked('health_verification_failed')

    def replace(self, image):
        self.fence('status')
        self.validate_compose(image)
        self.compose(image, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', self.c['service'])

    def deploy(self):
        self.save('preflight')
        self.t['previous'] = self.selected_image()
        self.validate_compose(self.t['previous'])
        self.validate_compose(self.t['image'])
        current = self.inspect()
        # The running image must be available under a registry digest for rollback.
        require(current['Image'] == self.image(self.t['previous']), 'rollback_image_not_retained')
        self.healthy(self.t['previous'])
        self.preflight()
        self.save('pulling')
        with self.registry_auth():
            self.command(['docker', 'pull', self.t['image']])
        self.image(self.t['image'], self.t['revision'], self.t['bundle'])
        self.preflight()
        self.save('fencing')
        self.fence('acquire')
        self.save('backup')
        copied, signature = self.backup('before')
        self.t['schema'] = signature
        self.t['state_identity'] = state_identity(copied)
        self.compatible(self.t['image'], copied)
        self.compatible(self.t['previous'], copied)
        self.preflight()
        self.healthy(self.t['previous'])
        self.fence('status')
        self.save('replacing')  # Durable before ANY app interruption.
        self.replace(self.t['image'])
        self.save('verifying')
        self.healthy(self.t['image'])
        after, signature = self.backup('after')
        require(signature == self.t['schema'], 'candidate_changed_schema')
        require(state_identity(after) == self.t['state_identity'], 'candidate_changed_fenced_state')
        self.compatible(self.t['previous'], after)
        self.preflight()
        self.save('committing')
        self.select(self.t['image'])
        self.save('releasing')
        self.fence('release')
        self.save('succeeded')

    def recover(self):
        phase = self.t['phase']
        if phase in TERMINAL:
            return
        if phase in UNTOUCHED:
            self.save('failed')
            return
        # A partially completed release can have accepted writes. Never restore data
        # or automatically restart rollback after the public write fence is opened.
        if phase in {'releasing', 'releasing_rollback'}:
            self.incident()
            return
        try:
            require(digest(CONFIG) == self.t['config_hash'], 'recovery_config_changed')
            self.fence('status')
            if phase in {'replacing', 'verifying', 'committing', 'rolling_back'}:
                self.save('rolling_back')
                copied, signature = self.backup('recovery-' + str(time.time_ns()))
                require(signature == self.t['schema'], 'rollback_schema_changed')
                require(state_identity(copied) == self.t['state_identity'], 'rollback_state_changed')
                self.compatible(self.t['previous'], copied)
                self.replace(self.t['previous'])
                self.select(self.t['previous'])
            self.healthy(self.t['previous'])
            self.save('releasing_rollback')
            self.fence('release')
            self.save('rolled_back')
        except Exception:
            self.incident()



def recovery_entry_failure():
    """Record a config/approval failure without allowing unvalidated host actions."""
    try:
        protected(STATE, directory=True)
        t = json.loads(protected(STATE / 'current.json').read_text())
        require(re.fullmatch('[0-9a-f]{32}', t.get('id', '')), 'invalid_transaction_id')
        if t.get('phase') in TERMINAL:
            return
        host = object.__new__(Host)
        host.t = t
        host.dir = protected(STATE / t['id'], directory=True)
        host.incident()
    except Exception:
        try:
            syslog.syslog(syslog.LOG_CRIT, 'Project Paper recovery entry failed; inspect protected host state')
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['submit', 'status', 'run', 'recover'])
    parser.add_argument('values', nargs='*')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'root_helper_required')
    os.umask(0o077)
    if args.action == 'status':
        require(len(args.values) == 1 and IMAGE.fullmatch(args.values[0]), 'invalid_status_request')
        t = json.loads(protected(STATE / 'current.json').read_text())
        require(t['image'] == args.values[0], 'different_transaction')
        print(json.dumps({k: t[k] for k in ('id', 'image', 'phase')}))
        return 0 if t['phase'] == 'succeeded' else (1 if t['phase'] in TERMINAL else 3)
    with locked():
        try:
            c = load_config()
        except Exception:
            if args.action in {'run', 'recover'}:
                recovery_entry_failure()
            raise
        current = STATE / 'current.json'
        if args.action == 'submit':
            require(len(args.values) == 5, 'invalid_submit_request')
            require(run(['systemctl', 'show', UNIT, '--property=ActiveState', '--value']).strip()
                    in {'inactive', 'failed'}, 'host_service_busy')
            image, revision, bundle, run_id, run_attempt = args.values
            approval = release(c, image, revision, bundle, run_id, run_attempt)
            used = STATE / ('approval-' + approval['approval_id'])
            require(not used.exists(), 'approval_already_consumed')
            if current.exists():
                previous = json.loads(protected(current).read_text())
                require(previous['phase'] in TERMINAL - {'incident'}, 'unfinished_transaction')
            with used.open('x') as marker:
                marker.write(run_id + '/' + run_attempt)
                marker.flush()
                os.fsync(marker.fileno())
            t = {'id': uuid.uuid4().hex, 'image': image, 'revision': revision,
                 'run_id': run_id, 'run_attempt': run_attempt,
                 'bundle': bundle, 'config_hash': digest(CONFIG)}
            Host(c, t).save('queued')
        else:
            require(not args.values, 'unexpected_arguments')
            if not current.exists():
                return 0
            t = json.loads(protected(current).read_text())
            if t['phase'] in TERMINAL:
                return 0
            host = Host(c, t)
            try:
                host.cleanup_validator()
                if args.action == 'recover' or t['phase'] != 'queued':
                    host.recover()
                else:
                    try:
                        release(c, t['image'], t['revision'], t['bundle'], t['run_id'], t['run_attempt'])
                        host.deploy()
                    except Exception as error:
                        host.t['error'] = str(error) if isinstance(error, Blocked) else type(error).__name__
                        host.recover()
            except Exception:
                host.incident()
            return 0 if t['phase'] == 'succeeded' else 1
    # Start outside the flock: the independent worker acquires the same lock.
    run(['systemctl', 'reset-failed', UNIT])
    run(['systemctl', 'start', '--no-block', UNIT])
    print('Deployment submitted; host-owned service and recovery own the transaction.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        # Do not echo arbitrary exceptions, subprocess output, config or secrets.
        print('AWS deployment blocked or failed; inspect the private host transaction.', file=sys.stderr)
        sys.exit(1)

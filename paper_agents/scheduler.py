"""Opt-in fixed Mini schedule. No cron parser, catch-up, provider calls or initialization."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

from . import import_state, runtime_config
from .migration_lifecycle import lease, signal_group

ZONE = ZoneInfo('America/Los_Angeles')
ANCHOR = '2026-09-07'


@dataclass(frozen=True)
class Job:
    name: str
    hour: int
    script: str
    weekday: int | None = None


JOBS = (Job('backup',1,'backup_db.sh',6),
        Job('profile',2,'biweekly_profile_rebuild_compare.sh',0),
        Job('openalex',4,'openalex_pipeline.sh'),
        Job('arxiv',5,'nightly_pipeline.sh'),
        Job('semantic',6,'semantic_scholar_pipeline.sh'))


def due(now):
    if now.tzinfo is None:
        raise ValueError('Scheduler clock must be timezone-aware')
    local = now.astimezone(ZONE)
    return [job for job in JOBS if local.minute == 0 and local.hour == job.hour
            and (job.weekday is None or local.weekday() == job.weekday)]


def key(job, now):
    return f'{now.astimezone(ZONE).date().isoformat()}:{job.hour:02d}00:{job.name}'


def command(job, root, backup):
    args = ['bash',str(Path(root)/'scripts'/job.script)]
    if job.name == 'backup':
        args += [str(Path(root)/'data/paper_agent.db'),str(backup)]
    return args


def child_environment(root, locks, parent=None):
    env = dict(os.environ if parent is None else parent)
    env.update(PAPER_AGENT_REPO=str(root),PAPER_AGENT_LOCK_DIR=str(locks),
               PAPER_AGENT_TOPIC_SLOT='0',PAPER_AGENT_PROFILE_REBUILD_ANCHOR=ANCHOR,
               TZ='America/Los_Angeles',PAPER_AGENT_SELF_UPDATE='0',PAPER_AGENT_BUSY_EXIT_CODE='75')
    # A testing clock must never leak into the existing biweekly wrapper.
    env.pop('PAPER_AGENT_TODAY',None)
    return env


JOURNAL_SCHEMA = 'CREATE TABLE jobs (key TEXT PRIMARY KEY, job TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, exit_code INTEGER)'


def journal_schema(connection):
    """Exact v1 contract, including primary-key index and absence of extra objects."""
    indexes=connection.execute('PRAGMA index_list(jobs)').fetchall()
    return (
        connection.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall(),
        connection.execute('PRAGMA table_xinfo(jobs)').fetchall(),
        indexes,
        [(item[1], connection.execute('PRAGMA index_xinfo("'+item[1].replace('"','""')+'")').fetchall()) for item in indexes],
    )


class Journal:
    def __init__(self, folder):
        folder=Path(folder)
        if not folder.is_dir() or folder.is_symlink():
            raise RuntimeError('Persistent scheduler directory unavailable')
        path=folder/'scheduler.sqlite'
        existed=path.exists()
        if os.path.lexists(path):
            info=path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError('Invalid scheduler journal file')
        else:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
        self.connection=sqlite3.connect(path)
        try:
            if existed:
                expected=sqlite3.connect(':memory:')
                try:
                    expected.execute(JOURNAL_SCHEMA)
                    if journal_schema(self.connection) != journal_schema(expected):
                        raise RuntimeError('Unexpected scheduler journal schema')
                finally:
                    expected.close()
            else:
                self.connection.execute(JOURNAL_SCHEMA)
                self.connection.commit()
                import_state.sync_directory(folder)
            self.connection.execute('PRAGMA synchronous=FULL')
        except Exception:
            self.connection.close()
            raise
        # Claims are never replayed, even if the child was never launched.
        with self.connection:
            self.connection.execute("UPDATE jobs SET status='interrupted',reason='scheduler_restarted' WHERE status IN ('claimed','running')")

    def claim(self, identity, job):
        with self.connection:
            cursor=self.connection.execute("INSERT OR IGNORE INTO jobs(key,job,status) VALUES(?,?,'claimed')",(identity,job))
            return cursor.rowcount == 1

    def finish(self, identity, status, reason, code=None):
        with self.connection:
            self.connection.execute('UPDATE jobs SET status=?,reason=?,exit_code=? WHERE key=?',(status,reason,code,identity))

    def close(self):
        self.connection.close()


def app_ready(url):
    from .prepare_model import wall_clock_deadline
    try:
        with wall_clock_deadline(time.monotonic()+2):
            with urllib.request.urlopen(url,timeout=2) as response:
                body=response.read(8193)
                return len(body) <= 8192 and json.loads(body).get('ready') is True
    except Exception:
        return False


def accepted(root, manifest_path):
    approved=import_state.load_manifest(manifest_path)
    receipt=Path(root)/import_state.RECEIPT
    if receipt.is_symlink() or not receipt.is_file():
        return False
    return json.loads(receipt.read_text()) == {'format':1,'manifest_sha256':import_state.digest(approved)}


def manual_busy(root):
    # Preflight only; the pipeline takes its own authoritative lock after launch.
    from .manual_scout import acquire, ScoutBusy
    try:
        handle=acquire(Path(root)/'data/paper_agent.db')
    except ScoutBusy:
        return True
    handle.close()
    return False


class Scheduler:
    def __init__(self, root, locks, logs, backup, manifest, *, ready,
                 budget=1800, grace=8, clock=time.monotonic, launcher=subprocess.Popen, singleton_descriptor=None):
        self.root,self.locks,self.backup,self.manifest=map(Path,(root,locks,backup,manifest))
        self.ready,self.budget,self.grace,self.clock,self.launcher=ready,budget,grace,clock,launcher
        if not 0 < budget <= 7200 or not 6 <= grace <= 30:
            raise ValueError('Invalid scheduler job budget/grace')
        self.journal=Journal(logs)
        self.active=None
        self.singleton_descriptor=singleton_descriptor

    def tick(self, now):
        tick_start=self.clock()
        self.poll()
        for job in due(now):
            identity=key(job,now)
            if not self.journal.claim(identity,job.name):continue
            if self.active:
                self.journal.finish(identity,'skipped','scheduler_busy');continue
            held=lease(self.locks)
            try:
                descriptor=held.__enter__()
            except RuntimeError:
                self.journal.finish(identity,'skipped','initialization_busy');continue
            try:
                if not accepted(self.root,self.manifest) or not self.ready():
                    self.journal.finish(identity,'skipped','app_not_ready');continue
                if job.name in {'arxiv','openalex','semantic'} and manual_busy(self.root):
                    self.journal.finish(identity,'skipped','pipeline_busy');continue
                if job.name == 'profile':
                    days=(now.astimezone(ZONE).date()-datetime.fromisoformat(ANCHOR).date()).days
                    if days < 0 or days % 14:
                        self.journal.finish(identity,'skipped','outside_biweekly_cadence');continue
                elapsed=max(0,self.clock()-tick_start)
                if key(job,now+timedelta(seconds=elapsed)) != identity or (now+timedelta(seconds=elapsed)).astimezone(ZONE).minute != 0:
                    self.journal.finish(identity,'skipped','minute_expired');continue
                env=child_environment(self.root,self.locks)
                # Inherit the shared lease: a scheduler crash cannot unlock a still-running job.
                process=self.launcher([sys.executable,"-m","paper_agents.scheduler_child",str(descriptor),str(self.singleton_descriptor if self.singleton_descriptor is not None else -1),str(self.locks),str(self.budget),*command(job,self.root,self.backup)],cwd=self.root,env=env,
                    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                    start_new_session=True,pass_fds=(descriptor,) + ((self.singleton_descriptor,) if self.singleton_descriptor is not None else ()))
                self.active=(identity,process,held,self.clock())
                held=None
                self.journal.finish(identity,'running','launched')
            except Exception:
                self.journal.finish(identity,'failed','launch_or_readiness_failed')
            finally:
                if held is not None:held.__exit__(None,None,None)

    def stop_group(self, process):
        for sig in (signal.SIGTERM,signal.SIGKILL):
            signal_group(process,sig)
            if sig == signal.SIGTERM:
                time.sleep(self.grace)
        process.wait(timeout=2)

    def poll(self):
        if self.active is None:return
        identity,process,held,start=self.active
        code=process.poll()
        timed_out=self.clock()-start >= self.budget
        if code is None and not timed_out:return
        try:
            # Clean descendants even if the group leader already exited.
            self.stop_group(process)
            self.journal.finish(identity,'skipped' if code == 75 else ('failed' if timed_out or code else 'completed'),
                                'pipeline_busy' if code == 75 else ('wall_timeout' if timed_out or code == 124 else 'child_exit'),process.returncode)
        finally:
            held.__exit__(None,None,None);self.active=None

    def close(self):
        if self.active:
            identity,process,held,_=self.active
            try:
                self.stop_group(process)
                self.journal.finish(identity,'interrupted','scheduler_stopped',process.returncode)
            finally:
                held.__exit__(None,None,None);self.active=None
        self.journal.close()


def serve(root, locks, logs, backup, manifest, *, ready, budget=1800, grace=8,
          now=lambda:datetime.now(ZONE), stop=None, pause=time.sleep):
    # Singleton is separate from runtime lease; idle scheduler never blocks initialization.
    with lease(locks,exclusive=True,name='.scheduler.lock') as singleton_descriptor:
        scheduler=Scheduler(root,locks,logs,backup,manifest,ready=ready,budget=budget,grace=grace,singleton_descriptor=singleton_descriptor)
        previous={}
        def interrupted(signum,frame):raise KeyboardInterrupt
        for sig in (signal.SIGINT,signal.SIGTERM):previous[sig]=signal.signal(sig,interrupted)
        try:
            while stop is None or not stop():
                scheduler.tick(now())
                pause(.5)
        except KeyboardInterrupt:
            pass
        finally:
            for sig in previous:signal.signal(sig,signal.SIG_IGN)
            try:scheduler.close()
            finally:
                for sig,handler in previous.items():signal.signal(sig,handler)


def main():
    if os.environ.get('PAPER_AGENT_SCHEDULER_ENABLED','0') == '0':
        print('scheduler disabled',flush=True);return 0
    if os.environ.get('PAPER_AGENT_SCHEDULER_ENABLED') != '1':
        raise ValueError('Scheduler enable must be 0 or 1')
    from .migration_lifecycle import validate_container_contract
    validate_container_contract()
    config=runtime_config.migration_settings()
    if config is None or not config['PAPER_AGENT_GEMINI_ENABLED']:
        raise RuntimeError('Complete scheduler requires Gemini parity; Monday job cannot be silently disabled')
    # Refuse incomplete profile parity rather than silently dropping Monday work.
    qualify_integrations()
    with lease(Path(os.environ['PAPER_AGENT_LIFECYCLE_DIR'])):
        if not accepted(Path('/app'),Path(os.environ['PAPER_AGENT_IMPORT_MANIFEST'])) or not app_ready('http://app:8000/ready'):
            raise RuntimeError('Scheduler requires initialized state and a ready application')
    serve(Path('/app'),Path(os.environ['PAPER_AGENT_LIFECYCLE_DIR']),Path('/app/logs'),Path('/backups'),
          Path(os.environ['PAPER_AGENT_IMPORT_MANIFEST']),ready=lambda:app_ready('http://app:8000/ready'),
          budget=int(os.environ.get('PAPER_AGENT_JOB_TIMEOUT','1800')),
          grace=int(os.environ.get('PAPER_AGENT_JOB_STOP_GRACE','8')))


def qualify_integrations():
    from .gemini_runtime import check_ready
    check_ready()


if __name__ == '__main__':
    raise SystemExit(main())

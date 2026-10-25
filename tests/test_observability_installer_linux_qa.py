"""Root installer probes: ONLY run inside an explicitly disposable QA container.

Never run these against host /etc or /var. The guard intentionally requires both
an explicit opt-in and the read-only QA source mount in a Docker container.
"""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ISOLATED = (sys.platform == 'linux' and os.environ.get('PAPER_OBSERVABILITY_QA_ISOLATED') == '1'
            and Path('/.dockerenv').exists() and Path('/qa-source/deploy/observability').is_dir())
CONFIG = Path('/etc/project-paper-observability/mini-production/mini-production.env')
STATE = Path('/var/lib/project-paper-observability/mini-production')
CONTROL = Path('/var/lib/project-paper-observability/mini-production-control')
EXPORT = Path('/var/lib/project-paper-observability/mini-production-export')
WRAPPER = Path('/usr/local/sbin/project-paper-observability-container')
CRON = Path('/etc/cron.d/project-paper-observability-mini-production')
LOCK = Path('/run/lock/project-paper-observability.lock')
IMAGE = 'registry.invalid/qa@sha256:' + 'a' * 64
TARGETS = (CONFIG, WRAPPER, CRON, EXPORT / 'INSTALL-RECEIPT')


@unittest.skipUnless(ISOLATED, 'Requires explicit disposable Docker QA container; never run on a host')
class ObservabilityInstallerLinuxQA(unittest.TestCase):
    def setUp(self):
        for path in (CONFIG.parent, STATE, EXPORT, CONTROL):
            if path.exists(): shutil.rmtree(path)
        for path in (WRAPPER, CRON): path.unlink(missing_ok=True)
        Path('/etc/cron.d').mkdir(exist_ok=True); LOCK.parent.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.package = self.root / 'package'
        shutil.copytree('/qa-source/deploy/observability', self.package / 'deploy/observability')
        (self.package / 'scripts').mkdir()
        shutil.copy2('/qa-source/scripts/host_resource_collector.py', self.package / 'scripts/host_resource_collector.py')
        self.bin = self.root / 'bin'; self.bin.mkdir()
        self.capture = self.root / 'docker.jsonl'
        fake = self.bin / 'docker'
        fake.write_text('#!' + sys.executable + '\n' + '''import json,os,sys,time
with open(os.environ['QA_DOCKER_CAPTURE'],'a') as stream:stream.write(json.dumps(sys.argv[1:])+'\\n')
if os.environ.get('QA_DOCKER_DELAY'):time.sleep(float(os.environ['QA_DOCKER_DELAY']))
print('QA Docker boundary')
'''); fake.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'],
            PROJECT_PAPER_OBSERVABILITY_IMAGE=IMAGE, QA_DOCKER_CAPTURE=str(self.capture))

    def install(self, **extra):
        return subprocess.run(['bash', str(self.package / 'deploy/observability/install_container_cron.sh'),
            '--apply', '--target=mini-production'], env=dict(self.env, **extra),
            capture_output=True, text=True, timeout=10)

    def remove(self):
        return subprocess.run(['bash', str(self.package / 'deploy/observability/remove_container_cron.sh')],
                              env=self.env, capture_output=True, text=True, timeout=5)

    def installed(self):
        result = self.install(); self.assertEqual(result.returncode, 0, result.stderr)

    def snapshot(self):
        return {str(path): path.read_bytes() if path.exists() else None for path in TARGETS}

    def cron_command(self):
        return CRON.read_text().strip().split(None, 6)[6]

    def test_fresh_install_and_unrelated_files_survive_removal_twice(self):
        unrelated = Path('/etc/project-paper-observability/qa-unrelated')
        unrelated.parent.mkdir(parents=True, exist_ok=True); unrelated.write_text('preserve')
        self.installed()
        self.assertEqual(CONFIG.stat().st_mode & 0o777, 0o640)
        self.assertEqual(CRON.stat().st_mode & 0o777, 0o644)
        for _ in range(2): self.assertEqual(self.remove().returncode, 0)
        self.assertEqual(unrelated.read_text(), 'preserve')
        self.assertFalse(CRON.exists()); self.assertFalse(WRAPPER.exists())

    def test_installed_private_state_is_writable_by_runtime_uid(self):
        self.installed()
        def drop(): os.setgid(10001); os.setuid(10001)
        probe = subprocess.run([sys.executable, '-c',
            'from pathlib import Path; p=Path("/var/lib/project-paper-observability/mini-production"); (p/"qa-write").write_text("ok")'],
            preexec_fn=drop, capture_output=True, text=True, timeout=5)
        self.assertEqual(probe.returncode, 0, probe.stderr)

    def test_installed_export_is_writable_by_runtime_uid(self):
        self.installed()
        def drop(): os.setgid(10001); os.setuid(10001)
        probe = subprocess.run([sys.executable, '-c',
            'from pathlib import Path; (Path("/var/lib/project-paper-observability/mini-production-export")/"qa-write").write_text("ok")'],
            preexec_fn=drop, capture_output=True, text=True, timeout=5)
        self.assertEqual(probe.returncode, 0, probe.stderr)

    def test_direct_installed_launcher_loads_exact_config_without_inherited_variables(self):
        self.installed()
        env = {k:v for k,v in self.env.items() if not k.startswith('PROJECT_PAPER_')}
        run = subprocess.run([str(WRAPPER)], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(self.capture.exists())

    def test_installer_refuses_invalid_digest_without_installing_files(self):
        for image in ('repo:latest', 'repo@sha256:x', 'repo@sha256:' + 'z' * 64,
                      'repo@sha256:' + 'a' * 64 + ';echo QA_INJECTION'):
            with self.subTest(image_kind=image[:20]):
                result = self.install(PROJECT_PAPER_OBSERVABILITY_IMAGE=image)
                self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
                self.assertFalse(CRON.exists())

    def test_cron_serializes_two_simultaneous_collections(self):
        self.installed()
        env = dict(self.env, QA_DOCKER_DELAY='0.5')
        one = subprocess.Popen(['sh', '-c', self.cron_command()], env=env)
        two = subprocess.Popen(['sh', '-c', self.cron_command()], env=env)
        self.addCleanup(lambda: one.poll() is None and one.kill())
        self.addCleanup(lambda: two.poll() is None and two.kill())
        codes = [one.wait(timeout=5), two.wait(timeout=5)]
        self.assertIn(0, codes)
        self.assertEqual(len(self.capture.read_text().splitlines()), 1)

    def test_installer_refuses_overlap_when_collector_lock_is_held(self):
        LOCK.touch(exist_ok=True)
        with LOCK.open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.install()
        self.assertEqual(result.returncode, 75)
        self.assertFalse(CRON.exists())

    def test_new_cron_path_rotates_existing_large_collector_log(self):
        self.installed()
        log = STATE / 'collector.log'; log.write_bytes(b'x' * (2 * 1024 * 1024))
        run = subprocess.run(['sh', '-c', self.cron_command()], env=self.env,
                             capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue((STATE / 'collector.log.1').exists(), 'Container cron bypasses old log rotation')
        self.assertLessEqual(log.stat().st_size, 1024 * 1024)
        self.assertIn('QA Docker boundary', log.read_text(), 'Current run output belongs in the active log')
        self.assertLessEqual((STATE / 'collector.log.1').stat().st_size, 524288)

    def test_failure_after_first_replace_restores_previous_installation(self):
        self.installed(); before = self.snapshot()
        launcher = self.package / 'deploy/observability/run-container.sh'
        launcher.write_text(launcher.read_text() + '\n# QA new release\n')
        fake = self.bin / 'mv'
        fake.write_text('#!/bin/sh\nfor arg do last="$arg"; done\n'
            'if [ "$1" = "-f" ] && [ "$2" = "/etc/project-paper-observability/mini-production/mini-production.env.new" ]; then exit 41; fi\n'
            'exec /bin/mv "$@"\n'); fake.chmod(0o755)
        failed = self.install(PROJECT_PAPER_OBSERVABILITY_IMAGE='registry.invalid/qa@sha256:' + 'b' * 64)
        self.assertEqual(failed.returncode, 41)
        self.assertEqual(self.snapshot(), before, 'Injected env replace failure left new launcher installed')

    def test_upgrade_then_rollback_twice_restores_previous_managed_files(self):
        self.installed(); before = self.snapshot()
        metadata = {str(path): (path.stat().st_uid, path.stat().st_gid, path.stat().st_mode & 0o777)
                    for path in TARGETS}
        upgraded = self.install(PROJECT_PAPER_OBSERVABILITY_IMAGE='registry.invalid/qa@sha256:' + 'b' * 64)
        self.assertEqual(upgraded.returncode, 0)
        for attempt in range(1, 3):
            with self.subTest(removal=attempt):
                self.assertEqual(self.remove().returncode, 0)
                self.assertEqual(self.snapshot(), before,
                                 'Repeated rollback must preserve the restored installation')
                self.assertEqual({str(path): (path.stat().st_uid, path.stat().st_gid,
                                             path.stat().st_mode & 0o777) for path in TARGETS}, metadata)

    def test_failed_backup_pointer_publish_preserves_previous_transaction(self):
        self.installed(); before = self.snapshot()
        pointer = CONTROL / 'CURRENT-BACKUP'
        previous_pointer = pointer.read_bytes()
        fake = self.bin / 'chmod'
        marker = self.root / 'pointer-publish-fault-used'
        fake.write_text('#!/bin/sh\nfor arg do last="$arg"; done\n'
            'case "$last" in /var/lib/project-paper-observability/mini-production-control/CURRENT-BACKUP|/var/lib/project-paper-observability/mini-production-control/CURRENT-BACKUP.new) '
            'touch "' + str(marker) + '"; exit 43 ;; esac\n'
            'exec /bin/chmod "$@"\n'); fake.chmod(0o755)
        failed = self.install(PROJECT_PAPER_OBSERVABILITY_IMAGE='registry.invalid/qa@sha256:' + 'b' * 64)
        self.assertTrue(marker.exists(), 'Fault must reach the actual pointer metadata operation')
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(pointer.read_bytes(), previous_pointer,
                         'A failed upgrade must retain the previous durable rollback pointer')

    def test_collector_writable_manifest_cannot_delete_unrelated_root_file(self):
        self.installed()
        victim = Path('/etc/project-paper-observability/qa-unrelated-root-file')
        victim.write_text('preserve root-owned unrelated data')
        code = '''from pathlib import Path
state=Path('/var/lib/project-paper-observability/mini-production')
backup=state/'qa-untrusted-backup'; backup.mkdir()
(backup/'manifest').write_text('/etc/project-paper-observability/qa-unrelated-root-file|'+str(backup/'missing')+'\\n')
(state/'CURRENT-BACKUP').write_text(str(backup)+'\\n')
'''
        def drop(): os.setgid(10001); os.setuid(10001)
        probe = subprocess.run([sys.executable, '-c', code], preexec_fn=drop,
                               capture_output=True, text=True, timeout=5)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.remove()
        self.assertTrue(victim.exists(),
                        'Root rollback must reject collector-controlled paths outside its managed allowlist')

    def test_rollback_control_metadata_is_root_owned_and_collector_cannot_modify_it(self):
        self.installed()
        pointer = CONTROL / 'CURRENT-BACKUP'
        previous = pointer.read_bytes()
        self.assertEqual((CONTROL.stat().st_uid, CONTROL.stat().st_gid,
                          CONTROL.stat().st_mode & 0o777), (0, 0, 0o700))
        self.assertEqual((pointer.stat().st_uid, pointer.stat().st_gid,
                          pointer.stat().st_mode & 0o777), (0, 0, 0o600))
        code = '''from pathlib import Path
pointer=Path('/var/lib/project-paper-observability/mini-production-control/CURRENT-BACKUP')
try: pointer.write_text('QA_TAMPER')
except PermissionError: pass
else: raise AssertionError('Collector could modify root rollback pointer')
'''
        def drop(): os.setgid(10001); os.setuid(10001)
        probe = subprocess.run([sys.executable, '-c', code], preexec_fn=drop,
                               capture_output=True, text=True, timeout=5)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(pointer.read_bytes(), previous)

    def test_missing_active_backup_manifest_fails_without_claiming_success(self):
        self.installed(); before = self.snapshot()
        backup = Path((CONTROL / 'CURRENT-BACKUP').read_text().strip())
        (backup / 'manifest').unlink()
        result = self.remove()
        self.assertNotEqual(result.returncode, 0,
                            'An active pointer with missing recovery evidence is not a successful no-op')
        self.assertEqual(self.snapshot(), before)

    def test_retry_after_interrupted_restore_keeps_previous_installation(self):
        self.installed(); before = self.snapshot()
        self.assertEqual(self.install(PROJECT_PAPER_OBSERVABILITY_IMAGE=
                                     'registry.invalid/qa@sha256:' + 'b' * 64).returncode, 0)
        marker = self.root / 'restore-fault-used'
        for command in ('cp', 'mv'):
            fake = self.bin / command
            fake.write_text('#!/bin/sh\nfor arg do last="$arg"; done\n'
                'if [ "$last" = "/etc/cron.d/project-paper-observability-mini-production" ] '
                '&& [ ! -e "' + str(marker) + '" ]; then touch "' + str(marker) + '"; exit 44; fi\n'
                'exec /bin/' + command + ' "$@"\n'); fake.chmod(0o755)
        first = self.remove()
        self.assertTrue(marker.exists(), 'Fault injection must reach the actual restore operation')
        self.assertNotEqual(first.returncode, 0)
        second = self.remove()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.snapshot(), before,
                         'Retry must not delete files whose backups were consumed by the first attempt')

    def test_retry_after_restore_pointer_cleanup_failure_is_idempotent(self):
        self.installed(); before = self.snapshot()
        self.assertEqual(self.install(PROJECT_PAPER_OBSERVABILITY_IMAGE=
                                     'registry.invalid/qa@sha256:' + 'b' * 64).returncode, 0)
        marker = self.root / 'pointer-cleanup-fault-used'
        fake = self.bin / 'rm'
        fake.write_text('#!/bin/sh\nfor arg do last="$arg"; done\n'
            'if [ "$last" = "/var/lib/project-paper-observability/mini-production-control/CURRENT-BACKUP" ] '
            '&& [ ! -e "' + str(marker) + '" ]; then touch "' + str(marker) + '"; exit 45; fi\n'
            'exec /bin/rm "$@"\n'); fake.chmod(0o755)
        self.assertNotEqual(self.remove().returncode, 0)
        self.assertTrue(marker.exists())
        self.assertEqual(self.snapshot(), before)
        retry = self.remove()
        self.assertEqual(retry.returncode, 0, 'A completed restore must be able to finish pointer cleanup')
        self.assertEqual(self.snapshot(), before)

    def test_missing_required_backup_fails_before_removing_active_files(self):
        self.installed()
        self.assertEqual(self.install(PROJECT_PAPER_OBSERVABILITY_IMAGE=
                                     'registry.invalid/qa@sha256:' + 'b' * 64).returncode, 0)
        before = self.snapshot()
        backup = Path((CONTROL / 'CURRENT-BACKUP').read_text().strip())
        (backup / (WRAPPER.name + '.previous')).unlink()
        result = self.remove()
        self.assertNotEqual(result.returncode, 0,
                            'A missing backup from an upgrade is not proof of original absence')
        self.assertEqual(self.snapshot(), before, 'Validate all recovery inputs before deleting active files')

    def test_fresh_install_failure_after_cron_publish_removes_all_active_files(self):
        before = self.snapshot()
        fake = self.bin / 'chown'
        fake.write_text('#!/bin/sh\nfor arg do last="$arg"; done\n'
            'if [ "$last" = "/var/lib/project-paper-observability/mini-production-export/INSTALL-RECEIPT" ]; then exit 42; fi\n'
            'exec /bin/chown "$@"\n'); fake.chmod(0o755)
        failed = self.install()
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.snapshot(), before,
                         'Failed fresh installation left active config/cron/receipt behind')

    def test_rejected_overlapping_install_preserves_existing_directory_metadata(self):
        STATE.mkdir(parents=True); STATE.chmod(0o750)
        before = (STATE.stat().st_uid, STATE.stat().st_gid, STATE.stat().st_mode & 0o777)
        LOCK.touch(exist_ok=True)
        with LOCK.open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.install()
        self.assertEqual(result.returncode, 75)
        after = (STATE.stat().st_uid, STATE.stat().st_gid, STATE.stat().st_mode & 0o777)
        self.assertEqual(after, before, 'Lock rejection must occur before directory mutations')

    def test_digest_validation_rejects_invalid_repository_and_multiline_input(self):
        for prefix in ('', 'repo name', 'repo;echo QA_PREFIX #'):
            with self.subTest(prefix=prefix):
                result = self.install(PROJECT_PAPER_OBSERVABILITY_IMAGE=prefix + '@sha256:' + 'a' * 64)
                self.assertEqual(result.returncode, 64)
        with self.subTest(input='multiline'):
            result = self.install(PROJECT_PAPER_OBSERVABILITY_IMAGE='repo@sha256:' + 'a' * 64 + '\nQA_EXTRA=1')
            self.assertEqual(result.returncode, 64)

    def test_launcher_rejects_unsafe_image_input_before_invoking_docker(self):
        invalid = ('@sha256:' + 'a' * 64,
                   'repo name@sha256:' + 'a' * 64,
                   'repo;echo QA_PREFIX #@sha256:' + 'a' * 64,
                   'repo@sha256:' + 'a' * 64 + '\nQA_EXTRA=1',
                   'repo:latest', 'repo@sha256:x')
        for image in invalid:
            with self.subTest(image=image[:24]):
                env = dict(self.env, PROJECT_PAPER_RESOURCE_ENV_FILE=str(self.root / 'missing.env'),
                           PROJECT_PAPER_OBSERVABILITY_IMAGE=image,
                           PROJECT_PAPER_RESOURCE_ROOT=str(self.root / 'state'),
                           PROJECT_PAPER_RESOURCE_EXPORT_DIR=str(self.root / 'export'))
                result = subprocess.run(['sh', str(self.package / 'deploy/observability/run-container.sh')],
                    env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 64)
                self.assertFalse(self.capture.exists(), 'Rejected input must not reach Docker')
                self.assertFalse((self.root / 'state').exists())

    def test_valid_digest_references_preserve_registry_port_and_namespace(self):
        for repo in ('ghcr.io/example/paper-agent/observability', 'registry.example:5000/team/collector'):
            with self.subTest(repository=repo):
                image = repo + '@sha256:' + 'a' * 64
                result = self.install(PROJECT_PAPER_OBSERVABILITY_IMAGE=image)
                self.assertEqual(result.returncode, 0, result.stderr)
                run = subprocess.run([str(WRAPPER)], env=self.env,
                                     capture_output=True, text=True, timeout=5)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(json.loads(self.capture.read_text().splitlines()[-1])[-1], image)

    def test_launcher_exact_allowlisted_mounts_no_socket_secrets_db_or_pipeline(self):
        self.installed()
        run = subprocess.run(['sh', '-c', self.cron_command()], env=self.env,
                             capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        args = json.loads(self.capture.read_text().splitlines()[0])
        mounts = [args[i+1] for i,v in enumerate(args) if v == '-v']
        ro = [m for m in mounts if m.endswith(':ro')]
        allowed_ro = {'/proc/stat:/host/proc/stat:ro',
            '/proc/meminfo:/host/proc/meminfo:ro', '/proc/vmstat:/host/proc/vmstat:ro',
            '/sys/class/thermal:/host/sys/class/thermal:ro', '/sys/fs/cgroup:/sys/fs/cgroup:ro',
            '/proc/sys/kernel/random/boot_id:/host/boot_id:ro'}
        unexpected = {mount for mount in ro if mount not in allowed_ro and not (
            mount.startswith('/var/lib/project-paper-observability/') and mount.endswith(':/host/disk:ro'))}
        self.assertFalse(unexpected, 'Do not expose broad host directories to measure filesystem capacity')
        self.assertEqual(len(mounts) - len(ro), 2)
        for forbidden in ('docker.sock', 'paper_agent.db', '/app/data', '/etc/project-paper/production', 'pipeline'):
            self.assertNotIn(forbidden, ' '.join(args))
        for required in ('--network', '--read-only', '--cap-drop', '--security-opt', '--memory', '--cpus', '--pids-limit'):
            self.assertIn(required, args)

    def test_launcher_has_host_boot_and_disk_inputs_for_host_scope(self):
        self.installed()
        subprocess.run(['sh', '-c', self.cron_command()], env=self.env, check=True, timeout=5)
        args = json.loads(self.capture.read_text().splitlines()[0])
        mounts = [args[i+1] for i,v in enumerate(args) if v == '-v']
        with self.subTest(input='host boot identity'):
            self.assertTrue(any('/proc/sys/kernel/random/boot_id' in mount for mount in mounts),
                            'Host boot identity is not available in the collector container')
        with self.subTest(input='host filesystem target'):
            self.assertTrue(any(arg.startswith('PROJECT_PAPER_RESOURCE_DISK_PATH=') for arg in args),
                            'No host filesystem target is configured; collector measures its own root filesystem')


    def test_cron_collection_is_bounded_by_legacy_45_second_budget_and_cleanup(self):
        self.installed()
        process = subprocess.Popen(['sh', '-c', self.cron_command()],
            env=dict(self.env, QA_DOCKER_DELAY='120'), start_new_session=True)
        bounded = False
        try:
            try:
                process.wait(timeout=52)
                bounded = process.returncode != 0
            except subprocess.TimeoutExpired:
                pass
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try: process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=2)
        self.assertTrue(bounded, 'Cron collector still running after prior 45s + 5s cleanup budget')


if __name__ == '__main__':
    unittest.main(verbosity=2)

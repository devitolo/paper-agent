"""Full transaction fault matrix; run only in the guarded disposable Linux fixture."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import test_observability_installer_linux_qa as linux


@unittest.skipUnless(linux.ISOLATED, 'Requires explicitly disposable Linux QA container')
class ObservabilityRecoveryMatrixQA(unittest.TestCase):
    def setUp(self):
        self.fixture = None
        self.reset()

    def reset(self):
        if self.fixture is not None:
            self.fixture.doCleanups()
        self.fixture = linux.ObservabilityInstallerLinuxQA()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def state(self):
        result = {}
        for path in linux.TARGETS:
            result[str(path)] = None if not path.exists() else (
                path.read_bytes(), path.stat().st_uid, path.stat().st_gid, path.stat().st_mode & 0o777)
        return result

    def pointer(self):
        path = linux.CONTROL / 'CURRENT-BACKUP'
        return path.read_bytes() if path.exists() else None

    def backup(self):
        return Path(self.pointer().decode().strip())

    def upgrade(self):
        result = self.fixture.install(PROJECT_PAPER_OBSERVABILITY_IMAGE=
                                     'registry.invalid/qa@sha256:' + 'b' * 64)
        self.assertEqual(result.returncode, 0, result.stderr)

    def fault(self, commands, destination):
        marker = self.fixture.root / 'matrix-fault-used'
        for command in commands:
            real = shutil.which(command)
            self.assertIsNotNone(real)
            script = self.fixture.bin / command
            script.write_text('#!' + sys.executable + '\n' +
                'import os,sys\nfrom pathlib import Path\n' +
                'marker=Path(' + repr(str(marker)) + ')\n' +
                'if sys.argv[1:] and sys.argv[-1]==' + repr(str(destination)) + ' and not marker.exists():\n' +
                ' marker.write_text(' + repr(command) + ');sys.exit(66)\n' +
                'os.execv(' + repr(real) + ',[' + repr(real) + ',*sys.argv[1:]])\n')
            script.chmod(0o755)
        return marker

    def test_failure_at_each_managed_publish_restores_fresh_or_upgrade_state(self):
        for scenario in ('fresh', 'upgrade'):
            for target in linux.TARGETS:
                with self.subTest(scenario=scenario, target=str(target)):
                    self.reset()
                    if scenario == 'upgrade': self.fixture.installed()
                    before = self.state(); pointer = self.pointer()
                    marker = self.fault(('cp', 'mv', 'install', 'chmod', 'chown'), target)
                    result = self.fixture.install(PROJECT_PAPER_OBSERVABILITY_IMAGE=
                                                  'registry.invalid/qa@sha256:' + 'b' * 64)
                    self.assertTrue(marker.exists(), 'Publish fault must actually fire')
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(self.state(), before)
                    self.assertEqual(self.pointer(), pointer)

    def test_failure_at_each_restore_can_retry_with_original_metadata(self):
        for target in linux.TARGETS:
            with self.subTest(target=str(target)):
                self.reset(); self.fixture.installed(); before = self.state()
                self.upgrade()
                marker = self.fault(('cp', 'mv', 'install'), target)
                self.assertNotEqual(self.fixture.remove().returncode, 0)
                self.assertTrue(marker.exists(), 'Restore fault must actually fire')
                for _ in range(2):
                    retry = self.fixture.remove()
                    self.assertEqual(retry.returncode, 0, retry.stderr)
                    self.assertEqual(self.state(), before)

    def test_atomic_pointer_replace_failure_keeps_old_pointer_and_installation(self):
        self.fixture.installed(); before = self.state(); pointer = self.pointer()
        marker = self.fault(('mv', 'install'), linux.CONTROL / 'CURRENT-BACKUP')
        failed = self.fixture.install(PROJECT_PAPER_OBSERVABILITY_IMAGE=
                                      'registry.invalid/qa@sha256:' + 'b' * 64)
        self.assertTrue(marker.exists(), 'Pointer replace fault must actually fire')
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.pointer(), pointer)

    def test_corrupt_backup_bytes_fail_before_any_active_file_changes(self):
        self.fixture.installed(); self.upgrade(); before = self.state()
        (self.backup() / (linux.WRAPPER.name + '.previous')).write_bytes(b'QA_CORRUPT_COPY')
        result = self.fixture.remove()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state(), before)

    def test_incomplete_or_duplicate_manifest_fails_before_any_active_file_changes(self):
        for damage in ('missing_row', 'duplicate_row', 'unmanaged_target'):
            with self.subTest(damage=damage):
                self.reset(); self.fixture.installed(); self.upgrade(); before = self.state()
                manifest = self.backup() / 'manifest'
                rows = manifest.read_text().splitlines()
                if damage == 'missing_row': rows.pop()
                elif damage == 'duplicate_row': rows[-1] = rows[0]
                else: rows[-1] = '/etc/qa-unmanaged|/tmp/qa-missing|0|-|-|-|-'
                manifest.write_text('\n'.join(rows) + '\n')
                result = self.fixture.remove()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.state(), before)

    def test_backup_symlink_is_not_trusted_as_an_immutable_recovery_copy(self):
        self.fixture.installed(); self.upgrade(); before = self.state()
        backup = self.backup() / (linux.WRAPPER.name + '.previous')
        outside = self.fixture.root / 'outside-backup'
        outside.write_bytes(backup.read_bytes())
        backup.unlink(); backup.symlink_to(outside)
        result = self.fixture.remove()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state(), before)

    def test_collector_export_symlink_cannot_redirect_root_receipt_restore(self):
        self.fixture.installed(); self.upgrade()
        victim = Path('/etc/project-paper-observability/qa-root-receipt-victim')
        original = b'QA_UNRELATED_ROOT_CONTENT'
        victim.write_bytes(original)
        code = """from pathlib import Path
p=Path('/var/lib/project-paper-observability/mini-production-export/INSTALL-RECEIPT')
p.unlink();p.symlink_to('/etc/project-paper-observability/qa-root-receipt-victim')
"""
        def drop(): os.setgid(10001); os.setuid(10001)
        result = subprocess.run([sys.executable, '-c', code], preexec_fn=drop,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.fixture.remove()
        self.assertEqual(victim.read_bytes(), original,
                         'A collector-owned export entry must not redirect root recovery writes')

    def test_samples_exports_and_unrelated_files_survive_upgrade_and_rollback(self):
        self.fixture.installed()
        preserved = {linux.STATE / 'samples.jsonl': b'{"qa":"sample"}\n',
                     linux.STATE / 'last-cpu.json': b'{"qa":"baseline"}\n',
                     linux.EXPORT / 'status.json': b'{"qa":"export"}\n'}
        for path, content in preserved.items(): path.write_bytes(content)
        self.upgrade()
        for _ in range(2): self.assertEqual(self.fixture.remove().returncode, 0)
        for path, content in preserved.items(): self.assertEqual(path.read_bytes(), content)


if __name__ == '__main__':
    unittest.main(verbosity=2)

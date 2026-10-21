from contextlib import closing, redirect_stdout
from datetime import datetime, timezone, timedelta
import gzip
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from paper_agents import full_snapshot as snap
from paper_agents import snapshot_store as store
from paper_agents.import_state import TRUSTED_SCHEMA


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source'
        (self.source / 'data/extractions').mkdir(parents=True)
        (self.source / 'config').mkdir()
        self.exports = self.root / 'exports'
        self.store = self.root / 'input-store'
        self.key = b'test-only-private-shared-key-32bytes!'
        self.stream = 'aws-production'
        self.db = self.source / snap.DATABASE
        with closing(sqlite3.connect(self.db)) as connection:
            connection.executescript(TRUSTED_SCHEMA.read_text())
            connection.execute("INSERT INTO papers(id,canonical_key,title) VALUES(1,'paper-1','Original title')")
            connection.execute("INSERT INTO artifacts(id,paper_id,artifact_type,path) VALUES(1,1,'triage_summary','data/extractions/one.json')")
            connection.execute("INSERT INTO artifacts(id,paper_id,artifact_type,path) VALUES(2,1,'pdf','data/papers/one.pdf')")
            connection.execute("INSERT INTO openalex_search_state VALUES('cursor','{\"cursor\":\"page-two\"}')")
            connection.commit()
        (self.source / 'data/extractions/one.json').write_text('{"summary":"business data"}')
        (self.source / 'data/profile.json').write_text(json.dumps({
            'interests': ['reliability'], 'positive_signals': [], 'negative_signals': [], 'feedback_history': [], 'notes': 'business notes'}))
        (self.source / 'config/topics.yaml').write_text('# ignored source comment\ntopics:\n')
        (self.source / 'config/industry.json').write_text(json.dumps({
            'enabled': True, 'include_signals': [], 'exclude_signals': [], 'max_items_per_run': 5, 'schedule': 'manual'}))

    def tearDown(self):
        self.temp.cleanup()

    def export(self, revision=1):
        return snap.export_snapshot(self.source, self.exports, stream=self.stream, revision=revision,
                                    source_revision='a' * 40, key=self.key, source_quiesced=True)

    def pull(self, package, **kwargs):
        return store.pull_snapshot(store.LocalPackage(package), self.store,
                                   stream=kwargs.pop('stream', self.stream), key=kwargs.pop('key', self.key), **kwargs)

    def resign(self, package, manifest=None):
        if manifest is not None:
            (package / 'manifest.json').write_bytes(snap.canonical(manifest))
        auth = snap.envelope((package / 'manifest.json').read_bytes(), package / 'snapshot.tar.gz', self.key)
        (package / 'auth.json').write_bytes(snap.canonical(auth))

    def manifest(self, package):
        return json.loads((package / 'manifest.json').read_bytes())

    def assert_no_selection(self):
        self.assertFalse((self.store / 'state.json').exists())
        self.assertFalse(list(self.store.glob('.incoming-*')))

    def test_roundtrip_manifest_hash_schema_gzip_and_source_unchanged(self):
        original = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        package = self.export()
        manifest = self.manifest(package)
        self.assertEqual(manifest['revision'], 1)
        self.assertEqual(set(manifest['files']), snap.REQUIRED | {'data/extractions/one.json'})
        self.assertEqual(manifest['database']['counts']['openalex_search_state'], 1)
        self.assertEqual(manifest['excluded_pdf_references'], [{'id': 2, 'path': 'data/papers/one.pdf'}])
        self.assertEqual((package / 'snapshot.tar.gz').read_bytes()[:3], b'\x1f\x8b\x08')
        self.assertEqual((package / 'snapshot.tar.gz').read_bytes()[8], 2)  # gzip maximum compression XFL
        self.assertEqual(self.pull(package), 1)
        with store.selected_input(self.store, stream=self.stream, key=self.key) as selected:
            self.assertEqual((selected / 'data/extractions/one.json').read_text(), '{"summary":"business data"}')
            with closing(sqlite3.connect(selected / snap.DATABASE)) as db:
                self.assertEqual(db.execute('SELECT title FROM papers').fetchone(), ('Original title',))
                self.assertEqual(db.execute('SELECT value_json FROM openalex_search_state').fetchone(), ('{"cursor":"page-two"}',))
            for name, entry in manifest['files'].items():
                self.assertEqual(snap.file_hash(selected / name), entry['sha256'])
        self.assertEqual(original, {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()})

    def test_explicit_exclusions_and_ignored_topic_preamble(self):
        secret = b'NOT-FOR-EXPORT-secret-value'
        names = ['.env', 'data/test.db', 'data/extra.sqlite', 'data/logs/run.log', 'data/.scout.lock',
                 'data/models/minilm.bin', 'data/papers/one.pdf', 'config/credentials.json',
                 'data/extractions/unreferenced.json', 'data/paper_agent.db-journal']
        for name in names:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(secret)
        # A real SQLite journal isn't invented in fixtures: an invalid sidecar may block SQLite itself.
        (self.source / 'data/paper_agent.db-journal').unlink()
        (self.source / 'config/topics.yaml').write_text('api_key: NOT-FOR-EXPORT-secret-value\ntopics:\n')
        package = self.export()
        with tarfile.open(package / 'snapshot.tar.gz') as tar:
            self.assertEqual(set(tar.getnames()), snap.REQUIRED | {'data/extractions/one.json'})
            for member in tar:
                self.assertNotIn(secret, tar.extractfile(member).read())

    def test_committed_wal_is_backed_up_uncommitted_changes_are_not(self):
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute("UPDATE papers SET title='Committed WAL'")
            writer.commit()
            writer.execute("UPDATE papers SET title='Uncommitted'")
            package = self.export()
            with closing(sqlite3.connect(package / 'payload' / snap.DATABASE)) as captured:
                self.assertEqual(captured.execute('SELECT title FROM papers').fetchone(), ('Committed WAL',))
                self.assertEqual(captured.execute('PRAGMA integrity_check').fetchone(), ('ok',))
            writer.rollback()
        self.assertFalse(any(name.endswith(('-wal', '-shm', '-journal')) for name in self.manifest(package)['files']))

    def test_deleted_db_pages_not_exported(self):
        marker = 'DELETED_PRIVATE_MARKER_' * 100
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('PRAGMA secure_delete=OFF')
            db.execute('INSERT INTO papers(id,canonical_key,title) VALUES(2,?,?)', ('deleted', marker))
            db.commit()
            db.execute('DELETE FROM papers WHERE id=2')
            db.commit()
        self.assertNotIn(marker.encode(), (self.export() / 'payload' / snap.DATABASE).read_bytes())

    def test_schema_extra_table_and_credential_column_refused(self):
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('ALTER TABLE papers ADD COLUMN api_key TEXT')
        with self.assertRaisesRegex(snap.SnapshotError, 'schema'):
            self.export()
        self.assertFalse(list(self.exports.glob('snapshot-*')))

    def test_credential_fields_in_database_json_refused_without_echo(self):
        secret = 'do-not-echo-me'
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('UPDATE paper_sources SET metadata_json=?', (json.dumps({'api_key': secret}),))
            db.execute('INSERT INTO paper_sources(paper_id,source,source_id,metadata_json) VALUES(1,?,?,?)',
                       ('arxiv', '1', json.dumps({'credentials': secret})))
            db.commit()
        with self.assertRaises(snap.SnapshotError) as caught:
            self.export()
        self.assertNotIn(secret, str(caught.exception))
        self.assertFalse(list(self.exports.glob('snapshot-*')))

    def test_credential_config_and_artifacts_refused(self):
        for name in ('data/profile.json', 'config/industry.json', 'data/extractions/one.json'):
            with self.subTest(name=name):
                path = self.source / name
                before = path.read_bytes()
                data = json.loads(before)
                data['api_key'] = 'do-not-echo'
                path.write_text(json.dumps(data))
                with self.assertRaises(snap.SnapshotError) as caught:
                    self.export()
                self.assertNotIn('do-not-echo', str(caught.exception))
                path.write_bytes(before)

    def test_missing_artifact_integrity_and_foreign_keys_refused(self):
        artifact = self.source / 'data/extractions/one.json'
        artifact.unlink()
        with self.assertRaises(snap.SnapshotError):
            self.export()
        artifact.write_text('{}')
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('UPDATE artifacts SET paper_id=999')
            db.commit()
        with self.assertRaisesRegex(snap.SnapshotError, 'foreign keys'):
            self.export()

    def test_absolute_traversal_pdf_masquerade_and_secret_artifact_paths_refused(self):
        for name in ('/private/tmp/secret.json', 'data/extractions/../secret.json',
                     'data/extractions/secret.json', 'data/extractions/a.pdf', 'data/models/a.json'):
            with self.subTest(name=name):
                with closing(sqlite3.connect(self.db)) as db:
                    db.execute('UPDATE artifacts SET path=? WHERE id=1', (name,))
                    db.commit()
                with self.assertRaises(snap.SnapshotError):
                    self.export()

    def test_symlinks_and_hardlinks_refused(self):
        artifact = self.source / 'data/extractions/one.json'
        original = artifact.read_bytes()
        artifact.unlink()
        other = self.root / 'outside.json'
        other.write_bytes(original)
        artifact.symlink_to(other)
        with self.assertRaises(snap.SnapshotError):
            self.export()
        artifact.unlink()
        os.link(other, artifact)
        with self.assertRaises(snap.SnapshotError):
            self.export()

    def test_writer_fence_overlapping_roots_nonprod_and_duplicate_exports_refused(self):
        with self.assertRaises(snap.SnapshotError):
            snap.export_snapshot(self.source, self.exports, stream=self.stream, revision=1,
                                 source_revision='a'*40, key=self.key)
        with self.assertRaises(snap.SnapshotError):
            snap.export_snapshot(self.source, self.source / 'export', stream=self.stream, revision=1,
                                 source_revision='a'*40, key=self.key, source_quiesced=True)
        self.export()
        with self.assertRaisesRegex(snap.SnapshotError, 'already exported'):
            self.export()
        (self.source / 'data/MINI_NONPROD_SYNTHETIC').write_text('fixture')
        with self.assertRaises(snap.SnapshotError):
            self.export(2)

    def test_authentication_tamper_wrong_key_and_stream(self):
        package = self.export()
        for key in (b'x'*32, b'short'):
            with self.assertRaises(snap.SnapshotError):
                self.pull(package, key=key)
            self.assert_no_selection()
        with self.assertRaises(snap.SnapshotError):
            self.pull(package, stream='other-production')
        for name in ('manifest.json', 'snapshot.tar.gz', 'auth.json'):
            with self.subTest(name=name):
                path = package / name
                old = path.read_bytes()
                path.write_bytes(old + b'x')
                with self.assertRaises(snap.SnapshotError):
                    self.pull(package)
                self.assert_no_selection()
                path.write_bytes(old)

    def test_stale_duplicate_and_capture_time_checks(self):
        package = self.export()
        manifest = self.manifest(package)
        for timestamp in (datetime.now(timezone.utc)-timedelta(days=8), datetime.now(timezone.utc)+timedelta(hours=1)):
            altered = {**manifest, 'created_at': timestamp.isoformat()}
            self.resign(package, altered)
            with self.assertRaises(snap.SnapshotError):
                self.pull(package)
            self.assert_no_selection()
        self.resign(package, manifest)
        self.pull(package)
        with self.assertRaisesRegex(snap.SnapshotError, 'duplicate'):
            self.pull(package)
        second = self.export(2)
        changed = self.manifest(second)
        changed['created_at'] = (datetime.fromisoformat(manifest['created_at'])-timedelta(seconds=1)).isoformat()
        self.resign(second, changed)
        with self.assertRaisesRegex(snap.SnapshotError, 'regressed'):
            self.pull(second)

    def test_interrupted_transfer_cleans_stage_and_preserves_input(self):
        package = self.export()
        self.pull(package)
        state_before = (self.store / 'state.json').read_bytes()
        class BrokenSource(store.LocalPackage):
            def open(self, name):
                if name == 'snapshot.tar.gz':
                    class Broken(io.BytesIO):
                        def read(self, n=-1):
                            raise OSError('PRIVATE-TRANSPORT-SECRET')
                    return Broken()
                return super().open(name)
        with self.assertRaises(snap.SnapshotError) as error:
            store.pull_snapshot(BrokenSource(package), self.store, stream=self.stream, key=self.key)
        self.assertNotIn('PRIVATE-TRANSPORT-SECRET', str(error.exception))
        self.assertEqual((self.store / 'state.json').read_bytes(), state_before)
        self.assertFalse(list(self.store.glob('.incoming-*')))

    def test_truncated_signed_gzip_hash_mismatch_and_missing_member(self):
        package = self.export()
        archive = package / 'snapshot.tar.gz'
        original = archive.read_bytes()
        archive.write_bytes(original[:-8])
        self.resign(package)
        with self.assertRaises(snap.SnapshotError):
            self.pull(package)
        self.assert_no_selection()
        archive.write_bytes(original)
        manifest = self.manifest(package)
        manifest['files']['data/extractions/one.json']['sha256'] = '0'*64
        self.resign(package, manifest)
        with self.assertRaisesRegex(snap.SnapshotError, 'checksum'):
            self.pull(package)
        self.assert_no_selection()
        with tarfile.open(archive, 'w:gz', format=tarfile.USTAR_FORMAT):
            pass
        self.resign(package)
        with self.assertRaisesRegex(snap.SnapshotError, 'Incomplete'):
            self.pull(package)

    def test_signed_unsafe_tar_members_and_duplicates_refused(self):
        package = self.export()
        archive = package / 'snapshot.tar.gz'
        for kind, name in ((tarfile.SYMTYPE, 'data/extractions/one.json'),
                           (tarfile.LNKTYPE, 'data/extractions/one.json'),
                           (tarfile.FIFOTYPE, 'data/extractions/one.json'),
                           (tarfile.REGTYPE, '../escape'), (tarfile.REGTYPE, '/absolute'),
                           (tarfile.XHDTYPE, 'pax'), (tarfile.REGTYPE, 'data/rogue.db')):
            with self.subTest(kind=kind, name=name):
                with tarfile.open(archive, 'w:gz', format=tarfile.USTAR_FORMAT) as tar:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    tar.addfile(member)
                self.resign(package)
                with self.assertRaises(snap.SnapshotError):
                    self.pull(package)
                self.assert_no_selection()
        with tarfile.open(archive, 'w:gz', format=tarfile.USTAR_FORMAT) as tar:
            for _ in range(2):
                name = 'data/extractions/one.json'
                data = (package / 'payload' / name).read_bytes()
                member = tarfile.TarInfo(name)
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
        self.resign(package)
        with self.assertRaises(snap.SnapshotError):
            self.pull(package)

    def test_manifest_unapproved_files_and_size_bounds(self):
        package = self.export()
        original = self.manifest(package)
        for name in ('data/other.db', 'data/papers/one.pdf', 'config/.env'):
            value = json.loads(json.dumps(original))
            value['files'][name] = {'size': 0, 'sha256': '0'*64}
            self.resign(package, value)
            with self.assertRaises(snap.SnapshotError):
                self.pull(package)
        value = json.loads(json.dumps(original))
        value['files'][snap.DATABASE]['size'] = snap.MAX_FILE_BYTES + 1
        self.resign(package, value)
        with self.assertRaises(snap.SnapshotError):
            self.pull(package)
        self.resign(package, original)
        with patch.object(snap, 'MAX_ARCHIVE', 8):
            with self.assertRaises(snap.SnapshotError):
                self.pull(package)
        self.assert_no_selection()

    def test_atomic_replacement_rollback_preserves_both_versions_and_replay_barrier(self):
        first = self.export()
        self.pull(first)
        with store.selected_input(self.store, stream=self.stream, key=self.key) as old:
            old_db = (old / snap.DATABASE).read_bytes()
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("UPDATE papers SET title='Second title'")
            db.commit()
        second = self.export(2)
        self.pull(second)
        self.assertEqual((old / snap.DATABASE).read_bytes(), old_db)
        self.assertEqual(store.rollback(self.store, stream=self.stream, key=self.key), 1)
        with store.selected_input(self.store, stream=self.stream, key=self.key) as selected:
            self.assertEqual(selected, old)
            self.assertEqual((selected / snap.DATABASE).read_bytes(), old_db)
        with self.assertRaisesRegex(snap.SnapshotError, 'duplicate'):
            self.pull(second)
        self.assertEqual(store.rollback(self.store, stream=self.stream, key=self.key), 2)
        self.assertEqual(len(list((self.store / 'versions').iterdir())), 2)
        self.assertEqual(self.pull(self.export(3)), 3)

    def test_failed_pointer_switch_preserves_old_state_and_can_retry(self):
        first = self.export()
        self.pull(first)
        before = (self.store / 'state.json').read_bytes()
        second = self.export(2)
        with patch.object(store, 'state_commit', side_effect=OSError('disk full')):
            with self.assertRaises(snap.SnapshotError):
                self.pull(second)
        self.assertEqual((self.store / 'state.json').read_bytes(), before)
        self.assertEqual(len(list((self.store / 'versions').iterdir())), 1)
        self.assertEqual(self.pull(second), 2)

    def test_post_replace_fsync_failure_retains_complete_new_state(self):
        package = self.export()
        original = snap.sync_directory
        def fail_pointer_sync(path):
            if Path(path) == self.store and (self.store / 'state.json').exists():
                raise OSError('fsync failure')
            return original(path)
        with patch.object(snap, 'sync_directory', side_effect=fail_pointer_sync):
            with self.assertRaises(snap.SnapshotError):
                self.pull(package)
        with store.selected_input(self.store, stream=self.stream, key=self.key) as selected:
            self.assertTrue((selected / snap.DATABASE).is_file())
        with self.assertRaisesRegex(snap.SnapshotError, 'duplicate'):
            self.pull(package)

    def test_reader_lock_excludes_install_and_rollback(self):
        first = self.export()
        self.pull(first)
        second = self.export(2)
        with store.selected_input(self.store, stream=self.stream, key=self.key):
            with self.assertRaises(snap.SnapshotError):
                self.pull(second)
            with self.assertRaises(snap.SnapshotError):
                store.rollback(self.store, stream=self.stream, key=self.key)
        self.assertEqual(self.pull(second), 2)

    def test_tampered_previous_payload_blocks_rollback_and_current_remains(self):
        self.pull(self.export())
        with store.selected_input(self.store, stream=self.stream, key=self.key) as first:
            previous = first
        self.pull(self.export(2))
        before = (self.store / 'state.json').read_bytes()
        (previous / 'data/profile.json').write_text('{}')
        with self.assertRaises(snap.SnapshotError):
            store.rollback(self.store, stream=self.stream, key=self.key)
        self.assertEqual((self.store / 'state.json').read_bytes(), before)

    def test_lost_state_and_unsafe_store_do_not_reset_replay_protection(self):
        self.pull(self.export())
        (self.store / 'state.json').unlink()
        with self.assertRaisesRegex(snap.SnapshotError, 'operator recovery'):
            self.pull(self.export(2))
        alias = self.root / 'alias'
        alias.symlink_to(self.store, target_is_directory=True)
        with self.assertRaises(snap.SnapshotError):
            store.pull_snapshot(store.LocalPackage(self.exports / 'snapshot-00000000000000000001'),
                                alias, stream=self.stream, key=self.key)

    def test_source_mutation_during_copy_prevents_publication(self):
        original = snap.config_check
        def mutate(root):
            result = original(root)
            if root == self.source:
                with closing(sqlite3.connect(self.db)) as db:
                    db.execute("UPDATE papers SET title='Changed during export'")
                    db.commit()
            return result
        with patch.object(snap, 'config_check', side_effect=mutate):
            with self.assertRaisesRegex(snap.SnapshotError, 'Database changed'):
                self.export()
        self.assertFalse(list(self.exports.glob('snapshot-*')))

    def test_cli_and_key_errors_do_not_echo_secrets(self):
        key_file = self.root / '.snapshot-secret.key'
        key_file.write_bytes(self.key)
        key_file.chmod(0o600)
        output = io.StringIO()
        with redirect_stdout(output):
            result = store.main(['pull-local', str(self.root / 'missing-secret-path'), str(self.store),
                                 '--stream', self.stream, '--key-file', str(key_file)])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue()), {'status': 'failed', 'operation': 'pull-local'})
        self.assertNotIn('secret-path', output.getvalue())
        self.assertNotIn(self.key.decode(), output.getvalue())
        key_file.chmod(0o644)
        with self.assertRaises(snap.SnapshotError):
            snap.read_key(key_file)

    def test_owned_interrupted_stage_recovered_without_removing_foreign_files(self):
        self.store.mkdir(mode=0o700)
        abandoned = self.store / '.incoming-abandoned'
        abandoned.mkdir()
        (abandoned / 'transfer-owner.json').write_bytes(snap.canonical(
            {'format': 1, 'policy': snap.POLICY, 'directory': abandoned.name}))
        (abandoned / 'snapshot.tar.gz').write_bytes(b'incomplete')
        foreign = self.store / '.incoming-unowned'
        foreign.mkdir()
        (foreign / 'keep').write_text('operator-owned')
        self.pull(self.export())
        self.assertFalse(abandoned.exists())
        self.assertEqual((foreign / 'keep').read_text(), 'operator-owned')

    def test_export_and_pull_cli_success(self):
        key_file = self.root / 'key'
        key_file.write_bytes(self.key)
        key_file.chmod(0o600)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(store.main(['export', str(self.source), str(self.exports), '--revision', '1',
                '--source-revision', 'a'*40, '--source-quiesced', '--stream', self.stream,
                '--key-file', str(key_file)]), 0)
            self.assertEqual(store.main(['pull-local', str(self.exports / 'snapshot-00000000000000000001'),
                str(self.store), '--stream', self.stream, '--key-file', str(key_file)]), 0)
        self.assertEqual([json.loads(line)['status'] for line in output.getvalue().splitlines()], ['ok', 'ok'])

    def test_corrupt_database_and_unlisted_table_refused(self):
        original = self.db.read_bytes()
        self.db.write_bytes(b'not sqlite')
        with self.assertRaises(snap.SnapshotError):
            self.export()
        self.db.write_bytes(original)
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('CREATE TABLE unexpected(value TEXT)')
        with self.assertRaisesRegex(snap.SnapshotError, 'schema'):
            self.export()

    def test_missing_package_file_and_same_destination_refused(self):
        package = self.export()
        (package / 'auth.json').unlink()
        with self.assertRaises(snap.SnapshotError):
            self.pull(package)
        self.assert_no_selection()
        with self.assertRaises(snap.SnapshotError):
            store.pull_snapshot(store.LocalPackage(package), package, stream=self.stream, key=self.key)

    def test_signed_schema_identity_and_reference_mismatch_refused(self):
        package = self.export()
        original = self.manifest(package)
        bad = json.loads(json.dumps(original))
        bad['database']['schema_sha256'] = '0'*64
        self.resign(package, bad)
        with self.assertRaisesRegex(snap.SnapshotError, 'Database manifest'):
            self.pull(package)
        bad = json.loads(json.dumps(original))
        bad['excluded_pdf_references'] = []
        self.resign(package, bad)
        with self.assertRaisesRegex(snap.SnapshotError, 'PDF exclusions'):
            self.pull(package)

    def test_duplicate_json_fields_and_symlinked_package_refused(self):
        package = self.export()
        path = package / 'manifest.json'
        path.write_bytes(b'{"format":1,"format":1}')
        self.resign(package)
        with self.assertRaisesRegex(snap.SnapshotError, 'Duplicate JSON'):
            self.pull(package)
        auth = package / 'auth.json'
        auth.rename(package / 'auth-backup.json')
        auth.symlink_to(package / 'auth-backup.json')
        with self.assertRaises(snap.SnapshotError):
            self.pull(package)


if __name__ == '__main__':
    unittest.main()

"""Explicit one-batch Mini identity repair; never run by cron or sync automatically.

Preserves the held payload, remaps all declared paper references transactionally,
and removes only the now-redundant identity row. Never replaces the database.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile

from .db import normalize_doi
from .sync_worker import atomic_json, lock
from .whole_cycle_exchange import _file_hash, _sync_directory

BATCH = '176efddd00774894a8f36e9ba30141be'
CAPSULE = '0cd8aa34a1522279d7b6bb3010d16abe2b1a0ca1d0be9ecab8d2d9b088f44f76'


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def merge_identity(connection, payload, keep_id, duplicate_id, *, artifact_paths=None):
    """Apply an explicitly reviewed same-paper merge; conflicts roll back."""
    if keep_id == duplicate_id:
        raise ValueError('different paper IDs required')
    papers = payload['normalized']['papers']; sources = payload['normalized']['paper_sources']
    if len(papers) != 1 or len(sources) != 1 or sources[0]['source'] != 'core':
        raise ValueError('repair requires exactly one CORE paper/source')
    incoming, source = papers[0], sources[0]
    doi = normalize_doi(incoming.get('doi'))
    if not doi or incoming['canonical_key'] != 'doi:'+doi or source['canonical_key'] != incoming['canonical_key']:
        raise ValueError('unexpected held DOI identity')
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('BEGIN IMMEDIATE')
    try:
        keep = connection.execute('SELECT * FROM papers WHERE id=?', (keep_id,)).fetchone()
        duplicate = connection.execute('SELECT * FROM papers WHERE id=?', (duplicate_id,)).fetchone()
        if not keep or not duplicate or keep['canonical_key'] != 'core:'+source['source_id'] or duplicate['canonical_key'] != incoming['canonical_key']:
            raise ValueError('reviewed identities changed')
        titles = {' '.join(value.split()).casefold() for value in (keep['title'], duplicate['title'], incoming['title'])}
        if len(titles) != 1 or normalize_doi(duplicate['doi']) != doi or (keep['doi'] and normalize_doi(keep['doi']) != doi):
            raise ValueError('bibliographic identity conflict')
        if keep['arxiv_id'] != duplicate['arxiv_id']:
            raise ValueError('arxiv identity conflict')
        aliases = connection.execute('SELECT source,source_id FROM paper_sources WHERE paper_id=?', (duplicate_id,)).fetchall()
        if [tuple(row) for row in aliases] != [('core',source['source_id'])]:
            raise ValueError('reviewed source ownership changed')
        tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {table:connection.execute('SELECT COUNT(*) FROM '+quoted(table)).fetchone()[0] for table in tables}
        # Keep both artifact registrations/IDs if they shared one live path.
        # The held copy already exists and is validated by the explicit repair.
        artifact_changes = []
        duplicates = connection.execute('''SELECT a.id,a.path FROM artifacts a
            WHERE a.paper_id=? AND EXISTS(SELECT 1 FROM artifacts k
            WHERE k.paper_id=? AND k.artifact_type=a.artifact_type AND k.path=a.path)''',
            (duplicate_id,keep_id)).fetchall()
        for row in duplicates:
            target = (artifact_paths or {}).get(row['path'])
            if not target or not Path(target).is_file():
                raise ValueError('duplicate artifact requires a verified held copy')
            connection.execute('UPDATE artifacts SET path=? WHERE id=?',(target,row['id']))
            artifact_changes.append({'artifact_id':row['id'],'original_path':row['path'],
                                     'held_path':target,'sha256':_file_hash(target)})
        changes = {}
        for table in tables:
            for fk in connection.execute('PRAGMA foreign_key_list('+quoted(table)+')').fetchall():
                if fk['table'] != 'papers': continue
                if fk['to'] != 'id': raise ValueError('unsupported paper foreign key')
                column = fk['from']
                count = connection.execute('SELECT COUNT(*) FROM '+quoted(table)+' WHERE '+quoted(column)+'=?', (duplicate_id,)).fetchone()[0]
                if not count: continue
                connection.execute('UPDATE '+quoted(table)+' SET '+quoted(column)+'=? WHERE '+quoted(column)+'=?', (keep_id,duplicate_id))
                changes[table+'.'+column] = count
        connection.execute('UPDATE papers SET doi=? WHERE id=?', (doi,keep_id))
        connection.execute('DELETE FROM papers WHERE id=?', (duplicate_id,))
        for table in tables:
            after = connection.execute('SELECT COUNT(*) FROM '+quoted(table)).fetchone()[0]
            if after != before[table] - (1 if table == 'papers' else 0):
                raise ValueError('unexpected row loss: '+table)
        if connection.execute('PRAGMA foreign_key_check').fetchone() or connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('repaired database integrity failed')
        connection.commit()
        return {'status':'identity-reconciled','kept_paper_id':keep_id,'removed_duplicate_id':duplicate_id,
                'canonical_key':keep['canonical_key'],'reference_rows_moved':changes,
                'artifact_path_changes':artifact_changes,
                'original_kept_paper':dict(keep),'original_duplicate_paper':dict(duplicate)}
    except BaseException:
        connection.rollback()
        raise


def repair(db_path, state, *, apply=False, keep_id=991, duplicate_id=1783):
    db_path, state = Path(db_path), Path(state)
    capsule = state/'held'/CAPSULE
    manifest = json.loads((capsule/'manifest.json').read_text())
    if manifest['batch_id'] != BATCH or manifest['run_sequence'] != 33:
        raise ValueError('unexpected held batch')
    for name,digest in manifest['files'].items():
        if Path(name).name != name or (capsule/name).is_symlink() or _file_hash(capsule/name) != digest:
            raise ValueError('held evidence integrity failed')
    if _file_hash(capsule/'pipeline-outbox.json') != CAPSULE:
        raise ValueError('held payload hash mismatch')
    payload = json.loads((capsule/'pipeline-outbox.json').read_text())
    if payload['batch_id'] != BATCH or payload['run_sequence'] != 33:
        raise ValueError('unexpected held payload')
    marker = json.loads((state/'identity-restore-required.json').read_text())
    if marker != {'capsule':CAPSULE}:
        raise ValueError('unexpected recovery marker')
    if any((state/name).exists() for name in ('pipeline-outbox.json','active-cycle.json','health-outbox.json')):
        raise ValueError('active work blocks identity repair')
    if json.loads((state/'sequence.json').read_text())['sequence'] != 32 or json.loads((state/'reserved-sequence.json').read_text())['sequence'] != 33:
        raise ValueError('reviewed producer sequence changed')
    artifact_paths = {}
    root = db_path.parent.parent
    for item in manifest.get('artifacts',[]):
        original = Path(item['original_path'])
        original = original if original.is_absolute() else root/original
        target = capsule/item['file']
        if Path(item['file']).name != item['file'] or not original.resolve().is_relative_to(root.resolve()):
            raise ValueError('unsafe reviewed artifact path')
        if _file_hash(original) != _file_hash(target):
            raise ValueError('live summary changed since hold; explicit review required')
        artifact_paths[item['original_path']] = str(target)
    # Plan on a disposable consistent copy first; the live DB is never replaced.
    with tempfile.TemporaryDirectory(dir=state,prefix='.core-repair-plan-') as directory:
        planned = Path(directory)/'plan.db'
        with closing(sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True)) as live, closing(sqlite3.connect(planned)) as copy:
            live.backup(copy)
            result = merge_identity(copy,payload,keep_id,duplicate_id,artifact_paths=artifact_paths)
    result.update(batch_id=BATCH,held_capsule=CAPSULE,held_replayed=False,mode='apply' if apply else 'dry-run')
    if not apply: return result
    backup = state/('before-core-identity-repair-'+BATCH+'.db')
    if backup.exists(): raise ValueError('repair backup already exists; review before repeating')
    with closing(sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True)) as live, closing(sqlite3.connect(backup)) as saved:
        live.backup(saved)
    with backup.open('rb') as stream:
        import os
        os.fsync(stream.fileno())
    _sync_directory(state)
    with closing(sqlite3.connect(db_path)) as live:
        actual = merge_identity(live,payload,keep_id,duplicate_id,artifact_paths=artifact_paths)
    result['reference_rows_moved'] = actual['reference_rows_moved']
    result['artifact_path_changes'] = actual['artifact_path_changes']
    result['backup_file'] = backup.name; result['backup_sha256'] = _file_hash(backup)
    atomic_json(state/('core-identity-repair-'+BATCH+'.json'),result)
    _sync_directory(state)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db-path',required=True,type=Path)
    parser.add_argument('--state-dir',required=True,type=Path)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    from .migration_lifecycle import lease
    with lock(args.state_dir), lease(Path('/runtime-control')), lease(Path('/runtime-control'),exclusive=True,name='.scheduled-job.lock'):
        result=repair(args.db_path,args.state_dir,apply=args.apply)
    # Keep private pre-repair row details in the local audit only.
    print(json.dumps({k:v for k,v in result.items() if not k.startswith('original_')},sort_keys=True))
    return 0


if __name__ == '__main__': raise SystemExit(main())

"""Read-only startup validation for explicitly selected AWS production serving.

Never creates, restores, seeds or migrates state. The normal UI owns subsequent
user writes. Mini and nonproduction validators retain their existing contracts.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .import_state import schema, trusted_schemas
from .sync_backup import _safe
from .sync_exchange import _setup
from .topics import load_topic_config
from .industry_config import load_zenml_config


def _approved_optional_schema():
    """Known additive tables retained by older AWS serving databases.

    These tables are not migrated or written by serving startup.  We render the
    definitions here so compatibility remains an exact, allowlisted check.
    """
    with closing(sqlite3.connect(':memory:')) as connection:
        _setup(connection)
        connection.executescript('''
            CREATE TABLE topic_agent_allowance (
                allowance_day TEXT PRIMARY KEY, limit_count INTEGER NOT NULL,
                reserved_count INTEGER NOT NULL DEFAULT 0, consumed_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT(datetime('now'))
            );
            CREATE TABLE topic_agent_requests (
                request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, base_topics_hash TEXT NOT NULL,
                request_text TEXT NOT NULL, conversation_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
                proposal_json TEXT, error_category TEXT, created_at TEXT NOT NULL DEFAULT(datetime('now')),
                updated_at TEXT NOT NULL DEFAULT(datetime('now'))
            );
            CREATE INDEX idx_topic_agent_requests_status
                ON topic_agent_requests(status, created_at);
        ''')
        return {(row[0], row[1]): row for row in schema(connection)}


def _schema_row_key(row):
    """Normalize SQLite's harmless SQL formatting differences for allowlists."""
    if not row:
        return row
    if row[0] != 'table' and row[0] != 'index':
        return row
    sql = ' '.join((row[3] or '').replace('"', '').split())
    return row[:3] + (sql,)


def validate(root, *, full=True):
    root=Path(root).absolute()
    if any(p.is_symlink() for p in (root,*root.parents)):
        raise RuntimeError('AWS state root must not be aliased')
    for relative in ('data/paper_agent.db','data/profile.json','config/topics.yaml','config/industry.json'):
        if not _safe(root,relative).is_file():
            raise RuntimeError('Required existing AWS serving state missing')
    profile=json.loads((root/'data/profile.json').read_text())
    if not isinstance(profile,dict) or any(not isinstance(profile.get(k),list) for k in
            ('interests','positive_signals','negative_signals','feedback_history')):
        raise RuntimeError('Invalid existing AWS profile')
    load_topic_config(root/'config/topics.yaml')
    load_zenml_config(root/'config/industry.json')
    with closing(sqlite3.connect(':memory:')) as metadata:
        _setup(metadata)
        optional={(r[0],r[1]):r for r in schema(metadata)}
        optional.update(_approved_optional_schema())
    with closing(sqlite3.connect((root/'data/paper_agent.db').as_uri()+'?mode=ro',uri=True)) as db:
        actual={(r[0],r[1]):r for r in schema(db)}
        approved=False
        for expected in trusted_schemas():
            required={(r[0],r[1]):r for r in expected}
            if all(_schema_row_key(actual.get(k, ())) == _schema_row_key(v) for k,v in required.items()) and all(
                    k in required or _schema_row_key(optional.get(k, ())) == _schema_row_key(v)
                    for k,v in actual.items()):
                approved=True; break
        if not approved:raise RuntimeError('Unapproved AWS serving schema; no automatic migration')
        if full:
            if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)] or db.execute('PRAGMA foreign_key_check').fetchone():
                raise RuntimeError('AWS serving database integrity failed')
            for kind,stored in db.execute('SELECT artifact_type,path FROM artifacts'):
                if 'summary' in kind and (not stored.startswith('data/') or not _safe(root,stored).is_file()):
                    raise RuntimeError('AWS serving summary missing or unsafe')
        return {'state':'aws-production','tables':[r[1] for r in actual.values() if r[0]=='table'],
            'papers':db.execute('SELECT COUNT(*) FROM papers').fetchone()[0]}

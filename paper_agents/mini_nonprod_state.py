"""Read-only validation of explicit synthetic Mini state; no restore or migration."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .topics import load_topic_config
from .industry_config import load_zenml_config

SCHEMA = Path(__file__).resolve().parents[1] / 'sql/schema.sql'


def validate(root):
    root = Path(root).absolute()
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise RuntimeError('Synthetic state root must not be aliased')
    required = ('data/MINI_NONPROD_SYNTHETIC', 'data/paper_agent.db',
                'data/profile.json', 'config/topics.yaml', 'config/industry.json')
    for relative in required:
        path = root / relative
        if not path.is_file() or path.is_symlink() or path.parent.is_symlink():
            raise RuntimeError('Required synthetic state absent or aliased: ' + relative)
    if (root / required[0]).read_text() != 'synthetic-v1\n':
        raise RuntimeError('Synthetic state marker mismatch')
    profile = json.loads((root / 'data/profile.json').read_text())
    if not isinstance(profile, dict) or any(not isinstance(profile.get(k), list)
            for k in ('interests', 'positive_signals', 'negative_signals', 'feedback_history')):
        raise RuntimeError('Invalid synthetic profile')
    load_topic_config(root / 'config/topics.yaml')
    load_zenml_config(root / 'config/industry.json')
    query = "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    with closing(sqlite3.connect(':memory:')) as expected:
        expected.executescript(SCHEMA.read_text())
        expected_schema = expected.execute(query).fetchall()
    uri = (root / 'data/paper_agent.db').as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as actual:
        if actual.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or actual.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Synthetic database integrity failure')
        if actual.execute(query).fetchall() != expected_schema:
            raise RuntimeError('Synthetic schema differs from this image; no automatic migration')
        for (stored,) in actual.execute('SELECT path FROM artifacts'):
            path = root / stored
            if not str(stored).startswith('data/') or not path.resolve().is_relative_to((root / 'data').resolve()):
                raise RuntimeError('Artifact path outside isolated data')
            if not path.is_file():
                raise RuntimeError('Synthetic artifact missing')
        return {'papers': actual.execute('SELECT COUNT(*) FROM papers').fetchone()[0],
                'schema': 'exact', 'state': 'synthetic-only',
                'tables': [row[1] for row in expected_schema if row[0] == 'table']}

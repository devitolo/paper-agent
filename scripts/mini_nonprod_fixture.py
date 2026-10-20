#!/usr/bin/env python3
"""Create NEW synthetic Mini nonprod input offline; never read production state."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def create(destination, repo=None):
    repo = Path(repo) if repo else Path(__file__).resolve().parents[1]
    destination = Path(destination).absolute()
    if destination.exists() or any(p.is_symlink() for p in destination.parents):
        raise ValueError('new unaliased destination required')
    destination.mkdir(mode=0o700)
    (destination / 'data').mkdir()
    (destination / 'config').mkdir()
    db = sqlite3.connect(destination / 'data/paper_agent.db')
    try:
        with db:
            db.executescript((repo / 'sql/schema.sql').read_text())
            from paper_agents.sync_exchange import _setup
            _setup(db)
            db.row_factory = None
            db.execute("INSERT INTO papers(canonical_key,title) VALUES (?,?)",
                       ('mini-nonprod-synthetic', 'SYNTHETIC Mini non-production example'))
            db.execute("INSERT INTO feedback(paper_id,status,notes) VALUES (1,'keep','Synthetic fixture')")
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        db.close()
    (destination / 'data/profile.json').write_text(json.dumps(dict(
        interests=[], positive_signals=[], negative_signals=[], feedback_history=[])))
    (destination / 'config/topics.yaml').write_text('topics: []\n')
    (destination / 'config/industry.json').write_text('{}\n')
    (destination / 'data/MINI_NONPROD_SYNTHETIC').write_text('synthetic-v1\n')
    from paper_agents.serving_state import validate
    validate(destination, migrate=False)
    for name in ('logs', 'control', 'generated'):
        (destination / name).mkdir()
    files = {str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(destination.rglob('*')) if p.is_file()}
    (destination / 'fixture.json').write_text(json.dumps(
        {'purpose': 'mini-nonprod-synthetic-only', 'files': files}, indent=2)+'\n')
    return files


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    create(args.destination)
    print('Synthetic fixture created; image-specific offline schema validation still required.')

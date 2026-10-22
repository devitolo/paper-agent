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
    (destination / 'data/extractions').mkdir()
    (destination / 'config').mkdir()
    db = sqlite3.connect(destination / 'data/paper_agent.db')
    try:
        with db:
            db.executescript((repo / 'sql/schema.sql').read_text())
            db.row_factory = None
            db.execute("INSERT INTO papers(canonical_key,title) VALUES (?,?)",
                       ('mini-nonprod-synthetic', 'SYNTHETIC Mini non-production example'))
            db.execute("INSERT INTO paper_sources(paper_id,source,source_id,url,metadata_json) VALUES (1,'synthetic','mini-nonprod-synthetic','https://example.invalid/mini-nonprod',?)",
                       (json.dumps({'original_title': 'SYNTHETIC Mini non-production example'}),))
            db.execute("INSERT INTO workflow_cycles(id,state,mode,metadata_json) VALUES (1,'recommendations_ready','synthetic',?)",
                       (json.dumps({'environment': 'mini-nonprod'}),))
            db.execute("INSERT INTO curator_runs(id,workflow_cycle_id,model,metadata_json) VALUES (1,1,'synthetic',?)",
                       (json.dumps({'environment': 'mini-nonprod'}),))
            db.execute("INSERT INTO curator_evaluations(curator_run_id,paper_id,score,rationale,matched_signals_json,quality_threshold_met) VALUES (1,1,92,'Synthetic fixture recommendation',?,1)",
                       (json.dumps(['synthetic']),))
            db.execute("INSERT INTO recommendations(id,curator_run_id,paper_id,recommendation_order,rationale) VALUES (1,1,1,1,'Synthetic fixture recommendation')")
            db.execute("INSERT INTO artifacts(paper_id,artifact_type,path,model,metadata_json) VALUES (1,'triage_summary','data/extractions/mini-nonprod.json','synthetic',?)",
                       (json.dumps({'environment': 'mini-nonprod'}),))
            db.execute("INSERT INTO feedback(paper_id,status,notes) VALUES (1,'keep','Synthetic fixture')")
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        db.close()
    (destination / 'data/extractions/mini-nonprod.json').write_text(json.dumps({
        'summary': 'Synthetic non-production paper for UI and lifecycle verification.',
        'why_it_matters': 'Confirms the isolated review queue has visible fixture content.',
        'approach': 'Generated locally without production or external service access.'
    }) + '\n')
    (destination / 'data/profile.json').write_text(json.dumps(dict(
        interests=[], positive_signals=[], negative_signals=[], feedback_history=[])))
    (destination / 'config/topics.yaml').write_text('topics: []\n')
    (destination / 'config/industry.json').write_text(json.dumps(dict(
        enabled=False, include_signals=[], exclude_signals=[], max_items_per_run=1,
        schedule='Disabled — synthetic nonprod bootstrap')) + '\n')
    (destination / 'data/MINI_NONPROD_SYNTHETIC').write_text('synthetic-v1\n')
    from paper_agents.mini_nonprod_state import validate
    validate(destination)
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

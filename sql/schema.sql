PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    pdf_url TEXT,
    published TEXT,
    updated TEXT,
    authors_json TEXT NOT NULL DEFAULT '[]',
    categories_json TEXT NOT NULL DEFAULT '[]',
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_papers_published ON papers (published);
CREATE INDEX IF NOT EXISTS idx_papers_title ON papers (title);

CREATE TABLE IF NOT EXISTS scout_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT NOT NULL,
    fetch_limit INTEGER NOT NULL,
    keep_limit INTEGER NOT NULL,
    topics_json TEXT NOT NULL DEFAULT '[]',
    mode TEXT NOT NULL DEFAULT 'manual'
);

CREATE INDEX IF NOT EXISTS idx_scout_runs_started_at ON scout_runs (started_at);

CREATE TABLE IF NOT EXISTS scout_candidates (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES scout_runs(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    score REAL NOT NULL DEFAULT 0,
    matched_keywords_json TEXT NOT NULL DEFAULT '[]',
    ranking_reason TEXT,
    selected INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0, 1)),
    UNIQUE (run_id, paper_id)
);

CREATE INDEX IF NOT EXISTS idx_scout_candidates_run_id ON scout_candidates (run_id);
CREATE INDEX IF NOT EXISTS idx_scout_candidates_paper_id ON scout_candidates (paper_id);
CREATE INDEX IF NOT EXISTS idx_scout_candidates_selected ON scout_candidates (selected);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (paper_id, artifact_type, path)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_paper_id ON artifacts (paper_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_type ON artifacts (artifact_type);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    rating INTEGER,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (rating IS NULL OR (rating >= 1 AND rating <= 5))
);

CREATE INDEX IF NOT EXISTS idx_feedback_paper_id ON feedback (paper_id);
CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback (status);

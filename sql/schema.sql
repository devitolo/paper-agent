PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY,
    canonical_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    abstract TEXT,
    published TEXT,
    updated TEXT,
    doi TEXT,
    arxiv_id TEXT,
    authors_json TEXT NOT NULL DEFAULT '[]',
    categories_json TEXT NOT NULL DEFAULT '[]',
    first_discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_discovered_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_papers_arxiv_id ON papers (arxiv_id);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers (doi);
CREATE INDEX IF NOT EXISTS idx_papers_published ON papers (published);
CREATE INDEX IF NOT EXISTS idx_papers_title ON papers (title);

CREATE TABLE IF NOT EXISTS paper_sources (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT,
    pdf_url TEXT,
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_paper_sources_paper_id ON paper_sources (paper_id);
CREATE INDEX IF NOT EXISTS idx_paper_sources_source ON paper_sources (source, source_id);

CREATE TABLE IF NOT EXISTS workflow_cycles (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    state TEXT NOT NULL DEFAULT 'created',
    mode TEXT NOT NULL DEFAULT 'manual',
    max_scout_attempts INTEGER NOT NULL DEFAULT 3,
    scout_attempts_used INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_workflow_cycles_state ON workflow_cycles (state);
CREATE INDEX IF NOT EXISTS idx_workflow_cycles_created_at ON workflow_cycles (created_at);

CREATE TABLE IF NOT EXISTS scouting_guidance (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    curator_run_id INTEGER REFERENCES curator_runs(id) ON DELETE SET NULL,
    guidance_text TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    expires_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_scouting_guidance_active ON scouting_guidance (active, id);

CREATE TABLE IF NOT EXISTS scout_runs (
    id INTEGER PRIMARY KEY,
    workflow_cycle_id INTEGER REFERENCES workflow_cycles(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    source TEXT NOT NULL,
    target_candidates INTEGER NOT NULL,
    max_candidates INTEGER NOT NULL,
    freshness_months INTEGER NOT NULL,
    topics_json TEXT NOT NULL DEFAULT '[]',
    guidance_id INTEGER REFERENCES scouting_guidance(id) ON DELETE SET NULL,
    diagnostics_json TEXT NOT NULL DEFAULT '{}',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    errors_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_scout_runs_cycle ON scout_runs (workflow_cycle_id);
CREATE INDEX IF NOT EXISTS idx_scout_runs_started_at ON scout_runs (started_at);

CREATE TABLE IF NOT EXISTS scout_candidates (
    id INTEGER PRIMARY KEY,
    scout_run_id INTEGER NOT NULL REFERENCES scout_runs(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    retrieval_order INTEGER NOT NULL,
    is_new INTEGER NOT NULL DEFAULT 1 CHECK (is_new IN (0, 1)),
    excluded INTEGER NOT NULL DEFAULT 0 CHECK (excluded IN (0, 1)),
    exclusion_reason TEXT,
    source_query TEXT,
    source_diagnostics_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (scout_run_id, paper_id)
);

CREATE INDEX IF NOT EXISTS idx_scout_candidates_run_id ON scout_candidates (scout_run_id);
CREATE INDEX IF NOT EXISTS idx_scout_candidates_paper_id ON scout_candidates (paper_id);
CREATE INDEX IF NOT EXISTS idx_scout_candidates_excluded ON scout_candidates (excluded);

CREATE TABLE IF NOT EXISTS curator_runs (
    id INTEGER PRIMARY KEY,
    workflow_cycle_id INTEGER NOT NULL REFERENCES workflow_cycles(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    profile_version_id INTEGER REFERENCES profile_versions(id) ON DELETE SET NULL,
    scout_attempt_count INTEGER NOT NULL DEFAULT 1,
    max_scout_attempts INTEGER NOT NULL DEFAULT 3,
    min_quality_score REAL NOT NULL DEFAULT 25,
    max_recommendations INTEGER NOT NULL DEFAULT 3,
    requested_rescout INTEGER NOT NULL DEFAULT 0 CHECK (requested_rescout IN (0, 1)),
    rescout_reason TEXT,
    model TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_curator_runs_cycle ON curator_runs (workflow_cycle_id);

CREATE TABLE IF NOT EXISTS curator_evaluations (
    id INTEGER PRIMARY KEY,
    curator_run_id INTEGER NOT NULL REFERENCES curator_runs(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    scout_candidate_id INTEGER REFERENCES scout_candidates(id) ON DELETE SET NULL,
    score REAL NOT NULL,
    rationale TEXT NOT NULL,
    matched_signals_json TEXT NOT NULL DEFAULT '[]',
    quality_threshold_met INTEGER NOT NULL DEFAULT 0 CHECK (quality_threshold_met IN (0, 1)),
    UNIQUE (curator_run_id, paper_id)
);

CREATE INDEX IF NOT EXISTS idx_curator_evaluations_run ON curator_evaluations (curator_run_id);
CREATE INDEX IF NOT EXISTS idx_curator_evaluations_paper ON curator_evaluations (paper_id);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY,
    curator_run_id INTEGER NOT NULL REFERENCES curator_runs(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    recommendation_order INTEGER NOT NULL,
    rationale TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'recommended',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (curator_run_id, paper_id),
    UNIQUE (curator_run_id, recommendation_order),
    CHECK (recommendation_order >= 1 AND recommendation_order <= 3)
);

CREATE INDEX IF NOT EXISTS idx_recommendations_run ON recommendations (curator_run_id);
CREATE INDEX IF NOT EXISTS idx_recommendations_paper ON recommendations (paper_id);

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

CREATE TABLE IF NOT EXISTS raw_feedback (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER REFERENCES papers(id) ON DELETE SET NULL,
    recommendation_id INTEGER REFERENCES recommendations(id) ON DELETE SET NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_raw_feedback_paper ON raw_feedback (paper_id);

CREATE TABLE IF NOT EXISTS feedback_parse_attempts (
    id INTEGER PRIMARY KEY,
    raw_feedback_id INTEGER NOT NULL REFERENCES raw_feedback(id) ON DELETE CASCADE,
    attempted_at TEXT NOT NULL DEFAULT (datetime('now')),
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    model TEXT,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    error TEXT,
    output_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_feedback_parse_attempts_raw ON feedback_parse_attempts (raw_feedback_id);

CREATE TABLE IF NOT EXISTS structured_feedback (
    id INTEGER PRIMARY KEY,
    parse_attempt_id INTEGER NOT NULL UNIQUE REFERENCES feedback_parse_attempts(id) ON DELETE CASCADE,
    paper_id INTEGER REFERENCES papers(id) ON DELETE SET NULL,
    decision TEXT,
    score REAL,
    observations_json TEXT NOT NULL DEFAULT '[]',
    preference_signals_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (score IS NULL OR (score >= 1 AND score <= 5))
);

CREATE INDEX IF NOT EXISTS idx_structured_feedback_paper ON structured_feedback (paper_id);
CREATE INDEX IF NOT EXISTS idx_structured_feedback_decision ON structured_feedback (decision);

CREATE TABLE IF NOT EXISTS profile_versions (
    id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    profile_json TEXT NOT NULL,
    source_structured_feedback_id INTEGER REFERENCES structured_feedback(id) ON DELETE SET NULL,
    change_summary TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_profile_versions_active ON profile_versions (active, version);

CREATE TABLE IF NOT EXISTS feedback_profile_applications (
    id INTEGER PRIMARY KEY,
    structured_feedback_id INTEGER NOT NULL UNIQUE REFERENCES structured_feedback(id) ON DELETE CASCADE,
    profile_version_id INTEGER NOT NULL REFERENCES profile_versions(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_feedback_profile_applications_profile ON feedback_profile_applications (profile_version_id);

CREATE TABLE IF NOT EXISTS feedback_profile_apply_attempts (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT,
    structured_feedback_ids_json TEXT NOT NULL DEFAULT '[]',
    dry_run INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    error TEXT,
    profile_version_id INTEGER REFERENCES profile_versions(id) ON DELETE SET NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_feedback_profile_apply_attempts_status ON feedback_profile_apply_attempts (status, created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_profile_apply_attempts_profile ON feedback_profile_apply_attempts (profile_version_id);

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

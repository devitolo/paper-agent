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

CREATE TABLE IF NOT EXISTS scout_diagnostic_reports (
    scout_run_id INTEGER PRIMARY KEY REFERENCES scout_runs(id) ON DELETE CASCADE,
    report_json TEXT NOT NULL
);

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


-- Versioned OpenAlex traversal state; enabled only by the cursor retrieval path.
CREATE TABLE IF NOT EXISTS openalex_search_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS openalex_page_dispositions (
    id INTEGER PRIMARY KEY,
    scout_run_id INTEGER NOT NULL REFERENCES scout_runs(id),
    query_key TEXT NOT NULL,
    topic TEXT NOT NULL,
    page_json TEXT NOT NULL
);

-- Versioned summary-field quality feedback; additive after the OpenAlex traversal state.
CREATE TABLE IF NOT EXISTS summary_field_feedback (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    artifact_id INTEGER NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL CHECK (field_name IN ('research_problem', 'why_it_matters', 'approach')),
    field_text TEXT NOT NULL,
    model TEXT,
    artifact_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (artifact_id, field_name)
);

CREATE INDEX IF NOT EXISTS idx_summary_field_feedback_paper
ON summary_field_feedback (paper_id);

-- Temporary MiniLM Eval experiment; isolated from production ranking and feedback/profile learning.
CREATE TABLE IF NOT EXISTS minilm_eval_runs (
    id INTEGER PRIMARY KEY,
    source_run_id INTEGER NOT NULL UNIQUE REFERENCES scout_runs(id) ON DELETE CASCADE,
    candidate_pool_snapshot_json TEXT NOT NULL DEFAULT '{}',
    minilm_query_version TEXT NOT NULL,
    minilm_model_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS minilm_eval_queue (
    id INTEGER PRIMARY KEY,
    eval_run_id INTEGER NOT NULL REFERENCES minilm_eval_runs(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    recommendation_mode TEXT NOT NULL CHECK (recommendation_mode IN ('baseline', 'minilm_assisted')),
    rank_position INTEGER NOT NULL CHECK (rank_position > 0),
    minilm_raw_logit REAL,
    minilm_bucket TEXT,
    title_snapshot TEXT NOT NULL,
    abstract_snapshot TEXT,
    problem_snapshot TEXT,
    why_it_matters_snapshot TEXT,
    approach_snapshot TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (eval_run_id, recommendation_mode, paper_id),
    UNIQUE (eval_run_id, recommendation_mode, rank_position)
);

CREATE INDEX IF NOT EXISTS idx_minilm_eval_queue_run_mode
ON minilm_eval_queue (eval_run_id, recommendation_mode, rank_position);

CREATE TABLE IF NOT EXISTS minilm_eval_decisions (
    id INTEGER PRIMARY KEY,
    eval_run_id INTEGER NOT NULL REFERENCES minilm_eval_runs(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    representative_queue_item_id INTEGER REFERENCES minilm_eval_queue(id) ON DELETE SET NULL,
    decision TEXT NOT NULL CHECK (decision IN ('send_to_curator', 'maybe', 'skip')),
    decided_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (eval_run_id, paper_id)
);

-- Versioned Semantic Scholar offset traversal state; enabled only by the progressive retrieval path.
CREATE TABLE IF NOT EXISTS semantic_scholar_search_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS semantic_scholar_page_dispositions (
    id INTEGER PRIMARY KEY,
    scout_run_id INTEGER NOT NULL REFERENCES scout_runs(id),
    query_key TEXT NOT NULL,
    topic TEXT NOT NULL,
    page_json TEXT NOT NULL
);

-- Versioned bidirectional summary-field quality signals; legacy feedback rows remain down signals.
CREATE TABLE IF NOT EXISTS summary_field_quality_signals (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    artifact_id INTEGER NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL CHECK (field_name IN ('research_problem', 'why_it_matters', 'approach')),
    signal TEXT NOT NULL CHECK (signal IN ('up', 'down')),
    field_text TEXT NOT NULL,
    model TEXT,
    artifact_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (artifact_id, field_name)
);

CREATE INDEX IF NOT EXISTS idx_summary_field_quality_signals_paper
ON summary_field_quality_signals (paper_id);

-- Versioned arXiv offset traversal state; enabled only by the progressive retrieval path.
CREATE TABLE IF NOT EXISTS arxiv_search_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS arxiv_page_dispositions (
    id INTEGER PRIMARY KEY,
    scout_run_id INTEGER NOT NULL REFERENCES scout_runs(id),
    query_key TEXT NOT NULL,
    topic TEXT NOT NULL,
    page_json TEXT NOT NULL
);

-- Versioned CORE v3 Works offset traversal state; enabled only by the opt-in trial.
CREATE TABLE IF NOT EXISTS core_search_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS core_page_dispositions (
    id INTEGER PRIMARY KEY,
    scout_run_id INTEGER NOT NULL REFERENCES scout_runs(id),
    query_key TEXT NOT NULL,
    topic TEXT NOT NULL,
    page_json TEXT NOT NULL
);

-- Temporary paired pre-Curator MiniLM shadow experiment; isolated from production behavior and learning.
CREATE TABLE IF NOT EXISTS minilm_shadow_runs (
    id INTEGER PRIMARY KEY,
    source_run_id INTEGER NOT NULL UNIQUE REFERENCES scout_runs(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
    pool_snapshot_json TEXT NOT NULL,
    pool_hash TEXT NOT NULL,
    input_limit INTEGER NOT NULL CHECK (input_limit > 0),
    output_limit INTEGER NOT NULL CHECK (output_limit > 0),
    min_quality_score REAL NOT NULL,
    minilm_model_id TEXT NOT NULL,
    minilm_model_hash TEXT NOT NULL,
    minilm_query_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    failure_reason TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS minilm_shadow_path_results (
    id INTEGER PRIMARY KEY,
    shadow_run_id INTEGER NOT NULL REFERENCES minilm_shadow_runs(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    path TEXT NOT NULL CHECK (path IN ('baseline', 'minilm')),
    original_rank INTEGER NOT NULL CHECK (original_rank > 0),
    minilm_raw_logit REAL,
    minilm_rank INTEGER,
    curator_input_position INTEGER NOT NULL CHECK (curator_input_position > 0),
    curator_score_value REAL,
    curator_rationale TEXT,
    curator_accepted INTEGER NOT NULL DEFAULT 0 CHECK (curator_accepted IN (0, 1)),
    final_output_position INTEGER,
    failure_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (shadow_run_id, path, paper_id),
    UNIQUE (shadow_run_id, path, curator_input_position)
);

CREATE INDEX IF NOT EXISTS idx_minilm_shadow_path_run_path
ON minilm_shadow_path_results (shadow_run_id, path, curator_accepted, final_output_position);

CREATE TABLE IF NOT EXISTS minilm_shadow_outputs (
    id INTEGER PRIMARY KEY,
    shadow_run_id INTEGER NOT NULL REFERENCES minilm_shadow_runs(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    canonical_key_snapshot TEXT,
    source_snapshot TEXT,
    source_id_snapshot TEXT,
    title_snapshot TEXT NOT NULL,
    abstract_snapshot TEXT,
    problem_snapshot TEXT,
    why_it_matters_snapshot TEXT,
    approach_snapshot TEXT,
    summary_source TEXT NOT NULL CHECK (summary_source IN ('existing_full_text', 'existing_abstract', 'generated_abstract', 'unavailable')),
    summary_hash TEXT NOT NULL,
    baseline_output_position INTEGER,
    minilm_output_position INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (shadow_run_id, paper_id)
);

CREATE TABLE IF NOT EXISTS minilm_shadow_decisions (
    id INTEGER PRIMARY KEY,
    shadow_run_id INTEGER NOT NULL REFERENCES minilm_shadow_runs(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    would_sample TEXT NOT NULL CHECK (would_sample IN ('yes', 'maybe', 'no')),
    usefulness INTEGER NOT NULL CHECK (usefulness BETWEEN 1 AND 5),
    reason_tags_json TEXT NOT NULL DEFAULT '[]',
    decided_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (shadow_run_id, paper_id)
);

-- Personal review collections; isolated from feedback, ranking, and profile learning.
CREATE TABLE IF NOT EXISTS saved_papers (
    paper_id INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_saved_papers_created_at
ON saved_papers (created_at DESC);

CREATE TABLE IF NOT EXISTS excluded_papers (
    paper_id INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_excluded_papers_created_at
ON excluded_papers (created_at DESC);

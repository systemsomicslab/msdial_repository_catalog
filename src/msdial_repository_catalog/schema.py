SCHEMA_VERSION = 2

BASE_SCHEMA = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_run (
    crawl_run_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    crawler_version TEXT NOT NULL,
    status TEXT NOT NULL,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    hydrated_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS study (
    study_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    accession TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    public_url TEXT NOT NULL DEFAULT '',
    license TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL,
    source_updated_at TEXT NOT NULL DEFAULT '',
    retrieved_at TEXT NOT NULL DEFAULT '',
    parser_version TEXT NOT NULL DEFAULT '',
    current_snapshot_id TEXT NOT NULL DEFAULT '',
    source_payload_json TEXT NOT NULL DEFAULT '{}',
    source_urls_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(repository, accession)
);

CREATE TABLE IF NOT EXISTS source_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES study(study_id) ON DELETE CASCADE,
    source_hash TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    parser_version TEXT NOT NULL DEFAULT '',
    source_blob_hash TEXT NOT NULL DEFAULT '',
    source_payload_json TEXT NOT NULL DEFAULT '{}',
    source_urls_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(study_id, source_hash, parser_version)
);

CREATE TABLE IF NOT EXISTS source_blob (
    source_hash TEXT PRIMARY KEY,
    encoding TEXT NOT NULL DEFAULT 'gzip-json-v1',
    payload BLOB NOT NULL,
    uncompressed_bytes INTEGER NOT NULL,
    compressed_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publication (
    publication_id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES study(study_id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    doi TEXT NOT NULL DEFAULT '',
    pubmed_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS analysis_unit (
    unit_id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES study(study_id) ON DELETE CASCADE,
    source_subrecord_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    separation TEXT NOT NULL DEFAULT 'Unknown',
    chromatography TEXT NOT NULL DEFAULT 'Unknown',
    ion_mode TEXT NOT NULL DEFAULT 'Unknown',
    acquisition_mode TEXT NOT NULL DEFAULT 'Unknown',
    ion_mobility TEXT NOT NULL DEFAULT 'Unknown',
    instrument TEXT NOT NULL DEFAULT '',
    target_omics TEXT NOT NULL DEFAULT 'Unknown',
    untargeted INTEGER,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    signature TEXT NOT NULL,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(study_id, source_subrecord_id, signature)
);

CREATE TABLE IF NOT EXISTS analysis_unit_evidence (
    evidence_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES analysis_unit(unit_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_field TEXT NOT NULL DEFAULT '',
    source_value TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL DEFAULT 'declared',
    confidence REAL NOT NULL DEFAULT 1.0,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS analysis_unit_context (
    context_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES analysis_unit(unit_id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    value TEXT NOT NULL,
    normalized_value TEXT NOT NULL DEFAULT '',
    ontology_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    source_field TEXT NOT NULL DEFAULT '',
    source_value TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL DEFAULT 'study-text',
    confidence REAL NOT NULL DEFAULT 0.6,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sample (
    sample_pk TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES analysis_unit(unit_id) ON DELETE CASCADE,
    sample_id TEXT NOT NULL,
    source_name TEXT NOT NULL DEFAULT '',
    raw_file TEXT NOT NULL DEFAULT '',
    attributes_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(unit_id, sample_id, raw_file)
);

CREATE TABLE IF NOT EXISTS sample_attribute (
    attribute_id TEXT PRIMARY KEY,
    sample_pk TEXT NOT NULL REFERENCES sample(sample_pk) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    normalized_field TEXT NOT NULL,
    raw_value TEXT NOT NULL DEFAULT '',
    normalized_value TEXT NOT NULL DEFAULT '',
    namespace TEXT NOT NULL DEFAULT 'repository',
    source TEXT NOT NULL DEFAULT 'repository',
    confidence REAL NOT NULL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS sample_context (
    context_id TEXT PRIMARY KEY,
    sample_pk TEXT NOT NULL REFERENCES sample(sample_pk) ON DELETE CASCADE,
    category TEXT NOT NULL,
    value TEXT NOT NULL,
    normalized_value TEXT NOT NULL DEFAULT '',
    ontology_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    source_field TEXT NOT NULL DEFAULT '',
    source_value TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL DEFAULT 'declared',
    confidence REAL NOT NULL DEFAULT 1.0,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS raw_file (
    file_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES analysis_unit(unit_id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'raw',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    checksum TEXT NOT NULL DEFAULT '',
    download_url TEXT NOT NULL DEFAULT '',
    sample_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS class_proposal (
    proposal_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES analysis_unit(unit_id) ON DELETE CASCADE,
    purpose TEXT NOT NULL,
    selected_fields_json TEXT NOT NULL DEFAULT '[]',
    rationale TEXT NOT NULL,
    contrast_definition_json TEXT NOT NULL DEFAULT '{}',
    model TEXT NOT NULL DEFAULT '',
    prompt_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'proposed',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS class_assignment (
    proposal_id TEXT NOT NULL REFERENCES class_proposal(proposal_id) ON DELETE CASCADE,
    sample_id TEXT NOT NULL,
    class_label TEXT NOT NULL,
    values_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(proposal_id, sample_id)
);

CREATE TABLE IF NOT EXISTS manual_override (
    override_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    old_value_json TEXT NOT NULL DEFAULT 'null',
    new_value_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    reviewer TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_run (
    run_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES analysis_unit(unit_id) ON DELETE CASCADE,
    class_proposal_id TEXT REFERENCES class_proposal(proposal_id) ON DELETE SET NULL,
    msdial_version TEXT NOT NULL,
    interactive_version TEXT NOT NULL DEFAULT '',
    catalog_schema_version INTEGER NOT NULL,
    parameter_hash TEXT NOT NULL,
    parameter_file TEXT NOT NULL DEFAULT '',
    mztab_path TEXT NOT NULL DEFAULT '',
    mztab_sha256 TEXT NOT NULL DEFAULT '',
    qa_status TEXT NOT NULL DEFAULT 'not_evaluated',
    started_at TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT '',
    provenance_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS contrast (
    contrast_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES analysis_run(run_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    case_definition_json TEXT NOT NULL,
    control_definition_json TEXT NOT NULL,
    covariates_json TEXT NOT NULL DEFAULT '[]',
    pairing_field TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'proposed'
);

CREATE TABLE IF NOT EXISTS metabolite_entity (
    metabolite_id TEXT PRIMARY KEY,
    preferred_name TEXT NOT NULL DEFAULT '',
    inchikey TEXT NOT NULL DEFAULT '',
    formula TEXT NOT NULL DEFAULT '',
    smiles TEXT NOT NULL DEFAULT '',
    lipid_class TEXT NOT NULL DEFAULT '',
    identifiers_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS metabolite_observation (
    observation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES analysis_run(run_id) ON DELETE CASCADE,
    feature_id TEXT NOT NULL,
    metabolite_id TEXT REFERENCES metabolite_entity(metabolite_id) ON DELETE SET NULL,
    name TEXT NOT NULL DEFAULT '',
    mz REAL,
    rt REAL,
    ccs REAL,
    adduct TEXT NOT NULL DEFAULT '',
    annotation_level TEXT NOT NULL DEFAULT '',
    annotation_confidence REAL,
    reference_matched INTEGER,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(run_id, feature_id)
);

CREATE TABLE IF NOT EXISTS metabolite_response (
    response_id TEXT PRIMARY KEY,
    contrast_id TEXT NOT NULL REFERENCES contrast(contrast_id) ON DELETE CASCADE,
    observation_id TEXT NOT NULL REFERENCES metabolite_observation(observation_id) ON DELETE CASCADE,
    direction TEXT NOT NULL DEFAULT 'unchanged',
    log2_fold_change REAL,
    effect_size REAL,
    p_value REAL,
    adjusted_p_value REAL,
    statistic_method TEXT NOT NULL DEFAULT '',
    case_n INTEGER,
    control_n INTEGER,
    evidence_level TEXT NOT NULL DEFAULT 'observational',
    details_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(contrast_id, observation_id)
);

CREATE INDEX IF NOT EXISTS idx_study_repository ON study(repository, accession);
CREATE INDEX IF NOT EXISTS idx_source_snapshot_study ON source_snapshot(study_id, retrieved_at);
CREATE INDEX IF NOT EXISTS idx_unit_filter ON analysis_unit(
    separation, chromatography, acquisition_mode, ion_mode, target_omics, review_status
);
CREATE INDEX IF NOT EXISTS idx_sample_unit ON sample(unit_id);
CREATE INDEX IF NOT EXISTS idx_raw_file_unit ON raw_file(unit_id);
CREATE INDEX IF NOT EXISTS idx_raw_file_download_url ON raw_file(download_url);
CREATE INDEX IF NOT EXISTS idx_sample_attribute_sample ON sample_attribute(sample_pk);
CREATE INDEX IF NOT EXISTS idx_sample_context_sample ON sample_context(sample_pk);
CREATE INDEX IF NOT EXISTS idx_unit_context_unit ON analysis_unit_context(unit_id);
CREATE INDEX IF NOT EXISTS idx_publication_study ON publication(study_id);
CREATE INDEX IF NOT EXISTS idx_attribute_field ON sample_attribute(normalized_field, normalized_value);
CREATE INDEX IF NOT EXISTS idx_context_category ON sample_context(category, normalized_value);
CREATE INDEX IF NOT EXISTS idx_unit_context_category ON analysis_unit_context(category, normalized_value);
CREATE INDEX IF NOT EXISTS idx_run_unit ON analysis_run(unit_id);
CREATE INDEX IF NOT EXISTS idx_contrast_run ON contrast(run_id);
CREATE INDEX IF NOT EXISTS idx_observation_identity ON metabolite_observation(metabolite_id, name);
CREATE INDEX IF NOT EXISTS idx_response_contrast ON metabolite_response(contrast_id, direction);
"""

FTS_SCHEMA = r"""
CREATE VIRTUAL TABLE IF NOT EXISTS study_fts USING fts5(
    study_id UNINDEXED,
    title,
    description,
    biological_context
);
"""

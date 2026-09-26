"""Migration 001: schema operacional local inicial."""
from __future__ import annotations

from . import Migration

SQL = r"""
CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_uri TEXT NOT NULL DEFAULT '',
    local_path TEXT,
    original_name TEXT,
    fingerprint TEXT,
    media_type TEXT NOT NULL DEFAULT 'video',
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    extra_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE videos (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_asset_id TEXT,
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    extra_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_asset_id) REFERENCES sources(id) ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    video_id TEXT,
    name TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    edit_state_json TEXT NOT NULL DEFAULT '{}',
    extra_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (video_id) REFERENCES videos(id) ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    external_account_id TEXT,
    local_key TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    extra_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    video_id TEXT,
    project_id TEXT,
    operation TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0.0 CHECK (progress >= 0.0 AND progress <= 1.0),
    input_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
    output_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
    extra_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (video_id) REFERENCES videos(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE templates (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    template_type TEXT NOT NULL DEFAULT 'CUSTOM',
    source_path TEXT,
    layout_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    extra_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    video_id TEXT,
    project_id TEXT,
    job_id TEXT,
    kind TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    fingerprint TEXT,
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    extra_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (video_id) REFERENCES videos(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE publications (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    video_id TEXT,
    account_id TEXT,
    artifact_id TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    remote_id TEXT,
    title TEXT,
    description TEXT,
    extra_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (video_id) REFERENCES videos(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE schedules (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    publication_id TEXT,
    scheduled_local TEXT,
    timezone_iana TEXT,
    scheduled_utc TEXT,
    time_origin TEXT NOT NULL CHECK (time_origin IN ('MANUAL', 'RECOMMENDED')),
    delivery_state TEXT NOT NULL CHECK (delivery_state IN ('LOCAL_PENDING', 'REMOTE_SCHEDULED', 'UNKNOWN')),
    extra_json TEXT NOT NULL DEFAULT '{}',
    CHECK ((scheduled_local IS NULL AND scheduled_utc IS NULL) OR timezone_iana IS NOT NULL),
    FOREIGN KEY (publication_id) REFERENCES publications(id) ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE errors (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    entity_type TEXT,
    entity_id TEXT,
    job_id TEXT,
    recoverable INTEGER NOT NULL DEFAULT 0 CHECK (recoverable IN (0, 1)),
    details_json TEXT NOT NULL DEFAULT '{}',
    extra_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE audit_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_videos_source_asset_id ON videos(source_asset_id);
CREATE INDEX idx_projects_video_id ON projects(video_id);
CREATE INDEX idx_accounts_platform ON accounts(platform);
CREATE INDEX idx_accounts_local_key ON accounts(local_key);
CREATE INDEX idx_jobs_video_id ON jobs(video_id);
CREATE INDEX idx_jobs_project_id ON jobs(project_id);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_updated_at ON jobs(updated_at);
CREATE INDEX idx_artifacts_video_id ON artifacts(video_id);
CREATE INDEX idx_artifacts_project_id ON artifacts(project_id);
CREATE INDEX idx_artifacts_job_id ON artifacts(job_id);
CREATE INDEX idx_publications_video_id ON publications(video_id);
CREATE INDEX idx_publications_account_id ON publications(account_id);
CREATE INDEX idx_publications_artifact_id ON publications(artifact_id);
CREATE INDEX idx_publications_status ON publications(status);
CREATE UNIQUE INDEX idx_schedules_publication_id ON schedules(publication_id) WHERE publication_id IS NOT NULL;
CREATE INDEX idx_schedules_scheduled_utc ON schedules(scheduled_utc);
CREATE INDEX idx_schedules_delivery_state ON schedules(delivery_state);
CREATE INDEX idx_errors_job_id ON errors(job_id);
CREATE INDEX idx_errors_code ON errors(code);
CREATE INDEX idx_audit_events_entity ON audit_events(entity_type, entity_id);
CREATE INDEX idx_audit_events_created_at ON audit_events(created_at);
""".strip()

MIGRATION = Migration(version=1, name="initial_local_schema", sql=SQL)

CREATE TABLE IF NOT EXISTS librarian_jobs (
  job_id TEXT PRIMARY KEY,
  source_key TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  model TEXT,
  thinking_level TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  latency_ms INTEGER,
  output_keys TEXT,
  error_code TEXT,
  analysis_payload TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id)
);

CREATE INDEX IF NOT EXISTS librarian_jobs_scope_created
ON librarian_jobs(tenant_id, client_id, project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS librarian_project_locks (
  project_key TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  lease_until TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_settings (
  google_sub TEXT PRIMARY KEY,
  timezone TEXT NOT NULL DEFAULT 'America/Santiago',
  updated_at TEXT NOT NULL,
  FOREIGN KEY(google_sub) REFERENCES users(google_sub)
);

CREATE TABLE IF NOT EXISTS commitments (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  client_id TEXT,
  project_id TEXT,
  owner_sub TEXT,
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK(status IN ('open','done','dropped')),
  week TEXT NOT NULL,
  due_on TEXT,
  origin TEXT NOT NULL CHECK(origin IN ('meeting','manual','mcp')),
  source_key TEXT,
  evidence TEXT,
  external_key TEXT,
  client_request_id TEXT,
  suggested_status TEXT,
  assignee_label TEXT,
  visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','project')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS commitments_external_key
ON commitments(tenant_id, external_key) WHERE external_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS commitments_request
ON commitments(owner_sub, client_request_id) WHERE client_request_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS commitments_tenant_status
ON commitments(tenant_id, status, client_id, project_id);

CREATE TABLE IF NOT EXISTS commitment_events (
  id TEXT PRIMARY KEY,
  commitment_id TEXT NOT NULL,
  actor_sub TEXT,
  from_status TEXT,
  to_status TEXT,
  from_week TEXT,
  to_week TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(commitment_id) REFERENCES commitments(id)
);

CREATE TABLE IF NOT EXISTS notes (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  author_sub TEXT NOT NULL,
  title TEXT,
  body TEXT NOT NULL,
  client_id TEXT,
  project_id TEXT,
  pinned INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL CHECK(source IN ('manual','mcp')),
  client_request_id TEXT,
  visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','project')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS notes_request
ON notes(author_sub, client_request_id) WHERE client_request_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS notes_author_tenant
ON notes(author_sub, tenant_id, updated_at DESC);

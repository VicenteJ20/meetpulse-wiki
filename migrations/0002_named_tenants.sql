-- Replace the one-owner/one-tenant schema with named, globally unique tenants.
-- The dependent tables are recreated so their foreign keys continue to point
-- at the replacement tenant table.

CREATE TABLE tenants_next (
  tenant_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  owner_google_sub TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(owner_google_sub) REFERENCES users(google_sub)
);

CREATE TABLE tenant_members_next (
  tenant_id TEXT NOT NULL,
  google_sub TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('owner','guest')),
  joined_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id,google_sub),
  FOREIGN KEY(tenant_id) REFERENCES tenants_next(tenant_id),
  FOREIGN KEY(google_sub) REFERENCES users(google_sub)
);

CREATE TABLE tenant_invitations_next (
  invitation_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  email TEXT NOT NULL,
  invited_by_google_sub TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','revoked')),
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  FOREIGN KEY(tenant_id) REFERENCES tenants_next(tenant_id)
);

INSERT INTO tenants_next(tenant_id, display_name, owner_google_sub, created_at)
SELECT tenant_id, tenant_id, owner_google_sub, created_at FROM tenants;
INSERT INTO tenant_members_next(tenant_id, google_sub, role, joined_at)
SELECT tenant_id, google_sub, role, joined_at FROM tenant_members;
INSERT INTO tenant_invitations_next(invitation_id, tenant_id, email, invited_by_google_sub, status, created_at, resolved_at)
SELECT invitation_id, tenant_id, email, invited_by_google_sub, status, created_at, resolved_at FROM tenant_invitations;

DROP TABLE tenant_invitations;
DROP TABLE tenant_members;
DROP TABLE tenants;
ALTER TABLE tenants_next RENAME TO tenants;
ALTER TABLE tenant_members_next RENAME TO tenant_members;
ALTER TABLE tenant_invitations_next RENAME TO tenant_invitations;
CREATE INDEX tenant_invitations_email_status ON tenant_invitations(email,status);

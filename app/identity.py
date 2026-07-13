from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

import httpx


@dataclass(frozen=True)
class User:
    google_sub: str
    email: str
    name: str | None = None


class IdentityStore(Protocol):
    def provision(self, user: User) -> dict[str, Any]: ...
    def tenants_for(self, user: User) -> list[dict[str, Any]]: ...
    def role_for(self, user: User, tenant_id: str) -> str | None: ...
    def invite(self, owner: User, tenant_id: str, email: str) -> dict[str, Any]: ...
    def invitations(self, owner: User, tenant_id: str) -> list[dict[str, Any]]: ...
    def pending(self, user: User) -> list[dict[str, Any]]: ...
    def respond(self, user: User, invitation_id: str, status: str) -> dict[str, Any]: ...
    def members(self, owner: User, tenant_id: str) -> list[dict[str, Any]]: ...
    def revoke(self, owner: User, tenant_id: str, member_sub: str) -> None: ...


class D1Store:
    def __init__(self, account_id: str, database_id: str, token: str) -> None:
        self.url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
        self.client = httpx.Client(timeout=15, headers={"Authorization": f"Bearer {token}"})

    def query(self, sql: str, params: list[Any] = []) -> list[dict[str, Any]]:
        response = self.client.post(self.url, json={"sql": sql, "params": params})
        response.raise_for_status(); data = response.json()
        if not data.get("success") or not data.get("result", [{}])[0].get("success", True): raise RuntimeError("D1 query failed")
        return data.get("result", [{}])[0].get("results", [])

    def user(self, user: User) -> None:
        self.query("INSERT INTO users(google_sub,email,name,created_at,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) ON CONFLICT(google_sub) DO UPDATE SET email=excluded.email,name=excluded.name,updated_at=CURRENT_TIMESTAMP", [user.google_sub, user.email.lower(), user.name])

    def provision(self, user: User) -> dict[str, Any]:
        self.user(user); existing = self.query("SELECT tenant_id FROM tenants WHERE owner_google_sub=?", [user.google_sub])
        if existing: return {"tenant_id": existing[0]["tenant_id"], "created": False}
        tenant_id = f"tenant_{uuid4().hex}"; self.query("INSERT INTO tenants(tenant_id,owner_google_sub,created_at) VALUES(?,?,CURRENT_TIMESTAMP)", [tenant_id, user.google_sub]); self.query("INSERT INTO tenant_members(tenant_id,google_sub,role,joined_at) VALUES(?,?, 'owner', CURRENT_TIMESTAMP)", [tenant_id, user.google_sub]); return {"tenant_id": tenant_id, "created": True}

    def tenants_for(self, user: User) -> list[dict[str, Any]]:
        self.user(user); return self.query("SELECT t.tenant_id, m.role, t.owner_google_sub FROM tenants t JOIN tenant_members m ON m.tenant_id=t.tenant_id WHERE m.google_sub=? ORDER BY t.created_at", [user.google_sub])
    def role_for(self, user: User, tenant_id: str) -> str | None:
        rows = self.query("SELECT role FROM tenant_members WHERE tenant_id=? AND google_sub=?", [tenant_id, user.google_sub]); return rows[0]["role"] if rows else None
    def invite(self, owner: User, tenant_id: str, email: str) -> dict[str, Any]:
        invitation_id = uuid4().hex; self.query("INSERT INTO tenant_invitations(invitation_id,tenant_id,email,invited_by_google_sub,status,created_at) VALUES(?,?,?,?, 'pending', CURRENT_TIMESTAMP)", [invitation_id, tenant_id, email.lower(), owner.google_sub]); return {"invitation_id": invitation_id, "email": email.lower(), "status": "pending"}
    def invitations(self, owner: User, tenant_id: str) -> list[dict[str, Any]]:
        return self.query("SELECT invitation_id,email,status,created_at,resolved_at FROM tenant_invitations WHERE tenant_id=? ORDER BY created_at DESC", [tenant_id])
    def pending(self, user: User) -> list[dict[str, Any]]:
        return self.query("SELECT i.invitation_id,i.tenant_id,i.email,i.created_at FROM tenant_invitations i WHERE i.email=? AND i.status='pending' ORDER BY i.created_at DESC", [user.email.lower()])
    def respond(self, user: User, invitation_id: str, status: str) -> dict[str, Any]:
        rows = self.query("SELECT tenant_id,email,status FROM tenant_invitations WHERE invitation_id=?", [invitation_id])
        if not rows or rows[0]["email"].lower() != user.email.lower() or rows[0]["status"] != "pending": raise ValueError("Invitation is not pending for this user")
        tenant_id = rows[0]["tenant_id"]; self.query("UPDATE tenant_invitations SET status=?,resolved_at=CURRENT_TIMESTAMP WHERE invitation_id=?", [status, invitation_id])
        if status == "accepted": self.user(user); self.query("INSERT INTO tenant_members(tenant_id,google_sub,role,joined_at) VALUES(?,?, 'guest', CURRENT_TIMESTAMP) ON CONFLICT(tenant_id,google_sub) DO NOTHING", [tenant_id, user.google_sub])
        return {"invitation_id": invitation_id, "tenant_id": tenant_id, "status": status}
    def members(self, owner: User, tenant_id: str) -> list[dict[str, Any]]:
        return self.query("SELECT u.google_sub,u.email,u.name,m.role,m.joined_at FROM tenant_members m JOIN users u ON u.google_sub=m.google_sub WHERE m.tenant_id=? ORDER BY CASE m.role WHEN 'owner' THEN 0 ELSE 1 END,u.email", [tenant_id])
    def revoke(self, owner: User, tenant_id: str, member_sub: str) -> None:
        self.query("DELETE FROM tenant_members WHERE tenant_id=? AND google_sub=? AND role='guest'", [tenant_id, member_sub])

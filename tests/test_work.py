from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
import json
import time

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.identity import User
from app.librarian import sign_librarian_body
from app.main import create_app
from app.service import format_instant
from app.work import MemoryWorkStore
from test_api import ANALYSIS, MemoryJobs, MemoryStorage, ingest


class Tokens:
    def verify(self, token: str) -> User:
        users = {
            "owner": User("owner", "owner@example.com"),
            "guest": User("guest", "guest@example.com"),
            "stranger": User("stranger", "stranger@example.com"),
        }
        if token not in users:
            raise HTTPException(status_code=401, detail="invalid token")
        return users[token]


class Roles:
    def role_for(self, user: User, tenant_id: str) -> str | None:
        return {("owner", "tenant_1"): "owner", ("guest", "tenant_1"): "guest"}.get((user.google_sub, tenant_id))


def client_for(work: MemoryWorkStore | None = None) -> tuple[TestClient, MemoryStorage, MemoryWorkStore]:
    storage = MemoryStorage({})
    work = work or MemoryWorkStore()
    client = TestClient(create_app(
        storage, identity=Roles(), verifier=Tokens(), jobs=MemoryJobs(), work=work,
        require_raw_source=False, librarian_secret="test-secret",
    ))
    client.headers["Authorization"] = "Bearer owner"
    return client, storage, work


def signed(client: TestClient, payload: dict) -> object:
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    return client.post(
        "/api/v1/internal/librarian/apply", content=body,
        headers={"content-type": "application/json", "x-librarian-timestamp": timestamp, "x-librarian-signature": sign_librarian_body(body, timestamp, "test-secret")},
    )


def librarian_payload(storage: MemoryStorage, created: dict, *, project_id: str, commitments: list[dict], decision: bool) -> dict:
    source_key = created["source_key"]
    context_key = f"wiki/tenant_1/client-1/{project_id}/context.md"
    documents = []
    if decision:
        documents.append({"type": "decision", "title": "Use R2", "description": "R2 stores Wiki data", "timestamp": "2026-07-12T19:30:00Z", "body": "# Decision\n\nUse R2.\n", "supersedes": [], "related": []})
    return {
        "job_id": created["job_id"], "tenant_id": "tenant_1", "client_id": "client-1", "project_id": project_id,
        "source_key": source_key, "expected_context_etag": storage.objects[context_key][1],
        "context": {"title": "Project context", "description": "Project status and pending work", "sources": [source_key], "timestamp": "2026-07-12T19:30:00Z", "body": "# Estado actual\n\nActivo.\n\n## Hitos\n\n- Revisión.\n\n## Pendientes\n\n- Seguimiento.\n"},
        "documents": documents, "commitments": commitments,
    }


def test_meeting_commitments_roll_up_by_client_and_can_be_cleared() -> None:
    client, storage, work = client_for()
    client.headers["Authorization"] = "Bearer owner"
    first = ingest(client)
    second = ingest(client, project_id="mobile-app", title="Mobile review")
    assert first.status_code == 201 and second.status_code == 201
    portal = librarian_payload(storage, first.json(), project_id="project_1", decision=True, commitments=[
        {"title": "Enviar la propuesta", "detail": "Incluir alcance", "evidence": "Bruno lo pide el viernes", "week": "", "due_on": "", "suggested_status": ""},
        {"title": "Revisar el contrato", "detail": "", "evidence": "", "week": "2026-W28", "due_on": "2026-07-17", "suggested_status": ""},
    ])
    mobile = librarian_payload(storage, second.json(), project_id="mobile-app", decision=False, commitments=[
        {"title": "Publicar la beta", "detail": "", "evidence": "Quedó para esta semana", "week": "", "due_on": "", "suggested_status": ""},
    ])
    assert signed(client, portal).status_code == 200
    assert signed(client, mobile).status_code == 200
    again = signed(client, {**portal, "expected_context_etag": storage.objects["wiki/tenant_1/client-1/project_1/context.md"][1]})
    assert again.status_code == 200
    assert len(work.commitments) == 3

    pending = client.get("/api/v1/tenants/tenant_1/pending", params={"client_id": "client-1"})
    assert pending.status_code == 200, pending.text
    body = pending.json()
    assert body["open_count"] == 3 and body["project_count"] == 2 and body["all_clear"] is False
    portal_row = next(item for item in body["clients"][0]["projects"] if item["project_id"] == "project_1")
    assert portal_row["open_count"] == 2 and portal_row["decision_count"] == 1
    assert portal_row["recent_decisions"][0]["title"] == "Use R2"
    assert portal_row["last_updated_at"] == "2026-07-12T19:30:00Z"

    guest = TestClient(client.app)
    guest.headers["Authorization"] = "Bearer guest"
    listed = guest.get("/api/v1/tenants/tenant_1/pending", params={"client_id": "client-1"}).json()
    ids = [item["id"] for project in listed["clients"][0]["projects"] for item in project["open_items"]]
    assert len(ids) == 3
    for commitment_id in ids:
        assert guest.patch(f"/api/v1/tenants/tenant_1/commitments/{commitment_id}", json={"status": "done"}).status_code == 200
        assert guest.patch(f"/api/v1/tenants/tenant_1/commitments/{commitment_id}", json={"status": "done"}).status_code == 200
    cleared = guest.get("/api/v1/tenants/tenant_1/pending", params={"client_id": "client-1"}).json()
    assert cleared["open_count"] == 0 and cleared["all_clear"] is True and cleared["done_this_week"] == 3
    assert sum(1 for event in work.events if event["to_status"] == "done") == 3


def test_manual_commitment_is_private_and_idempotent() -> None:
    client, _, _ = client_for()
    created = client.post("/api/v1/tenants/tenant_1/commitments", json={
        "title": "Llamar al cliente", "client_request_id": "request-1234", "week": "2020-W01",
    })
    assert created.status_code == 201, created.text
    replay = client.post("/api/v1/tenants/tenant_1/commitments", json={
        "title": "Llamar al cliente", "client_request_id": "request-1234", "week": "2020-W01",
    })
    assert replay.status_code == 201 and replay.json()["id"] == created.json()["id"]
    conflict = client.post("/api/v1/tenants/tenant_1/commitments", json={
        "title": "Otra cosa", "client_request_id": "request-1234", "week": "2020-W01",
    })
    assert conflict.status_code == 409
    guest = TestClient(client.app)
    guest.headers["Authorization"] = "Bearer guest"
    assert guest.patch(f"/api/v1/tenants/tenant_1/commitments/{created.json()['id']}", json={"status": "done"}).status_code == 404
    week = client.get("/api/v1/me/week", params={"tenant_id": "tenant_1", "week": "2026-W40"})
    assert week.status_code == 200
    assert week.json()["overdue_count"] == 1 and week.json()["all_clear"] is False
    assert client.get("/api/v1/tenants/tenant_1/pending", headers={"Authorization": "Bearer stranger"}).status_code == 403


def test_notes_can_be_unscoped_and_reject_unknown_projects() -> None:
    client, _, _ = client_for()
    assert ingest(client).status_code == 201
    loose = client.post("/api/v1/tenants/tenant_1/notes", json={"body": "Idea del día sin proyecto", "source": "mcp"})
    assert loose.status_code == 201, loose.text
    assert loose.json()["client_id"] is None and loose.json()["project_id"] is None
    missing = client.post("/api/v1/tenants/tenant_1/notes", json={"body": "No existe", "client_id": "client-1", "project_id": "missing"})
    assert missing.status_code == 422
    scoped = client.post("/api/v1/tenants/tenant_1/notes", json={"title": "Contrato", "body": "Cláusula de pago", "client_id": "client-1", "project_id": "project_1"})
    assert scoped.status_code == 201
    found = client.get("/api/v1/tenants/tenant_1/notes", params={"q": "pago"})
    assert found.json()["total"] == 1 and found.json()["items"][0]["id"] == scoped.json()["id"]
    assert client.delete(f"/api/v1/tenants/tenant_1/notes/{loose.json()['id']}").status_code == 204
    assert client.get(f"/api/v1/tenants/tenant_1/notes/{loose.json()['id']}").status_code == 404


def test_instants_without_a_zone_are_serialized_as_utc() -> None:
    naive = datetime(2026, 7, 12, 15, 30)
    aware = datetime(2026, 7, 12, 15, 30, tzinfo=timezone(timedelta(hours=-4)))
    assert format_instant(naive) == "2026-07-12T15:30:00Z"
    assert format_instant(aware) == "2026-07-12T19:30:00Z"
    assert format_instant(datetime(2026, 7, 12, 19, 30, tzinfo=UTC)) == "2026-07-12T19:30:00Z"

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import time

from fastapi.testclient import TestClient

from app.identity import User
from app.jobs import job_id_for_source
from app.librarian import sign_librarian_body
from app.main import create_app
from app.storage import ObjectNotFound, PreconditionFailed, StoredBytes, StoredObject


@dataclass
class MemoryStorage:
    objects: dict[str, tuple[str, str]]
    binary_objects: dict[str, tuple[bytes, str, str, str]] = field(default_factory=dict)
    sequence: int = 0

    def get_text(self, key: str) -> StoredObject:
        if key not in self.objects: raise ObjectNotFound(key)
        text, etag = self.objects[key]
        return StoredObject(text, etag)

    def get_bytes(self, key: str) -> StoredBytes:
        if key not in self.binary_objects: raise ObjectNotFound(key)
        data, etag, content_type, sha256_hex = self.binary_objects[key]
        return StoredBytes(data, etag, content_type, sha256_hex)

    def put_if_absent(self, key: str, text: str) -> None:
        if key in self.objects: raise PreconditionFailed(key)
        self.sequence += 1; self.objects[key] = (text, str(self.sequence))

    def put_if_match(self, key: str, text: str, etag: str) -> None:
        if key not in self.objects or self.objects[key][1] != etag: raise PreconditionFailed(key)
        self.sequence += 1; self.objects[key] = (text, str(self.sequence))

    def put_bytes_if_absent(self, key: str, data: bytes, *, content_type: str, sha256_hex: str) -> None:
        if key in self.binary_objects: raise PreconditionFailed(key)
        self.sequence += 1; self.binary_objects[key] = (data, str(self.sequence), content_type, sha256_hex)

    def list_keys(self, prefix: str) -> list[str]:
        return [key for key in (*self.objects, *self.binary_objects) if key.startswith(prefix)]


class TestVerifier:
    def verify(self, token: str) -> User:
        if token != "test-token": raise Exception("invalid token")
        return User(google_sub="owner", email="owner@example.com")


class OwnerIdentity:
    def role_for(self, user: User, tenant_id: str) -> str: return "owner"


class MemoryJobs:
    def __init__(self) -> None: self.items: dict[str, dict[str, object]] = {}
    def create_pending(self, *, job_id: str, source_key: str, tenant_id: str, client_id: str, project_id: str) -> None:
        self.items.setdefault(job_id, {"job_id": job_id, "source_key": source_key, "tenant_id": tenant_id, "client_id": client_id, "project_id": project_id, "status": "pending", "attempts": 0, "output_keys": []})
    def get(self, tenant_id: str, job_id: str):
        item = self.items.get(job_id); return item if item and item["tenant_id"] == tenant_id else None
    def list(self, tenant_id: str, *, client_id: str | None, project_id: str | None, limit: int):
        return [item for item in self.items.values() if item["tenant_id"] == tenant_id and (not client_id or item["client_id"] == client_id) and (not project_id or item["project_id"] == project_id)][:limit]


ANALYSIS = """---
custom: preserve
title: old
---

## Contexto y Estado Actual

El proyecto está activo.

## Decisiones Tomadas

No se tomaron decisiones formales.

## Compromisos y Próximos Pasos

- Revisar el siguiente hito.
"""


def api(*, require_raw: bool = False) -> tuple[TestClient, MemoryStorage]:
    storage = MemoryStorage({})
    client = TestClient(create_app(storage, identity=OwnerIdentity(), verifier=TestVerifier(), jobs=MemoryJobs(), require_raw_source=require_raw, librarian_secret="test-secret"))
    client.headers["Authorization"] = "Bearer test-token"
    return client, storage


def payload(title: str = "Reunión de diseño") -> dict[str, object]:
    return {"tenant_id": "tenant_1", "client_id": "client-1", "project_id": "project_1", "title": title, "date_time": "2026-07-12T15:30:00-04:00", "participants": ["Ana", "Bruno"]}


def ingest(client: TestClient, raw: bytes | None = None, **extra: object):
    data = payload(); data.update(extra)
    files: dict[str, tuple[str, object, str]] = {"file": ("meeting.md", ANALYSIS, "text/markdown")}
    if raw is not None: files["raw_file"] = ("transcript.txt", raw, "text/plain")
    return client.post("/api/v1/ingest", data=data, files=files)


def context_markdown(source_key: str, **overrides: str) -> str:
    metadata = {"type": "context", "title": "Project context", "description": "Current state after the design meeting.", "sources": f"\n  - {source_key}", "timestamp": "2026-07-12T19:30:00Z"}
    metadata.update(overrides)
    return "---\n" + "\n".join(f"{key}: {value}" for key, value in metadata.items()) + "\n---\n\n# Extra context\n"


def test_ingest_creates_analysis_wiki_job_and_log() -> None:
    client, storage = api(); response = ingest(client)
    assert response.status_code == 201, response.text
    source_key = "sources/tenant_1/client-1/project_1/2026-07-12-reunion-de-diseno.md"
    assert response.json() | {"updated_keys": response.json()["updated_keys"]} == {
        "source_key": source_key, "raw_key": None, "provenance_status": "analysis_only",
        "ingest_status": "created", "job_id": job_id_for_source(source_key),
        "processing_status": "pending", "updated_keys": response.json()["updated_keys"],
    }
    source = storage.objects[source_key][0]
    assert "custom: preserve" in source and "title: Reunión de diseño" in source and "title: old" not in source
    assert "source_kind: meeting_analysis" in source and "provenance_status: analysis_only" in source
    context = storage.objects["wiki/tenant_1/client-1/project_1/context.md"][0]
    assert "type: context" in context and f"- {source_key}" in context
    assert source_key in storage.objects["wiki/tenant_1/client-1/project_1/index.md"][0]
    assert "ingest:" in storage.objects["wiki/tenant_1/log.md"][0]


def test_identical_ingest_is_idempotent_but_changed_analysis_conflicts() -> None:
    client, storage = api(); assert ingest(client).status_code == 201
    key = "sources/tenant_1/client-1/project_1/2026-07-12-reunion-de-diseno.md"
    original_source, original_log = storage.objects[key], storage.objects["wiki/tenant_1/log.md"]
    retry = ingest(client)
    assert retry.status_code == 201 and retry.json()["ingest_status"] == "unchanged"
    assert retry.json()["updated_keys"] == []
    assert storage.objects[key] == original_source and storage.objects["wiki/tenant_1/log.md"] == original_log

    changed = ANALYSIS.replace("activo.", "cambiÃ³ sin una nueva identidad de fuente.")
    data = payload(); files = {"file": ("meeting.md", changed, "text/markdown")}
    assert client.post("/api/v1/ingest", data=data, files=files).status_code == 409
    assert storage.objects[key] == original_source


def test_retry_reconciles_missing_raw_and_triggers_source_update() -> None:
    client, storage = api(); first = ingest(client); assert first.status_code == 201
    source_key = first.json()["source_key"]
    original_context = storage.objects["wiki/tenant_1/client-1/project_1/context.md"]
    raw = "Speaker A: reuniÃ³n completa.\n".encode()
    orphan_key = "raw/tenant_1/client-1/project_1/2026-07-12-reunion-de-diseno/transcript.txt"
    storage.put_bytes_if_absent(orphan_key, raw, content_type="text/plain", sha256_hex=sha256(raw).hexdigest())

    reconciled = ingest(client, raw=raw)
    assert reconciled.status_code == 201 and reconciled.json()["ingest_status"] == "reconciled"
    assert reconciled.json()["provenance_status"] == "complete"
    assert reconciled.json()["raw_key"] in storage.binary_objects
    assert "provenance_status: complete" in storage.objects[source_key][0]
    assert "provenance:" in storage.objects["wiki/tenant_1/log.md"][0]
    assert storage.objects["wiki/tenant_1/client-1/project_1/context.md"] == original_context

    source_after_reconciliation = storage.objects[source_key]
    identical = ingest(client, raw=raw)
    assert identical.status_code == 201 and identical.json()["ingest_status"] == "unchanged"
    assert storage.objects[source_key] == source_after_reconciliation


def test_retry_upgrades_historical_source_without_phase_two_metadata() -> None:
    client, storage = api(); first = ingest(client); assert first.status_code == 201
    source_key = first.json()["source_key"]
    current, etag = storage.objects[source_key]
    historical = current
    for field in (
        "source_kind: meeting_analysis\n", "analysis_schema_version: '1'\n",
        "raw_sources: []\n", "raw_sha256: []\n", "provenance_status: analysis_only\n",
    ):
        historical = historical.replace(field, "")
    storage.objects[source_key] = (historical, etag)

    response = ingest(client, raw=b"historical transcript")
    assert response.status_code == 201 and response.json()["ingest_status"] == "reconciled"
    upgraded = storage.objects[source_key][0]
    assert "source_kind: meeting_analysis" in upgraded and "provenance_status: complete" in upgraded


def test_ingest_preserves_raw_bytes_and_provenance() -> None:
    client, storage = api(); raw = "Speaker A: reunión sin transformar.\n".encode()
    response = ingest(client, raw=raw); assert response.status_code == 201, response.text
    raw_key = "raw/tenant_1/client-1/project_1/2026-07-12-reunion-de-diseno/transcript.txt"
    assert response.json()["raw_key"] == raw_key and response.json()["provenance_status"] == "complete"
    assert storage.binary_objects[raw_key][0] == raw and storage.binary_objects[raw_key][3] == sha256(raw).hexdigest()
    source = storage.objects[response.json()["source_key"]][0]
    assert raw_key in source and sha256(raw).hexdigest() in source


def test_raw_required_and_conflicting_raw_are_rejected() -> None:
    client, _ = api(require_raw=True)
    assert ingest(client).status_code == 422
    assert ingest(client, raw=b"original").status_code == 201
    assert ingest(client, raw=b"different").status_code == 409


def test_analysis_contract_and_tree_validation() -> None:
    client, _ = api()
    data = payload(); files = {"file": ("meeting.md", "## Notes\n\nNo structure", "text/markdown")}
    assert client.post("/api/v1/ingest", data=data, files=files).status_code == 422
    assert ingest(client, tenant_id="not valid").status_code == 422
    assert ingest(client).status_code == 201
    assert client.get("/api/v1/tree/tenant_1").json()["children"] == [{"name": "client-1", "href": "client-1/index.md"}]
    assert client.get("/api/v1/tree/tenant_1", params={"project_id": "project_1"}).status_code == 422


def test_dashboard_and_document_reading() -> None:
    client, _ = api(); assert ingest(client).status_code == 201
    summary = client.get("/api/v1/dashboard/tenant_1/summary").json()
    assert summary["source_count"] == 1 and summary["wiki_page_count"] == 2
    documents = client.get("/api/v1/wiki/tenant_1/documents", params={"client_id": "client-1", "project_id": "project_1"}).json()["items"]
    assert {item["document"] for item in documents} == {"analysis:2026-07-12-reunion-de-diseno", "context"}
    read = client.get("/api/v1/wiki/tenant_1/documents/analysis:2026-07-12-reunion-de-diseno", params={"client_id": "client-1", "project_id": "project_1"})
    assert read.status_code == 200 and read.json()["content_markdown"].startswith("## Contexto y Estado Actual")
    assert read.json()["body_markdown"] == read.json()["content_markdown"]
    assert read.json()["type"] == "meeting" and read.json()["source_kind"] == "meeting_analysis" and read.json()["raw_available"] is False

    context = client.get("/api/v1/wiki/tenant_1/documents/context", params={"client_id": "client-1", "project_id": "project_1"}).json()
    assert context["content_markdown"].startswith("---\n")
    assert context["body_markdown"] == ""
    assert context["metadata"]["type"] == "context"


def test_context_okf_validation_and_update() -> None:
    client, _ = api(); assert ingest(client).status_code == 201
    endpoint = "/api/v1/wiki/tenant_1/documents/context"; params = {"client_id": "client-1", "project_id": "project_1"}
    source_key = "sources/tenant_1/client-1/project_1/2026-07-12-reunion-de-diseno.md"
    expected = context_markdown(source_key)
    updated = client.put(endpoint, params=params, json={"content_markdown": expected}).json()
    assert updated["content_markdown"] == expected and updated["body_markdown"] == "# Extra context\n"
    invalid = ["# no front matter\n", "---\ntype: context\n", context_markdown(source_key, type="unknown"), context_markdown(source_key, sources="[]"), context_markdown(source_key, timestamp="2026-07-12T19:30:00-04:00"), context_markdown("sources/tenant_1/client-1/project_1/missing.md")]
    for document in invalid: assert client.put(endpoint, params=params, json={"content_markdown": document}).status_code == 422


def test_second_ingest_does_not_replace_context() -> None:
    client, storage = api(); assert ingest(client).status_code == 201
    key = "wiki/tenant_1/client-1/project_1/context.md"; original = storage.objects[key]
    assert ingest(client, title="Second meeting").status_code == 201 and storage.objects[key] == original


def test_jobs_are_visible() -> None:
    storage = MemoryStorage({}); jobs = MemoryJobs()
    client = TestClient(create_app(storage, identity=OwnerIdentity(), verifier=TestVerifier(), jobs=jobs, require_raw_source=False, librarian_secret="test-secret"), headers={"Authorization": "Bearer test-token"})
    created = ingest(client); job_id = created.json()["job_id"]
    assert client.get(f"/api/v1/jobs/tenant_1/{job_id}").json()["status"] == "pending"
    assert client.get("/api/v1/jobs/tenant_1", params={"client_id": "client-1", "project_id": "project_1"}).json()["items"][0]["job_id"] == job_id


def test_signed_librarian_apply_creates_atomic_documents() -> None:
    client, storage = api(); created = ingest(client); source_key = created.json()["source_key"]
    context_key = "wiki/tenant_1/client-1/project_1/context.md"
    payload = {
        "job_id": created.json()["job_id"], "tenant_id": "tenant_1", "client_id": "client-1", "project_id": "project_1", "source_key": source_key,
        "expected_context_etag": storage.objects[context_key][1],
        "context": {"title": "Project context", "description": "Project status and pending work", "sources": [source_key], "timestamp": "2026-07-12T19:30:00Z", "body": "# Estado actual\n\nActivo.\n\n## Hitos\n\n- Revisión completada.\n\n## Pendientes\n\n- Seguimiento.\n"},
        "documents": [{"type": "decision", "title": "Use R2", "description": "R2 stores Wiki data", "timestamp": "2026-07-12T19:30:00Z", "body": "# Decision\n\nUse R2.\n", "supersedes": [], "related": []}],
    }
    body = json.dumps(payload, separators=(",", ":")).encode(); timestamp = str(int(time.time()))
    response = client.post("/api/v1/internal/librarian/apply", content=body, headers={"content-type": "application/json", "x-librarian-timestamp": timestamp, "x-librarian-signature": sign_librarian_body(body, timestamp, "test-secret")})
    assert response.status_code == 200, response.text
    assert any("/decisions/" in key for key in response.json()["output_keys"])
    assert "# Estado actual" in storage.objects[context_key][0]
    listed = client.get("/api/v1/wiki/tenant_1/documents", params={"client_id": "client-1", "project_id": "project_1"}).json()["items"]
    assert any(item.get("type") == "decision" for item in listed)


def test_librarian_signature_is_required() -> None:
    client, _ = api()
    assert client.post("/api/v1/internal/librarian/apply", json={}).status_code == 401


def test_maintenance_sorts_logs_enriches_indexes_and_is_idempotent() -> None:
    client, storage = api(); assert ingest(client).status_code == 201
    payload = {"tenant_id": "tenant_1"}
    body = json.dumps(payload, separators=(",", ":")).encode(); timestamp = str(int(time.time()))
    headers = {"content-type": "application/json", "x-librarian-timestamp": timestamp, "x-librarian-signature": sign_librarian_body(body, timestamp, "test-secret")}
    first = client.post("/api/v1/internal/librarian/maintenance", content=body, headers=headers)
    assert first.status_code == 200, first.text
    assert "## 20" in storage.objects["wiki/tenant_1/log.md"][0]
    assert "## Reuniones analizadas" in storage.objects["wiki/tenant_1/client-1/project_1/index.md"][0]
    assert " — " in storage.objects["wiki/tenant_1/client-1/index.md"][0]
    second_timestamp = str(int(time.time()))
    second_headers = {"content-type": "application/json", "x-librarian-timestamp": second_timestamp, "x-librarian-signature": sign_librarian_body(body, second_timestamp, "test-secret")}
    second = client.post("/api/v1/internal/librarian/maintenance", content=body, headers=second_headers)
    assert second.status_code == 200 and second.json()["updated_keys"] == []

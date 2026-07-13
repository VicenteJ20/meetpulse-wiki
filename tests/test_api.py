from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient

from app.main import create_app
from app.storage import ObjectNotFound, PreconditionFailed, StoredObject


@dataclass
class MemoryStorage:
    objects: dict[str, tuple[str, str]]
    sequence: int = 0

    def get_text(self, key: str) -> StoredObject:
        if key not in self.objects:
            raise ObjectNotFound(key)
        text, etag = self.objects[key]
        return StoredObject(text, etag)

    def put_if_absent(self, key: str, text: str) -> None:
        if key in self.objects:
            raise PreconditionFailed(key)
        self.sequence += 1
        self.objects[key] = (text, str(self.sequence))

    def put_if_match(self, key: str, text: str, etag: str) -> None:
        if key not in self.objects or self.objects[key][1] != etag:
            raise PreconditionFailed(key)
        self.sequence += 1
        self.objects[key] = (text, str(self.sequence))

    def list_keys(self, prefix: str) -> list[str]:
        return [key for key in self.objects if key.startswith(prefix)]


def api() -> tuple[TestClient, MemoryStorage]:
    storage = MemoryStorage({})
    return TestClient(create_app(storage)), storage


def payload(title: str = "Reunión de diseño") -> dict[str, object]:
    return {
        "tenant_id": "tenant_1",
        "client_id": "client-1",
        "project_id": "project_1",
        "title": title,
        "date_time": "2026-07-12T15:30:00-04:00",
        "participants": ["Ana", "Bruno"],
    }


def ingest(client: TestClient, **extra: object):
    data = payload()
    data.update(extra)
    return client.post(
        "/api/v1/ingest",
        data=data,
        files={"file": ("meeting.md", "---\ncustom: preserve\ntitle: old\n---\n\n# Notes\n", "text/markdown")},
    )


def test_ingest_creates_source_wiki_and_log() -> None:
    client, storage = api()
    response = ingest(client)
    assert response.status_code == 201
    source_key = "sources/tenant_1/client-1/project_1/2026-07-12-reunion-de-diseno.md"
    assert response.json()["source_key"] == source_key
    source, _ = storage.objects[source_key]
    assert "custom: preserve" in source
    assert "title: Reunión de diseño" in source
    assert "title: old" not in source
    assert "# Notes" in source
    assert "wiki/tenant_1/client-1/project_1/context.md" in storage.objects
    assert not any(key.endswith(("acuerdos.md", "arquitectura.md", "conceptos.md", "participantes.md", "riesgos.md")) for key in storage.objects)
    assert source_key in storage.objects["wiki/tenant_1/client-1/project_1/index.md"][0]
    assert "ingest:" in storage.objects["wiki/tenant_1/log.md"][0]


def test_ingest_collision_is_immutable() -> None:
    client, storage = api()
    assert ingest(client).status_code == 201
    original = storage.objects["sources/tenant_1/client-1/project_1/2026-07-12-reunion-de-diseno.md"]
    response = ingest(client)
    assert response.status_code == 409
    assert storage.objects["sources/tenant_1/client-1/project_1/2026-07-12-reunion-de-diseno.md"] == original


def test_validation_and_tree_levels() -> None:
    client, _ = api()
    assert ingest(client, tenant_id="not valid").status_code == 422
    response = ingest(client)
    assert response.status_code == 201
    tenant = client.get("/api/v1/tree/tenant_1")
    assert tenant.status_code == 200
    assert tenant.json()["children"] == [{"name": "client-1", "href": "client-1/index.md"}]
    project = client.get("/api/v1/tree/tenant_1", params={"client_id": "client-1", "project_id": "project_1"})
    assert project.status_code == 200
    assert len(project.json()["children"]) == 1
    assert client.get("/api/v1/tree/tenant_1", params={"project_id": "project_1"}).status_code == 422


def test_file_and_logs_validation() -> None:
    client, _ = api()
    data = payload()
    not_markdown = client.post("/api/v1/ingest", data=data, files={"file": ("meeting.txt", "notes")})
    assert not_markdown.status_code == 422
    assert client.get("/api/v1/logs/tenant_1").status_code == 404
    assert ingest(client).status_code == 201
    logs = client.get("/api/v1/logs/tenant_1", params={"limit": 1})
    assert logs.status_code == 200
    assert len(logs.json()["entries"]) == 1


def test_dashboard_and_wiki_document_reading() -> None:
    client, _ = api()
    assert ingest(client).status_code == 201

    summary = client.get("/api/v1/dashboard/tenant_1/summary")
    assert summary.status_code == 200
    assert summary.json() | {"last_activity_at": summary.json()["last_activity_at"]} == {
        "tenant_id": "tenant_1", "client_count": 1, "project_count": 1,
        "source_count": 1, "wiki_page_count": 2, "last_activity_at": summary.json()["last_activity_at"],
    }
    assert summary.json()["last_activity_at"]

    clients = client.get("/api/v1/dashboard/tenant_1/clients")
    assert clients.json()["items"][0]["client_id"] == "client-1"
    assert clients.json()["items"][0]["project_count"] == 1
    projects = client.get("/api/v1/dashboard/tenant_1/clients/client-1/projects")
    assert projects.json()["items"][0]["project_id"] == "project_1"
    assert projects.json()["items"][0]["wiki_page_count"] == 2

    documents = client.get("/api/v1/wiki/tenant_1/documents", params={"client_id": "client-1", "project_id": "project_1"})
    assert documents.status_code == 200
    assert {item["document"] for item in documents.json()["items"]} == {"analysis:2026-07-12-reunion-de-diseno", "context"}
    read = client.get("/api/v1/wiki/tenant_1/documents/analysis:2026-07-12-reunion-de-diseno", params={"client_id": "client-1", "project_id": "project_1"})
    assert read.status_code == 200
    assert read.json()["content_markdown"].startswith("# Notes")
    assert "tenant_id:" not in read.json()["content_markdown"]
    context = client.put("/api/v1/wiki/tenant_1/documents/context", params={"client_id": "client-1", "project_id": "project_1"}, json={"content_markdown": "# Extra context\n"})
    assert context.status_code == 200
    assert context.json()["content_markdown"] == "# Extra context\n"
    assert client.get("/api/v1/wiki/tenant_1/documents/unknown", params={"client_id": "client-1", "project_id": "project_1"}).status_code == 422

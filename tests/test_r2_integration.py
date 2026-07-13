"""Opt-in integration test against a real Cloudflare R2 bucket.

Run only after setting RUN_R2_INTEGRATION=1 and configuring .env.
Every object created by this test belongs to a random r2test* tenant and is
deleted in teardown. It never lists, reads, or deletes any other tenant.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.storage import R2Storage


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def r2_storage() -> R2Storage:
    if os.getenv("RUN_R2_INTEGRATION") != "1":
        pytest.skip("set RUN_R2_INTEGRATION=1 to enable real R2 integration tests")
    return R2Storage.from_environment()


@pytest.fixture(scope="module")
def tenant_id(r2_storage: R2Storage):
    tenant = f"r2test{uuid.uuid4().hex[:12]}"
    yield tenant
    # Cleanup remains strictly inside the random test tenant namespace.
    prefixes = (f"sources/{tenant}/", f"wiki/{tenant}/")
    for prefix in prefixes:
        keys = r2_storage.list_keys(prefix)
        if keys:
            r2_storage.client.delete_objects(
                Bucket=r2_storage.bucket,
                Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
            )


@pytest.fixture
def api_client(r2_storage: R2Storage):
    """Use a running Uvicorn server when API_BASE_URL is supplied."""
    base_url = os.getenv("API_BASE_URL")
    if base_url:
        with httpx.Client(base_url=base_url, timeout=30) as client:
            yield client
    else:
        yield TestClient(create_app(r2_storage))


def test_real_r2_ingest_tree_logs_and_collision(api_client, r2_storage: R2Storage, tenant_id: str) -> None:
    data = {
        "tenant_id": tenant_id,
        "client_id": "integration-client",
        "project_id": "r2-check",
        "title": "Validación real R2",
        "date_time": "2026-07-12T15:30:00-04:00",
        "participants": ["MeetPulse QA", "R2"],
    }
    files = {"file": ("transcript.md", "---\nsource: integration-test\n---\n\n# Prueba real\n", "text/markdown")}

    created = api_client.post("/api/v1/ingest", data=data, files=files)
    assert created.status_code == 201, created.text
    source_key = created.json()["source_key"]
    assert source_key == f"sources/{tenant_id}/integration-client/r2-check/2026-07-12-validacion-real-r2.md"

    source = r2_storage.get_text(source_key).text
    assert "source: integration-test" in source
    assert f"tenant_id: {tenant_id}" in source
    assert "participants:" in source

    project = api_client.get(
        f"/api/v1/tree/{tenant_id}",
        params={"client_id": "integration-client", "project_id": "r2-check"},
    )
    assert project.status_code == 200, project.text
    assert any(child["href"] == f"/{source_key}" for child in project.json()["children"])

    logs = api_client.get(f"/api/v1/logs/{tenant_id}", params={"limit": 1})
    assert logs.status_code == 200, logs.text
    assert len(logs.json()["entries"]) == 1
    assert source_key in logs.json()["entries"][0]

    summary = api_client.get(f"/api/v1/dashboard/{tenant_id}/summary")
    assert summary.status_code == 200, summary.text
    assert summary.json()["client_count"] == 1
    assert summary.json()["project_count"] == 1
    assert summary.json()["source_count"] == 1
    assert summary.json()["wiki_page_count"] == 2

    clients = api_client.get(f"/api/v1/dashboard/{tenant_id}/clients")
    assert clients.status_code == 200, clients.text
    assert clients.json()["items"][0]["client_id"] == "integration-client"
    projects = api_client.get(f"/api/v1/dashboard/{tenant_id}/clients/integration-client/projects")
    assert projects.status_code == 200, projects.text
    assert projects.json()["items"][0]["project_id"] == "r2-check"

    documents = api_client.get(f"/api/v1/wiki/{tenant_id}/documents", params={"client_id": "integration-client", "project_id": "r2-check"})
    assert documents.status_code == 200, documents.text
    assert len(documents.json()["items"]) == 2
    document = api_client.get(f"/api/v1/wiki/{tenant_id}/documents/analysis:2026-07-12-validacion-real-r2", params={"client_id": "integration-client", "project_id": "r2-check"})
    assert document.status_code == 200, document.text
    assert document.json()["content_markdown"].startswith("# Prueba real")

    collision = api_client.post("/api/v1/ingest", data=data, files=files)
    assert collision.status_code == 409, collision.text

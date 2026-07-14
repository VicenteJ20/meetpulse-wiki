from fastapi.testclient import TestClient

from app.main import create_app


def test_wiki_get_preflight_allows_meetpulse_development_origin() -> None:
    client = TestClient(create_app())

    response = client.options(
        "/api/v1/dashboard/tenant_1/summary",
        headers={
            "Origin": "http://127.0.0.1:3118",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:3118"
    assert "GET" in response.headers["access-control-allow-methods"]


def test_wiki_ingest_preflight_allows_post() -> None:
    client = TestClient(create_app())

    response = client.options(
        "/api/v1/ingest",
        headers={
            "Origin": "http://127.0.0.1:3118",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert "POST" in response.headers["access-control-allow-methods"]


def test_preflight_allows_configured_production_origin(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://meetpulse-web.vercel.app")
    client = TestClient(create_app())

    response = client.options(
        "/api/v1/dashboard/tenant_1/summary",
        headers={
            "Origin": "https://meetpulse-web.vercel.app",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://meetpulse-web.vercel.app"

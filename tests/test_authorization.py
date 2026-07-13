from fastapi.testclient import TestClient

from app.identity import User
from app.main import create_app


class Verifier:
    def verify(self, token: str) -> User:
        if token != "valid":
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Invalid Google ID token")
        return User(google_sub="guest", email="guest@example.com")


class Identity:
    def role_for(self, user: User, tenant_id: str) -> str | None:
        return "guest" if tenant_id == "shared" else None


def test_api_requires_bearer_token() -> None:
    client = TestClient(create_app(identity=Identity(), verifier=Verifier()))
    assert client.get("/api/v1/dashboard/shared/summary").status_code == 401


def test_guest_cannot_access_another_tenant() -> None:
    client = TestClient(create_app(identity=Identity(), verifier=Verifier()), headers={"Authorization": "Bearer valid"})
    assert client.get("/api/v1/dashboard/private/summary").status_code == 403

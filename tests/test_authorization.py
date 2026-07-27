from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from app.auth import GoogleTokenVerifier
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


def test_google_verifier_accepts_configured_audience_list(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = GoogleTokenVerifier(["meetpulse-client", "clara-client"])
    verifier.jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _token: SimpleNamespace(key="public-key"))
    captured: dict[str, object] = {}

    def decode(_token: str, _key: str, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"sub": "google-user", "email": "user@example.com", "email_verified": True}

    monkeypatch.setattr("app.auth.jwt.decode", decode)
    user = verifier.verify("token")

    assert captured["audience"] == ["meetpulse-client", "clara-client"]
    assert user.google_sub == "google-user"


def test_google_verifier_rejects_unverified_email(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = GoogleTokenVerifier("meetpulse-client")
    verifier.jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _token: SimpleNamespace(key="public-key"))
    monkeypatch.setattr(
        "app.auth.jwt.decode",
        lambda *_args, **_kwargs: {"sub": "google-user", "email": "user@example.com", "email_verified": False},
    )

    with pytest.raises(Exception) as exc:
        verifier.verify("token")
    assert getattr(exc.value, "status_code", None) == 401

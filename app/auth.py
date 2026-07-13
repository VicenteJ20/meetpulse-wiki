from __future__ import annotations
from typing import Any
import jwt
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from app.identity import User

bearer = HTTPBearer(auto_error=False)
class GoogleTokenVerifier:
    def __init__(self, audience: str) -> None: self.audience, self.jwks = audience, PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")
    def verify(self, token: str) -> User:
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims: dict[str, Any] = jwt.decode(token, key, algorithms=["RS256"], audience=self.audience, issuer=["https://accounts.google.com", "accounts.google.com"])
        except Exception as exc: raise HTTPException(status_code=401, detail="Invalid Google ID token") from exc
        if claims.get("email_verified") is not True: raise HTTPException(status_code=401, detail="Google email must be verified")
        return User(google_sub=claims["sub"], email=claims["email"], name=claims.get("name"))

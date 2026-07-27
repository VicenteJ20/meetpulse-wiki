from __future__ import annotations
import logging
from typing import Any, Sequence
import jwt
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from app.identity import User

bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)

class GoogleTokenVerifier:
    def __init__(self, audiences: str | Sequence[str]) -> None:
        self.audiences = [audiences] if isinstance(audiences, str) else list(audiences)
        if not self.audiences:
            raise ValueError("At least one Google OAuth client ID must be configured")
        self.jwks = PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")
    def verify(self, token: str) -> User:
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims: dict[str, Any] = jwt.decode(token, key, algorithms=["RS256"], audience=self.audiences, issuer=["https://accounts.google.com", "accounts.google.com"])
        except Exception as exc:
            # Do not log credentials or claims. This is enough to distinguish
            # audience, expiry, signature and malformed-token failures.
            logger.warning("Rejected Google ID token (%s): %s", type(exc).__name__, exc)
            raise HTTPException(status_code=401, detail="Invalid Google ID token") from exc
        if claims.get("email_verified") is not True: raise HTTPException(status_code=401, detail="Google email must be verified")
        return User(google_sub=claims["sub"], email=claims["email"], name=claims.get("name"))

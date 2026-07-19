from __future__ import annotations

from hashlib import sha256
import hmac
import time


class InvalidLibrarianSignature(ValueError):
    pass


def verify_librarian_signature(*, body: bytes, timestamp: str | None, signature: str | None, secret: str, now: int | None = None) -> None:
    if not secret:
        raise InvalidLibrarianSignature("librarian webhook secret is not configured")
    if not timestamp or not signature:
        raise InvalidLibrarianSignature("librarian signature headers are required")
    try:
        signed_at = int(timestamp)
    except ValueError as exc:
        raise InvalidLibrarianSignature("invalid librarian timestamp") from exc
    current = int(time.time()) if now is None else now
    if abs(current - signed_at) > 300:
        raise InvalidLibrarianSignature("librarian signature has expired")
    expected = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise InvalidLibrarianSignature("invalid librarian signature")


def sign_librarian_body(body: bytes, timestamp: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, sha256).hexdigest()

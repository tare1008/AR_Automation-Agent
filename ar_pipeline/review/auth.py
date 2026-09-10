"""Shared-secret auth for the review UI, behind a swappable ``AuthProvider``.

The demo provider is a single shared password plus a free-text reviewer
name; the name rides in a signed, timestamped cookie and is what lands in
``extraction.reviewed_by`` / ``extraction_edit.edited_by``. An Entra ID
OIDC provider can implement the same protocol later without touching routes.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Protocol

from fastapi import HTTPException, Request
from itsdangerous import BadData, URLSafeTimedSerializer

from ar_pipeline.config import get_settings

COOKIE_NAME = "ar_review_session"
_SALT = "ar-review-session-v1"
_DEFAULT_MAX_AGE = 7 * 24 * 60 * 60
_DEV_SESSION_SECRET = "dev-insecure-session-key"  # keep in sync with config.py default


@dataclass(frozen=True)
class User:
    name: str


class AuthProvider(Protocol):
    def check_password(self, password: str) -> bool: ...
    def issue_session(self, name: str) -> str: ...
    def load_session(self, token: str | None) -> User | None: ...


class SharedSecretAuth:
    def __init__(
        self,
        *,
        shared_secret: str,
        signing_key: str,
        max_age_seconds: int = _DEFAULT_MAX_AGE,
    ) -> None:
        self._secret = shared_secret
        self._max_age = max_age_seconds
        self._serializer = URLSafeTimedSerializer(signing_key, salt=_SALT)

    def check_password(self, password: str) -> bool:
        if not self._secret or not password:
            return False
        return hmac.compare_digest(password.encode("utf-8"), self._secret.encode("utf-8"))

    def issue_session(self, name: str) -> str:
        return self._serializer.dumps({"name": name})

    def load_session(self, token: str | None) -> User | None:
        if not token:
            return None
        try:
            data = self._serializer.loads(token, max_age=self._max_age)
        except BadData:
            return None
        name = data.get("name") if isinstance(data, dict) else None
        if not isinstance(name, str) or not name:
            return None
        return User(name=name)


def get_auth_provider() -> AuthProvider:
    s = get_settings()
    secret = s.review_auth_secret.get_secret_value()
    if not secret:
        raise RuntimeError("review_auth_secret is not set")
    session_key = s.review_session_secret.get_secret_value()
    if not session_key or session_key == _DEV_SESSION_SECRET:
        raise RuntimeError(
            "review_session_secret is still the dev default — set a real REVIEW_SESSION_SECRET"
        )
    return SharedSecretAuth(shared_secret=secret, signing_key=session_key)


def current_user(request: Request) -> User | None:
    return get_auth_provider().load_session(request.cookies.get(COOKIE_NAME))


def require_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status_code=303,
            detail="login required",
            headers={"Location": "/review/login"},
        )
    return user

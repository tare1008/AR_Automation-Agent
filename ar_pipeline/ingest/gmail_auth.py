"""Google OAuth (installed-app / loopback flow) sign-in for a single Gmail
mailbox — Gmail's analogue of ``ingest/auth.py``'s ``DelegatedGraphAuth``.

Unlike Microsoft personal accounts, Google has no "needs a directory first"
requirement: any Gmail account can create a Cloud project and OAuth client
directly. That's the whole reason this path exists alongside the Graph one.
"""

from __future__ import annotations

import pathlib

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from ar_pipeline.config import get_settings

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


class GmailNotConfigured(Exception):
    """Gmail OAuth client id/secret are not set in configuration."""


class GmailLoginRequired(Exception):
    """No usable token yet — run ``ar-pipeline gmail-login``."""


class GmailAuth:
    def __init__(self, client_id: str, client_secret: str, cache_path: pathlib.Path) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._cache_path = cache_path

    @classmethod
    def from_settings(cls) -> GmailAuth:
        s = get_settings()
        secret = s.gmail_client_secret.get_secret_value()
        if not (s.gmail_client_id and secret):
            raise GmailNotConfigured
        return cls(s.gmail_client_id, secret, pathlib.Path(s.gmail_token_cache_path))

    def _client_config(self) -> dict:
        return {
            "installed": {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }

    def _load_credentials(self) -> Credentials | None:
        if not self._cache_path.exists():
            return None
        return Credentials.from_authorized_user_file(str(self._cache_path), _SCOPES)

    def _save_credentials(self, creds: Credentials) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(creds.to_json())

    def token(self) -> str:
        """Silent-only — a background poller must never block on interactive
        consent. Refreshes from the cached refresh token if the access token
        has expired; raises ``GmailLoginRequired`` if there's nothing to work
        with yet, same shape as the Graph delegated auth's ``token()``."""
        creds = self._load_credentials()
        if creds is None:
            raise GmailLoginRequired("no signed-in account — run `ar-pipeline gmail-login`")
        if not creds.valid:
            if not (creds.expired and creds.refresh_token):
                raise GmailLoginRequired("stored sign-in invalid — run `ar-pipeline gmail-login`")
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise GmailLoginRequired(
                    f"stored sign-in expired — run `ar-pipeline gmail-login` ({exc})"
                ) from exc
            self._save_credentials(creds)
        assert creds.token is not None
        return creds.token

    def login_interactive(self) -> str:
        """One-time interactive OAuth sign-in: opens a local browser tab for
        consent (loopback redirect), saves the refresh token on success, and
        returns the signed-in address for a confirmation message."""
        flow = InstalledAppFlow.from_client_config(self._client_config(), _SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
        self._save_credentials(creds)
        resp = httpx.get(_PROFILE_URL, headers={"Authorization": f"Bearer {creds.token}"})
        resp.raise_for_status()
        email: str = resp.json().get("emailAddress", "unknown")
        return email

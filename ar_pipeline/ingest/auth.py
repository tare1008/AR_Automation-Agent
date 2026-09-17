from __future__ import annotations

import pathlib

import msal

from ar_pipeline.config import get_settings

_SCOPES = ["https://graph.microsoft.com/.default"]
_DELEGATED_SCOPES = ["https://graph.microsoft.com/Mail.Read", "offline_access"]


class GraphNotConfigured(Exception):
    """Graph credentials / mailbox are not set in configuration."""


class GraphLoginRequired(Exception):
    """Delegated mode has no usable token yet — run ``ar-pipeline graph-login``."""


class GraphAuth:
    """App-only (client-credentials) auth — one token for any mailbox in a
    Microsoft 365 tenant. Requires tenant admin consent; does not work for
    personal Microsoft accounts."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        self._app = msal.ConfidentialClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=client_secret,
        )

    @classmethod
    def from_settings(cls) -> GraphAuth:
        s = get_settings()
        secret = s.graph_client_secret.get_secret_value()
        if not (s.graph_tenant_id and s.graph_client_id and secret and s.shared_mailbox):
            raise GraphNotConfigured
        return cls(s.graph_tenant_id, s.graph_client_id, secret)

    def token(self) -> str:
        result = self._app.acquire_token_for_client(scopes=_SCOPES)
        if "access_token" not in result:
            raise RuntimeError(
                f"MSAL token error: {result.get('error')} {result.get('error_description')}"
            )
        token: str = result["access_token"]
        return token


class DelegatedGraphAuth:
    """Device-code sign-in as a single mailbox owner — the only Graph auth
    mode that works against a personal Microsoft account. No client secret:
    it's a public client (device code + PKCE), so nothing confidential is
    stored except the token cache (a refresh token), which is why that
    cache file belongs next to ``BLOB_DIR``, not in source control.

    ``token()`` only ever tries a *silent* refresh — a background poller
    must never block on interactive device-code input. If the cache has no
    usable account yet (first run, or the refresh token finally expired
    from long inactivity), it raises ``GraphLoginRequired`` and the caller
    is expected to log that and skip the poll, same as ``GraphNotConfigured``.
    """

    def __init__(self, client_id: str, authority: str, cache_path: pathlib.Path) -> None:
        self._cache_path = cache_path
        self._cache = msal.SerializableTokenCache()
        if cache_path.exists():
            self._cache.deserialize(cache_path.read_text())
        self._app = msal.PublicClientApplication(
            client_id, authority=authority, token_cache=self._cache
        )

    @classmethod
    def from_settings(cls) -> DelegatedGraphAuth:
        s = get_settings()
        if not s.graph_client_id:
            raise GraphNotConfigured
        return cls(s.graph_client_id, s.graph_authority, pathlib.Path(s.graph_token_cache_path))

    def _save_cache_if_changed(self) -> None:
        if self._cache.has_state_changed:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(self._cache.serialize())

    def token(self) -> str:
        accounts = self._app.get_accounts()
        if not accounts:
            raise GraphLoginRequired("no signed-in account — run `ar-pipeline graph-login`")
        result = self._app.acquire_token_silent(_DELEGATED_SCOPES, account=accounts[0])
        self._save_cache_if_changed()
        if not result or "access_token" not in result:
            raise GraphLoginRequired("stored sign-in expired — run `ar-pipeline graph-login`")
        token: str = result["access_token"]
        return token

    def login_device_code(self) -> str:
        """Interactive, one-time device-code sign-in. Returns the signed-in
        account's username (usually the email address) on success."""
        flow = self._app.initiate_device_flow(scopes=_DELEGATED_SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"MSAL device-flow error: {flow.get('error_description')}")
        print(flow["message"])  # noqa: T201 — the whole point of this command is the printed prompt
        result = self._app.acquire_token_by_device_flow(flow)
        self._save_cache_if_changed()
        if "access_token" not in result:
            raise RuntimeError(
                f"MSAL token error: {result.get('error')} {result.get('error_description')}"
            )
        account: str = result.get("id_token_claims", {}).get("preferred_username", "unknown")
        return account


def build_graph_auth() -> GraphAuth | DelegatedGraphAuth:
    mode = get_settings().graph_auth_mode
    if mode == "delegated":
        return DelegatedGraphAuth.from_settings()
    return GraphAuth.from_settings()

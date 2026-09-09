from __future__ import annotations

import msal

from ar_pipeline.config import get_settings

_SCOPES = ["https://graph.microsoft.com/.default"]


class GraphNotConfigured(Exception):
    """Graph credentials / mailbox are not set in configuration."""


class GraphAuth:
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

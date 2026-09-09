from unittest.mock import patch

import pytest

from ar_pipeline.ingest.auth import GraphAuth, GraphNotConfigured


@patch("ar_pipeline.ingest.auth.msal.ConfidentialClientApplication")
def test_token_returns_access_token(mock_app_cls):
    app = mock_app_cls.return_value
    app.acquire_token_for_client.return_value = {"access_token": "tok-123"}
    auth = GraphAuth("tenant", "client", "secret")
    assert auth.token() == "tok-123"
    app.acquire_token_for_client.assert_called_once_with(
        scopes=["https://graph.microsoft.com/.default"]
    )


@patch("ar_pipeline.ingest.auth.msal.ConfidentialClientApplication")
def test_token_raises_on_error_response(mock_app_cls):
    app = mock_app_cls.return_value
    app.acquire_token_for_client.return_value = {
        "error": "invalid_client",
        "error_description": "bad secret",
    }
    auth = GraphAuth("tenant", "client", "secret")
    with pytest.raises(RuntimeError, match="invalid_client"):
        auth.token()


def test_from_settings_raises_when_unconfigured(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    import ar_pipeline.config as config_module

    config_module.get_settings.cache_clear()
    with pytest.raises(GraphNotConfigured):
        GraphAuth.from_settings()
    config_module.get_settings.cache_clear()

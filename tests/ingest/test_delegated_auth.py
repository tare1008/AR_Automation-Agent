from unittest.mock import patch

import pytest

from ar_pipeline.ingest.auth import (
    DelegatedGraphAuth,
    GraphAuth,
    GraphLoginRequired,
    GraphNotConfigured,
    build_graph_auth,
)


def _auth(tmp_path, mock_app_cls):
    app = mock_app_cls.return_value
    auth = DelegatedGraphAuth(
        "client-id", "https://login.microsoftonline.com/consumers", tmp_path / "cache.json"
    )
    return auth, app


@patch("ar_pipeline.ingest.auth.msal.PublicClientApplication")
def test_token_returns_access_token_when_account_cached(mock_app_cls, tmp_path):
    auth, app = _auth(tmp_path, mock_app_cls)
    app.get_accounts.return_value = [{"username": "me@outlook.com"}]
    app.acquire_token_silent.return_value = {"access_token": "tok-456"}

    assert auth.token() == "tok-456"
    app.acquire_token_silent.assert_called_once()


@patch("ar_pipeline.ingest.auth.msal.PublicClientApplication")
def test_token_raises_login_required_when_no_account(mock_app_cls, tmp_path):
    auth, app = _auth(tmp_path, mock_app_cls)
    app.get_accounts.return_value = []

    with pytest.raises(GraphLoginRequired):
        auth.token()


@patch("ar_pipeline.ingest.auth.msal.PublicClientApplication")
def test_token_raises_login_required_when_silent_refresh_fails(mock_app_cls, tmp_path):
    auth, app = _auth(tmp_path, mock_app_cls)
    app.get_accounts.return_value = [{"username": "me@outlook.com"}]
    app.acquire_token_silent.return_value = {"error": "invalid_grant"}

    with pytest.raises(GraphLoginRequired):
        auth.token()


@patch("ar_pipeline.ingest.auth.msal.PublicClientApplication")
def test_login_device_code_prints_prompt_and_returns_account(mock_app_cls, tmp_path, capsys):
    auth, app = _auth(tmp_path, mock_app_cls)
    app.initiate_device_flow.return_value = {
        "user_code": "ABC123",
        "message": "Go to https://microsoft.com/devicelogin and enter ABC123",
    }
    app.acquire_token_by_device_flow.return_value = {
        "access_token": "tok-789",
        "id_token_claims": {"preferred_username": "me@outlook.com"},
    }

    account = auth.login_device_code()

    assert account == "me@outlook.com"
    assert "devicelogin" in capsys.readouterr().out


@patch("ar_pipeline.ingest.auth.msal.PublicClientApplication")
def test_login_device_code_raises_on_flow_error(mock_app_cls, tmp_path):
    auth, app = _auth(tmp_path, mock_app_cls)
    app.initiate_device_flow.return_value = {"error_description": "bad client id"}

    with pytest.raises(RuntimeError, match="bad client id"):
        auth.login_device_code()


@patch("ar_pipeline.ingest.auth.msal.PublicClientApplication")
def test_login_device_code_raises_when_token_missing(mock_app_cls, tmp_path):
    auth, app = _auth(tmp_path, mock_app_cls)
    app.initiate_device_flow.return_value = {"user_code": "ABC123", "message": "go sign in"}
    app.acquire_token_by_device_flow.return_value = {
        "error": "authorization_declined",
        "error_description": "user declined",
    }

    with pytest.raises(RuntimeError, match="authorization_declined"):
        auth.login_device_code()


def test_from_settings_raises_when_client_id_missing(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("GRAPH_CLIENT_ID", "")
    import ar_pipeline.config as config_module

    config_module.get_settings.cache_clear()
    with pytest.raises(GraphNotConfigured):
        DelegatedGraphAuth.from_settings()
    config_module.get_settings.cache_clear()


def test_build_graph_auth_dispatches_on_mode(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("GRAPH_AUTH_MODE", "delegated")
    monkeypatch.setenv("GRAPH_CLIENT_ID", "client-id")
    import ar_pipeline.config as config_module

    config_module.get_settings.cache_clear()
    try:
        assert isinstance(build_graph_auth(), DelegatedGraphAuth)
    finally:
        config_module.get_settings.cache_clear()


def test_build_graph_auth_defaults_to_app_mode(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("GRAPH_AUTH_MODE", "app")
    import ar_pipeline.config as config_module

    config_module.get_settings.cache_clear()
    try:
        with pytest.raises(GraphNotConfigured):
            build_graph_auth()
    finally:
        config_module.get_settings.cache_clear()


def test_graph_auth_still_works_unmodified():
    # regression guard: adding DelegatedGraphAuth must not disturb GraphAuth.
    with patch("ar_pipeline.ingest.auth.msal.ConfidentialClientApplication") as mock_cls:
        mock_cls.return_value.acquire_token_for_client.return_value = {"access_token": "tok-1"}
        assert GraphAuth("tenant", "client", "secret").token() == "tok-1"

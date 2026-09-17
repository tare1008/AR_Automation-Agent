from unittest.mock import MagicMock, patch

import httpx
import pytest

from ar_pipeline.ingest.gmail_auth import GmailAuth, GmailLoginRequired, GmailNotConfigured


def _auth(tmp_path):
    return GmailAuth("client-id", "client-secret", tmp_path / "cache.json")


def _fake_creds(*, valid, expired=False, refresh_token=None, token="tok"):
    creds = MagicMock()
    creds.valid = valid
    creds.expired = expired
    creds.refresh_token = refresh_token
    creds.token = token
    creds.to_json.return_value = "{}"
    return creds


def test_token_returns_access_token_when_cache_valid(tmp_path):
    auth = _auth(tmp_path)
    (tmp_path / "cache.json").write_text("{}")
    fake = _fake_creds(valid=True, token="tok-1")
    with patch.object(auth, "_load_credentials", return_value=fake):
        assert auth.token() == "tok-1"


def test_token_refreshes_when_expired_with_refresh_token(tmp_path):
    auth = _auth(tmp_path)
    creds = _fake_creds(valid=False, expired=True, refresh_token="rt", token="tok-2")

    def _refresh(request):
        creds.token = "tok-2"

    creds.refresh.side_effect = _refresh
    with patch.object(auth, "_load_credentials", return_value=creds):
        assert auth.token() == "tok-2"
    creds.refresh.assert_called_once()
    assert (tmp_path / "cache.json").exists()


def test_token_raises_login_required_when_no_cache(tmp_path):
    auth = _auth(tmp_path)
    with (
        patch.object(auth, "_load_credentials", return_value=None),
        pytest.raises(GmailLoginRequired),
    ):
        auth.token()


def test_token_raises_login_required_when_invalid_and_no_refresh_token(tmp_path):
    auth = _auth(tmp_path)
    creds = _fake_creds(valid=False, expired=True, refresh_token=None)
    with (
        patch.object(auth, "_load_credentials", return_value=creds),
        pytest.raises(GmailLoginRequired),
    ):
        auth.token()


def test_token_raises_login_required_when_refresh_fails(tmp_path):
    auth = _auth(tmp_path)
    creds = _fake_creds(valid=False, expired=True, refresh_token="rt")
    creds.refresh.side_effect = RuntimeError("invalid_grant")
    with (
        patch.object(auth, "_load_credentials", return_value=creds),
        pytest.raises(GmailLoginRequired),
    ):
        auth.token()


@patch("ar_pipeline.ingest.gmail_auth.httpx.get")
@patch("ar_pipeline.ingest.gmail_auth.InstalledAppFlow")
def test_login_interactive_saves_cache_and_returns_email(mock_flow_cls, mock_get, tmp_path):
    auth = _auth(tmp_path)
    creds = _fake_creds(valid=True, token="tok-3")
    mock_flow_cls.from_client_config.return_value.run_local_server.return_value = creds
    mock_get.return_value = httpx.Response(
        200, json={"emailAddress": "me@gmail.com"}, request=httpx.Request("GET", "https://x")
    )

    email = auth.login_interactive()

    assert email == "me@gmail.com"
    assert (tmp_path / "cache.json").exists()


def test_from_settings_raises_when_unconfigured(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("GMAIL_CLIENT_ID", "")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "")
    import ar_pipeline.config as config_module

    config_module.get_settings.cache_clear()
    try:
        with pytest.raises(GmailNotConfigured):
            GmailAuth.from_settings()
    finally:
        config_module.get_settings.cache_clear()

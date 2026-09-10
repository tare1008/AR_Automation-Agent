import pytest
from fastapi import HTTPException

from ar_pipeline.review.auth import SharedSecretAuth, User, current_user, require_user


def _auth() -> SharedSecretAuth:
    return SharedSecretAuth(shared_secret="s3cret", signing_key="signing", max_age_seconds=1000)


def test_get_auth_provider_rejects_default_session_secret(monkeypatch):
    import ar_pipeline.config as config_module

    monkeypatch.setenv("REVIEW_AUTH_SECRET", "a-real-secret")
    monkeypatch.setenv("REVIEW_SESSION_SECRET", "dev-insecure-session-key")
    config_module.get_settings.cache_clear()
    from ar_pipeline.review.auth import get_auth_provider

    with pytest.raises(RuntimeError, match="dev default"):
        get_auth_provider()
    config_module.get_settings.cache_clear()


def test_get_auth_provider_rejects_blank_auth_secret(monkeypatch):
    import ar_pipeline.config as config_module

    monkeypatch.setenv("REVIEW_AUTH_SECRET", "")
    config_module.get_settings.cache_clear()
    from ar_pipeline.review.auth import get_auth_provider

    with pytest.raises(RuntimeError, match="review_auth_secret"):
        get_auth_provider()
    config_module.get_settings.cache_clear()


def test_check_password_handles_non_ascii():
    a = SharedSecretAuth(shared_secret="s3cret", signing_key="k")
    assert a.check_password("pä") is False  # no TypeError


def test_check_password_true_only_for_exact_secret():
    a = _auth()
    assert a.check_password("s3cret") is True
    assert a.check_password("wrong") is False
    assert a.check_password("") is False


def test_session_round_trips_the_name():
    a = _auth()
    token = a.issue_session("Asha Rao")
    assert a.load_session(token) == User(name="Asha Rao")


def test_tampered_token_is_rejected():
    a = _auth()
    token = a.issue_session("Asha Rao")
    assert a.load_session(token + "x") is None
    assert a.load_session(None) is None
    assert a.load_session("") is None


def test_expired_token_is_rejected():
    a = SharedSecretAuth(shared_secret="s", signing_key="k", max_age_seconds=-1)
    token = a.issue_session("Asha")
    assert a.load_session(token) is None


class _Req:
    def __init__(self, cookies):
        self.cookies = cookies


def test_current_user_returns_none_without_cookie(monkeypatch):
    monkeypatch.setattr("ar_pipeline.review.auth.get_auth_provider", _auth)
    assert current_user(_Req({})) is None  # type: ignore[arg-type]


def test_current_user_reads_valid_cookie(monkeypatch):
    monkeypatch.setattr("ar_pipeline.review.auth.get_auth_provider", _auth)
    token = _auth().issue_session("Asha")
    from ar_pipeline.review.auth import COOKIE_NAME

    assert current_user(_Req({COOKIE_NAME: token})) == User(name="Asha")  # type: ignore[arg-type]


def test_require_user_raises_303_without_session(monkeypatch):
    monkeypatch.setattr("ar_pipeline.review.auth.get_auth_provider", _auth)
    with pytest.raises(HTTPException) as ei:
        require_user(_Req({}))  # type: ignore[arg-type]
    assert ei.value.status_code == 303
    assert ei.value.headers.get("Location") == "/review/login"  # type: ignore[union-attr]

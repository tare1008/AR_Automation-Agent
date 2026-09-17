from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str  # required — no default
    test_database_url: str = "postgresql+psycopg://ar:ar@localhost:5432/ar_pipeline_test"
    blob_dir: str = "data/blob"

    # Which inbox connector ``poll_inbox`` uses: "graph" (Outlook/Microsoft
    # 365, default) or "gmail". Only one mailbox connector runs at a time.
    mailbox_provider: str = "graph"

    # "app" (default): client-credentials, one app-only token for any mailbox
    # in a Microsoft 365 tenant — needs tenant/client id + secret + admin
    # consent. "delegated": device-code sign-in as a single mailbox owner
    # (personal @outlook.com/@hotmail.com accounts can't use "app" mode at
    # all — Graph app-only permissions don't apply to consumer accounts).
    graph_auth_mode: str = "app"
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: SecretStr = SecretStr("")
    graph_authority: str = "https://login.microsoftonline.com/consumers"
    graph_token_cache_path: str = "data/graph_token_cache.json"
    shared_mailbox: str = ""

    # Gmail — a Google Cloud OAuth client (Desktop app type), no directory
    # requirement for personal accounts. Run `ar-pipeline gmail-login` once.
    gmail_client_id: str = ""
    gmail_client_secret: SecretStr = SecretStr("")
    gmail_token_cache_path: str = "data/gmail_token_cache.json"

    backend_url: str = ""
    backend_auth_header: SecretStr = SecretStr("")

    review_auth_secret: SecretStr = SecretStr("")
    review_session_secret: SecretStr = SecretStr("dev-insecure-session-key")
    review_cookie_secure: bool = True

    llm_provider: str = "anthropic"
    llm_model: str = "claude-opus-5"
    auto_approve_min_confidence: float = 0.0

    poll_interval_seconds: int = 300
    advance_interval_seconds: int = 60
    deliver_interval_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()

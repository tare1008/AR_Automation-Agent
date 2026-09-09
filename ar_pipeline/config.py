from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str  # required — no default
    test_database_url: str = "postgresql+psycopg://ar:ar@localhost:5432/ar_pipeline_test"
    blob_dir: str = "data/blob"

    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: SecretStr = SecretStr("")
    shared_mailbox: str = ""

    backend_url: str = ""
    backend_auth_header: SecretStr = SecretStr("")

    llm_provider: str = "anthropic"

    poll_interval_seconds: int = 300
    advance_interval_seconds: int = 60
    deliver_interval_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()

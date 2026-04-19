from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(..., alias="DATABASE_URL")
    test_database_url: str | None = Field(default=None, alias="TEST_DATABASE_URL")
    admin_token: str = Field(..., alias="ADMIN_TOKEN")

    tvmaze_base_url: str = Field(default="https://api.tvmaze.com", alias="TVMAZE_BASE_URL")
    tvmaze_rate_limit_requests: int = Field(default=18, alias="TVMAZE_RATE_LIMIT_REQUESTS")
    tvmaze_rate_limit_window_seconds: int = Field(
        default=10, alias="TVMAZE_RATE_LIMIT_WINDOW_SECONDS"
    )
    tvmaze_retry_max_attempts: int = Field(default=5, alias="TVMAZE_RETRY_MAX_ATTEMPTS")

    ingest_consecutive_failure_threshold: int = Field(
        default=10, alias="INGEST_CONSECUTIVE_FAILURE_THRESHOLD"
    )
    ingest_stale_run_minutes: int = Field(default=15, alias="INGEST_STALE_RUN_MINUTES")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # pydantic-settings reads from env

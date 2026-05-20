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

    activity_rollup_window_min: int = Field(default=30, alias="ACTIVITY_ROLLUP_WINDOW_MIN")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    cors_allowed_origins_raw: str = Field(
        default="https://app.tvbf.localhost", alias="CORS_ALLOWED_ORIGINS"
    )

    session_cookie_name: str = Field(default="tvbf_session", alias="SESSION_COOKIE_NAME")
    csrf_cookie_name: str = Field(default="csrf_token", alias="CSRF_COOKIE_NAME")
    session_ttl_days: int = Field(default=30, alias="SESSION_TTL_DAYS")
    cookie_secure: bool = Field(default=True, alias="COOKIE_SECURE")
    cookie_samesite: str = Field(default="lax", alias="COOKIE_SAMESITE")
    # Set this to the parent domain (e.g. ".tvbingefriend.com" in prod or
    # ".tvbf.localhost" in dev) so session+csrf cookies are shared between
    # the SPA and the API on different subdomains. Leave None for host-only.
    cookie_domain: str | None = Field(default=None, alias="COOKIE_DOMAIN")

    login_lockout_threshold: int = Field(default=5, alias="LOGIN_LOCKOUT_THRESHOLD")
    login_lockout_window_minutes: int = Field(default=15, alias="LOGIN_LOCKOUT_WINDOW_MINUTES")

    # Email transport. `smtp` is the default for local dev (Mailpit on the
    # shared `proxy` network). Set `EMAIL_PROVIDER=resend` + `RESEND_API_KEY`
    # in production.
    email_provider: str = Field(default="smtp", alias="EMAIL_PROVIDER")
    email_from_address: str = Field(
        default="TV BingeFriend <no-reply@tvbf.localhost>", alias="EMAIL_FROM_ADDRESS"
    )
    resend_api_key: str | None = Field(default=None, alias="RESEND_API_KEY")
    smtp_host: str = Field(default="mailpit", alias="SMTP_HOST")
    smtp_port: int = Field(default=1025, alias="SMTP_PORT")

    # Public base URL of the SPA. Used to build links in transactional emails.
    frontend_base_url: str = Field(default="https://app.tvbf.localhost", alias="FRONTEND_BASE_URL")

    # Linear feedback integration. Disabled by default; flip
    # LINEAR_FEEDBACK_ENABLED=true once an API key + team id are configured.
    linear_feedback_enabled: bool = Field(default=False, alias="LINEAR_FEEDBACK_ENABLED")
    linear_api_key: str | None = Field(default=None, alias="LINEAR_API_KEY")
    linear_team_id: str | None = Field(default=None, alias="LINEAR_TEAM_ID")
    linear_feedback_label_id: str | None = Field(default=None, alias="LINEAR_FEEDBACK_LABEL_ID")

    @property
    def cors_allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins_raw.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # pydantic-settings reads from env

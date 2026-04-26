import pytest
from pydantic import ValidationError

from tvbf.config import Settings


def test_settings_reads_database_url_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://a:b@c:5432/d")
    monkeypatch.setenv("ADMIN_TOKEN", "xxx")
    s = Settings()  # type: ignore[call-arg]
    assert s.database_url == "postgresql+asyncpg://a:b@c:5432/d"
    assert s.admin_token == "xxx"


def test_settings_has_sensible_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://a:b@c:5432/d")
    monkeypatch.setenv("ADMIN_TOKEN", "xxx")
    s = Settings()  # type: ignore[call-arg]
    assert s.tvmaze_base_url == "https://api.tvmaze.com"
    assert s.tvmaze_rate_limit_requests == 18
    assert s.tvmaze_rate_limit_window_seconds == 10
    assert s.tvmaze_retry_max_attempts == 5
    assert s.ingest_consecutive_failure_threshold == 10
    assert s.ingest_stale_run_minutes == 15
    assert s.log_level == "INFO"
    assert s.cors_allowed_origins == ["https://tvbf.localhost"]
    assert s.session_cookie_name == "tvbf_session"
    assert s.csrf_cookie_name == "csrf_token"
    assert s.session_ttl_days == 30
    assert s.cookie_secure is True
    assert s.cookie_samesite == "none"


def test_settings_parses_cors_origins_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://a:b@c:5432/d")
    monkeypatch.setenv("ADMIN_TOKEN", "xxx")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://a.example.com,https://b.example.com")
    s = Settings()  # type: ignore[call-arg]
    assert s.cors_allowed_origins == ["https://a.example.com", "https://b.example.com"]


def test_settings_requires_admin_token(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://a:b@c:5432/d")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_settings_session_overrides_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://a:b@c:5432/d")
    monkeypatch.setenv("ADMIN_TOKEN", "xxx")
    monkeypatch.setenv("SESSION_TTL_DAYS", "7")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("COOKIE_SAMESITE", "lax")
    s = Settings()  # type: ignore[call-arg]
    assert s.session_ttl_days == 7
    assert s.cookie_secure is False
    assert s.cookie_samesite == "lax"

from dataclasses import dataclass
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class Throttle:
    """One inbound request budget — a count and the window it is counted over.

    One frozen dataclass rather than two loose integers, for **half** of
    `rate_budget.Budget`'s reason: a call site states the budget it means, and
    the pair cannot drift apart across the settings below. Budget's other half
    does not transfer — it is one object because `get_rate_limiter` is
    `functools.cache`d and keys on the literal call, and nothing here is cached.
    It lives in this module rather than beside `auth_throttle.enforce` because
    `Settings` returns it and `db` imports `config`, so config cannot import
    anything under `app` without a cycle.

    **Named for the shape, not for the key** (NEU-1162 §6.1). It arrived as
    `IpThrottle` with NEU-1160 and every budget it held was keyed on an address;
    the report throttle is keyed on a *user*, and NEU-1157 adds a second
    user-keyed budget of exactly this shape. A second identical dataclass whose
    only difference is its name is the alternative, and it is worse. The
    `Settings` properties keep their own names, which is where the key is said.
    """

    max_attempts: int
    window_minutes: int


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(..., alias="DATABASE_URL")
    test_database_url: str | None = Field(default=None, alias="TEST_DATABASE_URL")
    admin_token: str = Field(..., alias="ADMIN_TOKEN")

    # TMDB. The credential is the account's **API Read Access Token** (the long
    # JWT), sent as `Authorization: Bearer` — never the `api_key` query
    # parameter, which lands in access logs, proxy logs and any error report
    # that echoes a URL. TMDB labels the bearer style "v4 auth"; it authenticates
    # the v3 endpoints this app calls, and the v3 key is not interchangeable
    # with it.
    #
    # Optional rather than required: an app serving reads out of `catalog` needs
    # no credential at all, and only the ingest and delta paths do. `TMDBClient`
    # raises when it is missing, so the failure surfaces at the call site rather
    # than at import, where it would take the whole process down.
    # Server-side only — nothing TMDB-shaped reaches the SPA.
    tmdb_read_access_token: str | None = Field(default=None, alias="TMDB_READ_ACCESS_TOKEN")
    tmdb_base_url: str = Field(default="https://api.themoviedb.org/3", alias="TMDB_BASE_URL")
    tmdb_image_base_url: str = Field(
        default="https://image.tmdb.org/t/p", alias="TMDB_IMAGE_BASE_URL"
    )
    # 20 req/s against a documented ceiling "somewhere in the 40 requests per
    # second range" with a CDN-level 50/s. Half the ceiling is still 11× what
    # TV Maze allowed, and no deadline needs more.
    tmdb_rate_limit_requests: int = Field(default=20, alias="TMDB_RATE_LIMIT_REQUESTS")
    tmdb_rate_limit_window_seconds: int = Field(default=1, alias="TMDB_RATE_LIMIT_WINDOW_SECONDS")
    tmdb_retry_max_attempts: int = Field(default=5, alias="TMDB_RETRY_MAX_ATTEMPTS")

    # TV Maze, which is not a mirror source any more (NEU-1050) and is read by
    # exactly one thing: the airdate oracle (NEU-1145). Keyless and free — there
    # is no credential to configure, which is half of why it was chosen over
    # Trakt. 18 calls per 10 seconds is TV Maze's published ~20/10s with room to
    # spare, and is the calibration ADR-0006 was validated at, restored token
    # for token.
    tvmaze_base_url: str = Field(default="https://api.tvmaze.com", alias="TVMAZE_BASE_URL")
    tvmaze_rate_limit_requests: int = Field(default=18, alias="TVMAZE_RATE_LIMIT_REQUESTS")
    tvmaze_rate_limit_window_seconds: int = Field(
        default=10, alias="TVMAZE_RATE_LIMIT_WINDOW_SECONDS"
    )
    tvmaze_retry_max_attempts: int = Field(default=5, alias="TVMAZE_RETRY_MAX_ATTEMPTS")

    # DeepInfra, which serves the model the weekly recommendations pass asks
    # for JSON (project spec §6). Optional rather than required for the
    # reason the TMDB token is: an app serving reads needs no credential, and
    # only the recommendations job does. `OpenAICompatClient` raises when either
    # of these is missing, so the failure surfaces at the call site rather than
    # at import, where it would take the whole process down.
    #
    # There is no `DEEPINFRA_BASE_URL`: a base URL is a property of the provider
    # rather than of a deployment, so it is a constant in `llm/registry.py`. The
    # model id is the knob that actually gets turned, which is why it is here.
    #
    # `RECOMMENDATION_MODEL` is deliberately **not defaulted**, and NEU-1180 is
    # why that stays true rather than why it should be revisited: the id has now
    # changed once, on the capacity measurement in
    # `scripts/probe_deepinfra_capacity.py`. A default here would be a claim the
    # client keeps making after the id is retired upstream or outgrown, and
    # asserting one from memory buys a non-retryable 404 that looks like an
    # outage. **The running id is not recorded anywhere in this repo** — not
    # here and not in `.env.example` — because the repo is public and the id is
    # the output of an expensive two-stage screen; it is set in the Coolify UI,
    # and the measurements behind it are in the umbrella `docs/`. Server-side
    # only — nothing about the provider reaches the SPA.
    deepinfra_api_key: str | None = Field(default=None, alias="DEEPINFRA_API_KEY")
    recommendation_model: str | None = Field(default=None, alias="RECOMMENDATION_MODEL")
    # This ceiling is **ours, not the provider's** (NEU-1099). DeepInfra's own
    # published limit was not measured, and one asserted from memory would be a
    # number that reads as a provider fact while being a guess. 5 per second is
    # deliberately conservative: the pass is sequential and makes one call per
    # changed user per week, so nothing today comes within three orders of
    # magnitude of it, and it exists to bound the bounded-semaphore change the
    # spec schedules for ~100–200 users (§10) rather than to pace anything now.
    # Raising it is a measurement, not an edit.
    deepinfra_rate_limit_requests: int = Field(default=5, alias="DEEPINFRA_RATE_LIMIT_REQUESTS")
    deepinfra_rate_limit_window_seconds: int = Field(
        default=1, alias="DEEPINFRA_RATE_LIMIT_WINDOW_SECONDS"
    )

    ingest_consecutive_failure_threshold: int = Field(
        default=10, alias="INGEST_CONSECUTIVE_FAILURE_THRESHOLD"
    )
    ingest_stale_run_minutes: int = Field(default=15, alias="INGEST_STALE_RUN_MINUTES")

    # healthchecks.io deadman for the TMDB catalog delta (NEU-1035), which runs
    # as a Coolify scheduled task. Coolify notifies when a task *fails*; it
    # cannot notify that one never ran — suspended and forgotten, container
    # down, scheduler broken. That gap is the whole reason for this. Unset makes
    # every ping a no-op, so local runs and tests never call out.
    #
    # One check per scheduled task, never one shared between them: either task
    # feeding a shared deadman keeps it alive on its own, so the day one stops
    # running the other goes on covering for it silently. `HEALTHCHECK_DAILY_URL`
    # was the TV Maze daily's and went with it (NEU-1050).
    healthcheck_catalog_url: str | None = Field(default=None, alias="HEALTHCHECK_CATALOG_URL")
    # The airdate reconciliation's own deadman (NEU-1145). Its own, for the rule
    # stated above and for no other reason: one check fed by both scheduled
    # tasks would let either keep it alive while the other quietly stopped.
    healthcheck_airdate_url: str | None = Field(default=None, alias="HEALTHCHECK_AIRDATE_URL")
    # The weekly recommendations pass's own deadman (NEU-1111). Third scheduled
    # task, third check, for the rule above — and the gap is widest here: this
    # one fires *weekly*, so a schedule that silently stops running is invisible
    # for seven days before anybody would even think to look.
    healthcheck_recommendations_url: str | None = Field(
        default=None, alias="HEALTHCHECK_RECOMMENDATIONS_URL"
    )
    # The daily trending snapshot's own deadman (NEU-1055). Fourth scheduled
    # task, fourth check, for the rule above — and this one hides the failure
    # best: a stopped snapshot does not error, it ages, and NEU-1056's seven-day
    # cutoff turns the section off a week later with nothing anywhere saying why.
    healthcheck_trending_url: str | None = Field(default=None, alias="HEALTHCHECK_TRENDING_URL")

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

    # Cloudflare Turnstile on `POST /auth/signup` (NEU-1160). **The switch is an
    # explicit boolean, not an inference from the secret's presence**, and
    # `create_app()` raises when it is true with no secret. Two knobs that can
    # disagree are worth the startup check because the failure they prevent is
    # the one the ticket exists to close: protection silently absent in
    # production. Deriving the switch from the credential — the shape
    # `TMDB_READ_ACCESS_TOKEN` and `DEEPINFRA_API_KEY` use — is right for those,
    # where an absent credential disables a job that fails loudly at its call
    # site; here it would mean a secret dropped from the Coolify UI turns signup
    # protection off with nothing anywhere saying so.
    #
    # Off is the default, so tests and localdev need no network. **Nothing in
    # the repo turns it on** — prod sets it in the Coolify UI. There is no
    # `TURNSTILE_SITE_KEY` here: the site key is public, is consumed by exactly
    # one thing (the widget in the SPA, which reads it from its own `env.ts`),
    # and a copy in backend config would be read by nothing while sitting ready
    # to disagree with the one the browser actually uses.
    turnstile_enabled: bool = Field(default=False, alias="TURNSTILE_ENABLED")
    turnstile_secret_key: str | None = Field(default=None, alias="TURNSTILE_SECRET_KEY")

    # How many `X-Forwarded-For` entries from the right the trusted proxy
    # boundary sits at — see `client_ip.py`. One in both environments (Traefik).
    # It exists so that putting Cloudflare, or any second proxy, in front of the
    # API later is a config change rather than a code change. **Raising it is a
    # trust decision**: setting it higher than the number of proxies actually in
    # front of the app hands the throttle key straight to the client.
    trusted_proxy_hops: int = Field(default=1, alias="TRUSTED_PROXY_HOPS")

    # The inbound per-IP throttle (NEU-1160), which has no switch and is always
    # on: it is local, needs no network, and the test suite truncates
    # `app.auth_attempt` between tests, so it is inert unless a test
    # deliberately exceeds a limit.
    #
    # Five signups per hour per address is far above any household and far below
    # a useful bot run. Ten login failures per fifteen minutes is twice the
    # per-email threshold above, so one forgetful person trips their own email
    # lockout well before they trip the network's.
    signup_ip_throttle_max: int = Field(default=5, alias="SIGNUP_IP_THROTTLE_MAX")
    signup_ip_throttle_window_minutes: int = Field(
        default=60, alias="SIGNUP_IP_THROTTLE_WINDOW_MINUTES"
    )
    login_ip_throttle_max: int = Field(default=10, alias="LOGIN_IP_THROTTLE_MAX")
    login_ip_throttle_window_minutes: int = Field(
        default=15, alias="LOGIN_IP_THROTTLE_WINDOW_MINUTES"
    )

    # The per-reporter budget on `POST /reports` (NEU-1162 §6), so the report
    # channel does not itself become a harassment vector. A **daily** window
    # because griefing is a volume problem measured in days; five is far above
    # any honest use — most users will file zero for life — while capping a
    # determined griefer at five Linear issues rather than the 72 an hourly
    # window of the same size would allow.
    report_throttle_max: int = Field(default=5, alias="REPORT_THROTTLE_MAX")
    report_throttle_window_minutes: int = Field(
        default=1440, alias="REPORT_THROTTLE_WINDOW_MINUTES"
    )

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
    # Optional label on the issues `report_service` files, so reports are
    # filterable apart from feedback. Unset means no label.
    linear_report_label_id: str | None = Field(default=None, alias="LINEAR_REPORT_LABEL_ID")

    # Optional recipient for a server-sent notification email each time a
    # feedback issue is created. Linear itself suppresses notifications when
    # the API actor is the recipient (i.e., when the personal API key is
    # owned by the same human you'd want to notify), so this is a workaround
    # without spinning up an OAuth app. Leave unset to disable.
    # It also carries the user-report notification (NEU-1162 §8.2): it means
    # "the maintainer's mailbox", and a second variable for the same human is a
    # second place to get it wrong. The name is now slightly narrow; noting that
    # beats renaming a live production variable.
    feedback_notify_email: str | None = Field(default=None, alias="FEEDBACK_NOTIFY_EMAIL")

    @property
    def cors_allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins_raw.split(",") if o.strip()]

    @property
    def signup_ip_throttle(self) -> Throttle:
        return Throttle(
            max_attempts=self.signup_ip_throttle_max,
            window_minutes=self.signup_ip_throttle_window_minutes,
        )

    @property
    def login_ip_throttle(self) -> Throttle:
        return Throttle(
            max_attempts=self.login_ip_throttle_max,
            window_minutes=self.login_ip_throttle_window_minutes,
        )

    @property
    def report_throttle(self) -> Throttle:
        return Throttle(
            max_attempts=self.report_throttle_max,
            window_minutes=self.report_throttle_window_minutes,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # pydantic-settings reads from env

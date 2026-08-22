# User Service + Watchlist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add email + password authentication, a server-side session layer, and per-user TV tracking (show-level statuses + per-episode watch state) to TV Binge Friend. Ships the backend `app` schema, all `/auth/*` and `/me/*` routes, and the frontend pages and controls that consume them.

**Architecture:** Single FastAPI service. Four new tables in the `app` schema with cross-schema FKs to `tvmaze.show` and `tvmaze.episode`. Session cookies (HttpOnly, SameSite=None, Secure) backed by an `app.session` table. Double-submit CSRF token. `argon2id` password hashing. Frontend: `AuthContext` + an `apiFetch` wrapper that injects the CSRF header; new pages for login/signup/my-list; watchlist controls grafted onto the existing show-detail page.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.x async + asyncpg, Pydantic v2, Alembic, argon2-cffi (new), httpx (tests). Frontend: React 19, React Router 7, TanStack Query 5, Vitest 2, RTL, MSW 2 (existing).

**Spec reference:** `docs/superpowers/specs/2026-04-25-user-service-watchlist-design.md`

---

## Execution notes for the implementing engineer

- **The user handles all git operations.** After each task, stop at the final "task complete" step. Do NOT run `git add`, `git commit`, or any other state-changing git command. Read-only git commands (`status`, `log`, `diff`) are fine.
- **Everything runs inside containers.** Backend: `task test -- <args>`, `task lint`, `task typecheck`, `task makemigration -- "<msg>"`, `task migrate`. Frontend: `task -d ../tvbf-frontend test --`, `task -d ../tvbf-frontend lint`, `task -d ../tvbf-frontend typecheck`. No host Python or Node.
- **Shared infra must be up first.** `task infra:up` from `tvbf-backend/` needs to have run at least once. Postgres + Traefik are the dependencies.
- **Test DB conventions (existing):** `tests/conftest.py` drops + recreates `tvmaze` and `app` schemas at session start, then uses `Base.metadata.create_all` to materialize tables. The `session` fixture truncates all `tvmaze.*` and `app.*` tables between tests. Adding new models to the `Base` metadata is enough for them to land in the test DB. **However**, Postgres extensions (`citext`, `pgcrypto`) used by the new tables are not created by `create_all` — Task 4 extends `conftest.py` to `CREATE EXTENSION IF NOT EXISTS` for both before `create_all` runs.
- **No `relationship()` declarations** on any of the new models. Tests that insert related rows in one transaction must `await session.flush()` between them. This matches the `tvmaze` model convention.
- **Route tests use `AsyncClient(ASGITransport(app=app))`**, never sync `TestClient`. The pattern is in `tests/test_browse_routes.py` and `tests/test_admin_routes.py`. Sync `TestClient` causes asyncpg event-loop mismatches.
- **Cookies in tests.** `httpx.AsyncClient` carries cookies between requests if you reuse the client. Auth route tests must reuse a single client across signup → me → logout. Authenticated tests get a pre-loaded client from the new `authed_client` fixture (Task 12).
- **`Settings()` instantiation in tests.** Pyright complains about missing positional args because Pydantic Settings uses env vars, not arguments. Existing tests use `# type: ignore[call-arg]` — match that pattern.
- **Frontend HMR through Traefik.** No changes here; the existing `vite.config.ts` is correct. Just be aware that adding new routes to React Router shows up immediately if HMR is healthy.
- **Frontend cookies.** The dev origin is `https://tvbf.localhost`; the API origin is `https://tvbf-backend.localhost`. The two are cross-site, so the frontend `fetch` calls must set `credentials: 'include'` and the backend cookies must be `SameSite=None; Secure`. This is in `apiFetch` (Task 19).

## File map

```
tvbf-backend/
  pyproject.toml                                  # Modified: + argon2-cffi
  src/tvbf/
    config.py                                     # Modified: + session/csrf settings
    main.py                                       # Modified: CORSMiddleware allow_credentials, allow more methods + headers; mount auth + me routers
    deps.py                                       # Modified: + get_current_user, require_csrf
    app/
      __init__.py                                 # Existed (empty); unchanged
      models.py                                   # Replaces placeholder: User, Session, UserShowWatch, UserEpisodeWatch
      auth.py                                     # Created: password + session helpers
      watchlist.py                                # Created: query helpers
      dto.py                                      # Created: Pydantic response/request models
    routers/
      auth.py                                     # Created: /auth/signup, /auth/login, /auth/logout, /auth/password
      me.py                                       # Created: /me, /me/shows*, /me/episodes*
  migrations/versions/
    XXXX_user_service_initial.py                  # Created (via task makemigration; then hand-edited)
  tests/
    conftest.py                                   # Modified: + CREATE EXTENSION citext, pgcrypto
    fixtures/
      users.py                                    # Created: make_user, authed_client
    test_app_models.py                            # Created
    test_app_auth.py                              # Created: password + session helpers
    test_auth_routes.py                           # Created: signup/login/logout/password
    test_csrf.py                                  # Created
    test_me_routes.py                             # Created: /me, /me/shows, /me/episodes
    test_watchlist_queries.py                     # Created
    test_config.py                                # Modified: assert new defaults

tvbf-frontend/
  src/
    api/
      client.ts                                   # Modified: extend ApiError, add credentials + csrf logic via apiFetch wrapper
      client.test.ts                              # Modified
      types.ts                                    # Modified: + User, WatchlistEntry, ShowStatus
      auth.ts                                     # Created: useAuth, login/signup/logout/changePassword/deleteAccount mutations
      auth.test.ts                                # Created
      me.ts                                       # Created: useMyShows, mutations for status + episode watch
      me.test.ts                                  # Created
    components/
      AuthContext.tsx                             # Created
      AuthContext.test.tsx                        # Created
      RequireAuth.tsx                             # Created
      RequireAuth.test.tsx                        # Created
      AppShell.tsx                                # Modified: header authed/unauthed
      UserMenu.tsx                                # Created
      ChangePasswordDialog.tsx                    # Created
      DeleteAccountDialog.tsx                     # Created
      EpisodeWatchCheckbox.tsx                    # Created
      SeasonWatchToggle.tsx                       # Created
      WatchlistStatusSelect.tsx                   # Created
      NextEpisodeCard.tsx                         # Created
    pages/
      LoginPage.tsx                               # Created
      LoginPage.test.tsx                          # Created
      SignupPage.tsx                              # Created
      SignupPage.test.tsx                         # Created
      MyListPage.tsx                              # Created
      MyListPage.test.tsx                         # Created
      ShowDetailPage.tsx                          # Modified: integrate watchlist controls + episode checkboxes
      EpisodesPage.tsx                            # Modified: integrate episode checkboxes
    router.tsx                                    # Modified: add /login, /signup, /my-list
    main.tsx                                      # Modified: wrap app in AuthContext provider
```

---

## Backend

### Task 1: Config — session and CSRF settings

**Files:**
- Modify: `tvbf-backend/src/tvbf/config.py`
- Modify: `tvbf-backend/tests/test_config.py`

- [ ] **Step 1: Add failing assertions for new defaults**

In `tests/test_config.py`, append to the body of `test_settings_has_sensible_defaults`:

```python
    assert s.session_cookie_name == "tvbf_session"
    assert s.csrf_cookie_name == "csrf_token"
    assert s.session_ttl_days == 30
    assert s.cookie_secure is True
    assert s.cookie_samesite == "none"
```

- [ ] **Step 2: Add a test for env-var overrides**

Append to `tests/test_config.py`:

```python
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
```

- [ ] **Step 3: Run tests, verify failures**

Run: `task test -- tests/test_config.py -v`
Expected: failures with `AttributeError: 'Settings' object has no attribute 'session_cookie_name'` (and similar).

- [ ] **Step 4: Add the settings**

In `src/tvbf/config.py`, add inside the `Settings` class (after `cors_allowed_origins_raw`):

```python
    session_cookie_name: str = Field(default="tvbf_session", alias="SESSION_COOKIE_NAME")
    csrf_cookie_name: str = Field(default="csrf_token", alias="CSRF_COOKIE_NAME")
    session_ttl_days: int = Field(default=30, alias="SESSION_TTL_DAYS")
    cookie_secure: bool = Field(default=True, alias="COOKIE_SECURE")
    cookie_samesite: str = Field(default="none", alias="COOKIE_SAMESITE")
```

- [ ] **Step 5: Run tests, verify pass**

Run: `task test -- tests/test_config.py -v`
Expected: all config tests pass.

---

### Task 2: Add `argon2-cffi` dependency

**Files:**
- Modify: `tvbf-backend/pyproject.toml`

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add `"argon2-cffi>=23.1.0"` to the `dependencies` list (the runtime list, not `[dev]`).

- [ ] **Step 2: Rebuild the container**

Run: `task build`
Expected: build succeeds; argon2-cffi installed.

- [ ] **Step 3: Verify import works**

Run: `task shell` and inside the container: `python -c "import argon2; print(argon2.__version__)"`
Expected: prints a version string. Exit the shell.

---

### Task 3: SQLAlchemy models — `app/models.py`

**Files:**
- Modify (replaces placeholder): `tvbf-backend/src/tvbf/app/models.py`
- Create: `tvbf-backend/tests/test_app_models.py`

- [ ] **Step 1: Write failing tests for model invariants**

Create `tests/test_app_models.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from tvbf.app.models import Session, User, UserEpisodeWatch, UserShowWatch
from tvbf.tvmaze.models import Episode, Show


@pytest.mark.asyncio
async def test_user_email_is_case_insensitive_unique(session):
    session.add(User(email="Alice@example.com", password_hash="x", display_name="Alice"))
    await session.commit()
    session.add(User(email="alice@example.com", password_hash="y", display_name="Alice2"))
    with pytest.raises(Exception):  # IntegrityError or wrapped
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_session_row_roundtrip(session):
    user = User(email="bob@example.com", password_hash="x", display_name="Bob")
    session.add(user)
    await session.flush()
    sess = Session(
        id="abc123",
        user_id=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(sess)
    await session.commit()
    found = (await session.execute(select(Session).where(Session.id == "abc123"))).scalar_one()
    assert found.user_id == user.id


@pytest.mark.asyncio
async def test_user_show_watch_status_check_constraint(session):
    user = User(email="c@example.com", password_hash="x", display_name="C")
    session.add(user)
    show = Show(id=900001, name="X", tvmaze_updated=1)
    session.add(show)
    await session.flush()
    session.add(UserShowWatch(user_id=user.id, show_id=show.id, status="bogus"))
    with pytest.raises(Exception):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_user_show_watch_valid_statuses(session):
    user = User(email="d@example.com", password_hash="x", display_name="D")
    session.add(user)
    show = Show(id=900002, name="X", tvmaze_updated=1)
    session.add(show)
    await session.flush()
    for status in ("watching", "want_to_watch", "dropped"):
        session.add(UserShowWatch(user_id=user.id, show_id=show.id, status=status))
        await session.commit()
        await session.execute(
            UserShowWatch.__table__.delete().where(UserShowWatch.user_id == user.id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_user_episode_watch_pk_prevents_duplicates(session):
    user = User(email="e@example.com", password_hash="x", display_name="E")
    session.add(user)
    show = Show(id=900003, name="X", tvmaze_updated=1)
    session.add(show)
    await session.flush()
    ep = Episode(id=900100, show_id=show.id, season=1, number=1)
    session.add(ep)
    await session.flush()
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=ep.id))
    await session.commit()
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=ep.id))
    with pytest.raises(Exception):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_user_delete_cascades_to_watch_tables(session):
    user = User(email="f@example.com", password_hash="x", display_name="F")
    session.add(user)
    show = Show(id=900004, name="X", tvmaze_updated=1)
    session.add(show)
    await session.flush()
    ep = Episode(id=900200, show_id=show.id, season=1, number=1)
    session.add(ep)
    session.add(UserShowWatch(user_id=user.id, show_id=show.id, status="watching"))
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=ep.id))
    session.add(Session(
        id="sess_x", user_id=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    ))
    await session.commit()

    await session.execute(User.__table__.delete().where(User.id == user.id))
    await session.commit()

    show_watch = (await session.execute(
        select(UserShowWatch).where(UserShowWatch.user_id == user.id)
    )).all()
    ep_watch = (await session.execute(
        select(UserEpisodeWatch).where(UserEpisodeWatch.user_id == user.id)
    )).all()
    sess = (await session.execute(select(Session).where(Session.user_id == user.id))).all()
    assert show_watch == []
    assert ep_watch == []
    assert sess == []
```

- [ ] **Step 2: Run tests, verify failures**

Run: `task test -- tests/test_app_models.py -v`
Expected: ImportError on `tvbf.app.models` (placeholder has no classes).

- [ ] **Step 3: Implement the models**

Replace `src/tvbf/app/models.py` with:

```python
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
)
from sqlalchemy.dialects.postgresql import CITEXT, INET, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from tvbf.db import Base


class User(Base):
    __tablename__ = "user"
    __table_args__ = {"schema": "app"}

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default="gen_random_uuid()",
    )
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )


class Session(Base):
    __tablename__ = "session"
    __table_args__ = (
        Index("ix_session_user_id", "user_id"),
        Index("ix_session_expires_at", "expires_at"),
        {"schema": "app"},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.user.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)


class UserShowWatch(Base):
    __tablename__ = "user_show_watch"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "show_id"),
        CheckConstraint(
            "status IN ('watching','want_to_watch','dropped')",
            name="ck_user_show_watch_status",
        ),
        Index("ix_user_show_watch_user_status", "user_id", "status"),
        {"schema": "app"},
    )

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.user.id", ondelete="CASCADE"),
        nullable=False,
    )
    show_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("tvmaze.show.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )


class UserEpisodeWatch(Base):
    __tablename__ = "user_episode_watch"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "episode_id"),
        {"schema": "app"},
    )

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.user.id", ondelete="CASCADE"),
        nullable=False,
    )
    episode_id: Mapped[int] = mapped_column(
        BigInteger,  # match tvmaze.episode.id type
        ForeignKey("tvmaze.episode.id", ondelete="CASCADE"),
        nullable=False,
    )
    watched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
```

Note on `episode_id` column type: open `src/tvbf/tvmaze/models.py` and confirm `Episode.id` is `Integer`. If it is `Integer`, change `BigInteger` above to `Integer`. If it is `BigInteger`, leave as-is. Match the FK target's type exactly or Alembic will fail.

- [ ] **Step 4: Verify Episode.id type and align**

Run: `grep -n "id:" tvbf-backend/src/tvbf/tvmaze/models.py | head -20` (run from outside container — read-only is fine, but you can also run it inside via `task shell`).
Expected: shows mapped column declarations. The `Episode.id` should be `Integer`. Update `app/models.py` accordingly: change `episode_id` to use `Integer` if needed.

- [ ] **Step 5: Run model tests**

Run: `task test -- tests/test_app_models.py -v`
Expected: still fails — `app` schema in test DB doesn't have the citext + pgcrypto extensions yet, and `gen_random_uuid()` requires pgcrypto. Move to Task 4.

---

### Task 4: Test conftest — create extensions in test DB

**Files:**
- Modify: `tvbf-backend/tests/conftest.py`

- [ ] **Step 1: Update the `test_engine` fixture**

In `tests/conftest.py`, modify the `test_engine` fixture's setup block. Replace:

```python
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS tvmaze CASCADE"))
        await conn.execute(text("DROP SCHEMA IF EXISTS app CASCADE"))
        await conn.execute(text("CREATE SCHEMA tvmaze"))
        await conn.execute(text("CREATE SCHEMA app"))
        await conn.run_sync(Base.metadata.create_all)
```

with:

```python
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS tvmaze CASCADE"))
        await conn.execute(text("DROP SCHEMA IF EXISTS app CASCADE"))
        await conn.execute(text("CREATE SCHEMA tvmaze"))
        await conn.execute(text("CREATE SCHEMA app"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.run_sync(Base.metadata.create_all)
```

- [ ] **Step 2: Ensure app models are imported into Base**

The test fixture uses `Base.metadata.create_all`, which only creates tables for models that have been imported. Add a top-level import in `conftest.py` to make sure the app models register:

```python
from tvbf.app import models as _app_models  # noqa: F401  -- register tables
from tvbf.tvmaze import models as _tvmaze_models  # noqa: F401  -- register tables (may already be implicit)
```

Place these imports at the top of the file, after `from tvbf.db import Base` and before fixtures.

- [ ] **Step 3: Run model tests**

Run: `task test -- tests/test_app_models.py -v`
Expected: all 6 tests pass.

- [ ] **Step 4: Run full suite to confirm no regressions**

Run: `task test`
Expected: full suite passes.

---

### Task 5: Alembic migration for the `app` schema

**Files:**
- Create: `tvbf-backend/migrations/versions/<hash>_user_service_initial.py` (via `task makemigration`, then hand-edited)

- [ ] **Step 1: Generate an empty migration scaffold**

Run: `task makemigration -- "user service initial"`
Expected: a new file under `tvbf-backend/migrations/versions/`. Note: Alembic autogenerate may diff against the live `tvbf` DB and produce noise (e.g., it doesn't know about extensions). Treat the generated file as a starting skeleton; rewrite the `upgrade` and `downgrade` bodies.

- [ ] **Step 2: Replace the migration body**

Open the new migration file. Keep the auto-generated revision id and `down_revision` (it should chain off `2668ec5f731e_drop_uq_season_show_number`). Replace the body with:

```python
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "user",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("email", name="uq_user_email"),
        schema="app",
    )

    op.create_table(
        "session",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app.user.id"], ondelete="CASCADE", name="fk_session_user"
        ),
        schema="app",
    )
    op.create_index("ix_session_user_id", "session", ["user_id"], schema="app")
    op.create_index("ix_session_expires_at", "session", ["expires_at"], schema="app")

    op.create_table(
        "user_show_watch",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("show_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("user_id", "show_id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app.user.id"], ondelete="CASCADE", name="fk_usw_user"
        ),
        sa.ForeignKeyConstraint(
            ["show_id"], ["tvmaze.show.id"], ondelete="CASCADE", name="fk_usw_show"
        ),
        sa.CheckConstraint(
            "status IN ('watching','want_to_watch','dropped')",
            name="ck_user_show_watch_status",
        ),
        schema="app",
    )
    op.create_index(
        "ix_user_show_watch_user_status",
        "user_show_watch",
        ["user_id", "status"],
        schema="app",
    )

    op.create_table(
        "user_episode_watch",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column(
            "watched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("user_id", "episode_id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app.user.id"], ondelete="CASCADE", name="fk_uew_user"
        ),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["tvmaze.episode.id"],
            ondelete="CASCADE",
            name="fk_uew_episode",
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("user_episode_watch", schema="app")
    op.drop_table("user_show_watch", schema="app")
    op.drop_index("ix_session_expires_at", table_name="session", schema="app")
    op.drop_index("ix_session_user_id", table_name="session", schema="app")
    op.drop_table("session", schema="app")
    op.drop_table("user", schema="app")
    # Extensions left in place intentionally — they're cheap and may be in use elsewhere.
```

If the `episode_id` type in Task 3 was changed to `BigInteger`, change `sa.Column("episode_id", sa.Integer(), nullable=False)` here to `sa.Column("episode_id", sa.BigInteger(), nullable=False)` to match.

- [ ] **Step 3: Apply the migration**

Run: `task migrate`
Expected: migration applied; output shows the new revision running.

- [ ] **Step 4: Verify schema**

Run: `docker exec tbc_postgresql_db psql -U root -d tvbf -c "\dt app.*"`
Expected: lists `app.user`, `app.session`, `app.user_show_watch`, `app.user_episode_watch`.

- [ ] **Step 5: Test downgrade then re-upgrade**

Run: `docker compose exec tvbf-backend alembic downgrade -1`
Then: `docker exec tbc_postgresql_db psql -U root -d tvbf -c "\dt app.*"`
Expected: no tables in `app` schema.

Then: `task migrate`
Expected: tables recreated.

---

### Task 6: Password helpers — `app/auth.py`

**Files:**
- Create: `tvbf-backend/src/tvbf/app/auth.py`
- Create: `tvbf-backend/tests/test_app_auth.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_app_auth.py`:

```python
import pytest

from tvbf.app.auth import hash_password, verify_password


def test_hash_password_returns_argon2_string():
    h = hash_password("hunter2")
    assert h.startswith("$argon2id$")


def test_verify_password_correct():
    h = hash_password("hunter2")
    assert verify_password("hunter2", h) is True


def test_verify_password_wrong():
    h = hash_password("hunter2")
    assert verify_password("wrong", h) is False


def test_verify_password_handles_invalid_hash():
    assert verify_password("hunter2", "not-a-real-hash") is False


def test_two_hashes_of_same_password_differ():
    a = hash_password("hunter2")
    b = hash_password("hunter2")
    assert a != b
```

- [ ] **Step 2: Run tests, verify failures**

Run: `task test -- tests/test_app_auth.py -v`
Expected: ImportError on `tvbf.app.auth`.

- [ ] **Step 3: Create `app/auth.py` with password helpers**

Create `src/tvbf/app/auth.py`:

```python
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError, InvalidHashError

_hasher = PasswordHasher()


def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
```

- [ ] **Step 4: Run tests, verify pass**

Run: `task test -- tests/test_app_auth.py -v`
Expected: 5 tests pass.

---

### Task 7: Session helpers — extend `app/auth.py`

**Files:**
- Modify: `tvbf-backend/src/tvbf/app/auth.py`
- Modify: `tvbf-backend/tests/test_app_auth.py`

- [ ] **Step 1: Add failing tests for session helpers**

Append to `tests/test_app_auth.py`:

```python
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from tvbf.app.auth import (
    create_session,
    delete_session,
    delete_user_sessions,
    lookup_session,
    new_csrf_token,
    new_session_id,
    touch_session,
)
from tvbf.app.models import Session, User


def test_new_session_id_is_url_safe_and_unique():
    a = new_session_id()
    b = new_session_id()
    assert a != b
    assert len(a) >= 32
    assert all(c.isalnum() or c in "-_" for c in a)


def test_new_csrf_token_is_url_safe():
    a = new_csrf_token()
    b = new_csrf_token()
    assert a != b
    assert len(a) >= 32


@pytest.mark.asyncio
async def test_create_and_lookup_session(session):
    user = User(email="g@example.com", password_hash="x", display_name="G")
    session.add(user)
    await session.flush()

    sess_id = await create_session(session, user_id=user.id, ttl_days=7)
    await session.commit()

    found = await lookup_session(session, session_id=sess_id)
    assert found is not None
    assert found.user_id == user.id
    assert found.expires_at > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_lookup_session_returns_none_when_expired(session):
    user = User(email="h@example.com", password_hash="x", display_name="H")
    session.add(user)
    await session.flush()
    sess = Session(
        id="expired",
        user_id=user.id,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    session.add(sess)
    await session.commit()
    assert await lookup_session(session, session_id="expired") is None


@pytest.mark.asyncio
async def test_lookup_session_returns_none_for_unknown(session):
    assert await lookup_session(session, session_id="does-not-exist") is None


@pytest.mark.asyncio
async def test_touch_session_updates_last_seen(session):
    user = User(email="i@example.com", password_hash="x", display_name="I")
    session.add(user)
    await session.flush()
    sess_id = await create_session(session, user_id=user.id, ttl_days=30)
    await session.commit()

    before = (
        await session.execute(select(Session).where(Session.id == sess_id))
    ).scalar_one().last_seen_at

    await touch_session(session, session_id=sess_id)
    await session.commit()

    after = (
        await session.execute(
            select(Session)
            .where(Session.id == sess_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one().last_seen_at
    assert after >= before


@pytest.mark.asyncio
async def test_delete_session(session):
    user = User(email="j@example.com", password_hash="x", display_name="J")
    session.add(user)
    await session.flush()
    sess_id = await create_session(session, user_id=user.id, ttl_days=30)
    await session.commit()

    await delete_session(session, session_id=sess_id)
    await session.commit()

    assert await lookup_session(session, session_id=sess_id) is None


@pytest.mark.asyncio
async def test_delete_user_sessions(session):
    user = User(email="k@example.com", password_hash="x", display_name="K")
    session.add(user)
    await session.flush()
    a = await create_session(session, user_id=user.id, ttl_days=30)
    b = await create_session(session, user_id=user.id, ttl_days=30)
    await session.commit()

    await delete_user_sessions(session, user_id=user.id)
    await session.commit()

    assert await lookup_session(session, session_id=a) is None
    assert await lookup_session(session, session_id=b) is None
```

- [ ] **Step 2: Run tests, verify failures**

Run: `task test -- tests/test_app_auth.py -v`
Expected: ImportError on the new helpers.

- [ ] **Step 3: Implement session helpers**

Append to `src/tvbf/app/auth.py`:

```python
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import Session


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


async def create_session(
    db: AsyncSession,
    *,
    user_id: UUID,
    ttl_days: int,
    user_agent: str | None = None,
    ip: str | None = None,
) -> str:
    sess_id = new_session_id()
    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    db.add(
        Session(
            id=sess_id,
            user_id=user_id,
            expires_at=expires_at,
            user_agent=user_agent,
            ip=ip,
        )
    )
    return sess_id


async def lookup_session(db: AsyncSession, *, session_id: str) -> Session | None:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Session).where(Session.id == session_id, Session.expires_at > now)
    )
    return result.scalar_one_or_none()


async def touch_session(db: AsyncSession, *, session_id: str) -> None:
    await db.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(last_seen_at=datetime.now(timezone.utc))
    )


async def delete_session(db: AsyncSession, *, session_id: str) -> None:
    await db.execute(delete(Session).where(Session.id == session_id))


async def delete_user_sessions(db: AsyncSession, *, user_id: UUID) -> None:
    await db.execute(delete(Session).where(Session.user_id == user_id))
```

- [ ] **Step 4: Run tests, verify pass**

Run: `task test -- tests/test_app_auth.py -v`
Expected: 12 tests pass (5 password + 7 session).

---

### Task 8: FastAPI deps — `get_current_user` and `require_csrf`

**Files:**
- Modify: `tvbf-backend/src/tvbf/deps.py`
- Create: `tvbf-backend/tests/test_csrf.py`

(The `get_current_user` dep is exercised end-to-end by the route tests in later tasks — no isolated unit test for it here. CSRF gets a dedicated test file because its behavior is independent of any specific route.)

- [ ] **Step 1: Write failing CSRF tests**

Create `tests/test_csrf.py`:

```python
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from tvbf.deps import require_csrf


def _build_app() -> FastAPI:
    app = FastAPI()
    router = APIRouter()

    @router.post("/danger", dependencies=[Depends(require_csrf)])
    async def danger() -> dict[str, bool]:
        return {"ok": True}

    @router.get("/safe")
    async def safe() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_csrf_passes_when_header_matches_cookie():
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.cookies.set("csrf_token", "abc123", domain="t")
        r = await c.post("/danger", headers={"X-CSRF-Token": "abc123"})
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_csrf_rejects_when_header_missing():
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.cookies.set("csrf_token", "abc123", domain="t")
        r = await c.post("/danger")
        assert r.status_code == 403
        assert r.json()["detail"] == "csrf_invalid"


@pytest.mark.asyncio
async def test_csrf_rejects_when_header_mismatched():
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.cookies.set("csrf_token", "abc123", domain="t")
        r = await c.post("/danger", headers={"X-CSRF-Token": "different"})
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_csrf_rejects_when_cookie_missing():
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/danger", headers={"X-CSRF-Token": "abc123"})
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_get_does_not_require_csrf():
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/safe")
        assert r.status_code == 200
```

- [ ] **Step 2: Run tests, verify failures**

Run: `task test -- tests/test_csrf.py -v`
Expected: ImportError on `require_csrf`.

- [ ] **Step 3: Add deps to `deps.py`**

Replace `src/tvbf/deps.py` with:

```python
from collections.abc import AsyncIterator

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.auth import lookup_session, touch_session
from tvbf.app.models import User
from tvbf.config import Settings, get_settings
from tvbf.db import SessionLocal


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


def require_admin(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.admin_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


async def require_csrf(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    cookie = request.cookies.get(settings.csrf_cookie_name)
    header = request.headers.get("X-CSRF-Token")
    if not cookie or not header or cookie != header:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf_invalid")


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> User:
    sess_id = request.cookies.get(settings.session_cookie_name)
    if not sess_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="auth_required")
    sess = await lookup_session(db, session_id=sess_id)
    if sess is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="auth_required")
    user = await db.get(User, sess.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="auth_required")
    await touch_session(db, session_id=sess_id)
    await db.commit()
    return user
```

The CSRF cookie name in the test is hardcoded to `csrf_token`, which is the default in `Settings`. The test doesn't override settings so the default applies.

- [ ] **Step 4: Run CSRF tests, verify pass**

Run: `task test -- tests/test_csrf.py -v`
Expected: 5 tests pass.

- [ ] **Step 5: Run full suite to confirm no regressions**

Run: `task test`
Expected: full suite passes.

---

### Task 9: DTOs — `app/dto.py`

**Files:**
- Create: `tvbf-backend/src/tvbf/app/dto.py`

(No standalone tests; these are exercised by the route tests in later tasks.)

- [ ] **Step 1: Create `app/dto.py`**

Create `src/tvbf/app/dto.py`:

```python
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from tvbf.tvmaze.dto import EpisodeOut, ShowSummary


ShowStatus = Literal["watching", "want_to_watch", "dropped"]
ShowStatusFilter = Literal["watching", "want_to_watch", "dropped", "watched"]


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class AccountDeleteRequest(BaseModel):
    password: str


class StatusUpdateRequest(BaseModel):
    status: ShowStatus


class UserOut(BaseModel):
    id: UUID
    email: str
    display_name: str
    created_at: datetime


class WatchlistEntry(BaseModel):
    show: ShowSummary
    status: ShowStatusFilter
    watched_episode_count: int
    total_episode_count: int
    next_episode: EpisodeOut | None = None
    updated_at: datetime


class EpisodeWatchOut(BaseModel):
    episode_id: int
    watched_at: datetime


class BulkSeasonResult(BaseModel):
    marked: int
```

Note: `EmailStr` requires the `email-validator` package. If it isn't already a dep, add `email-validator>=2.0.0` to `pyproject.toml` and rebuild.

- [ ] **Step 2: Verify email-validator dependency**

Run: `task shell` and inside: `python -c "import email_validator"`
If `ModuleNotFoundError`, add `"email-validator>=2.0.0"` to `pyproject.toml`'s `dependencies` and run `task build`.

- [ ] **Step 3: Confirm imports succeed**

Run: `docker compose exec tvbf-backend python -c "from tvbf.app.dto import UserOut, WatchlistEntry; print('ok')"`
Expected: prints `ok`.

---

### Task 10: Auth router — signup

**Files:**
- Create: `tvbf-backend/src/tvbf/routers/auth.py`
- Modify: `tvbf-backend/src/tvbf/main.py`
- Create: `tvbf-backend/tests/test_auth_routes.py`

- [ ] **Step 1: Update CORS middleware in main.py**

In `src/tvbf/main.py`, replace the `app.add_middleware(CORSMiddleware, ...)` call with:

```python
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )
```

- [ ] **Step 2: Write failing signup tests**

Create `tests/test_auth_routes.py`:

```python
import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.main import app


@pytest.fixture
async def client(session):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_signup_creates_user_and_sets_cookies(client):
    r = await client.post(
        "/auth/signup",
        json={
            "email": "Alice@example.com",
            "password": "hunter2hunter2",
            "display_name": "Alice",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "Alice@example.com"
    assert body["display_name"] == "Alice"
    assert "id" in body
    cookies = {c.name: c.value for c in r.cookies.jar}
    assert "tvbf_session" in cookies
    assert "csrf_token" in cookies


@pytest.mark.asyncio
async def test_signup_rejects_duplicate_email_case_insensitive(client):
    r1 = await client.post(
        "/auth/signup",
        json={"email": "bob@example.com", "password": "hunter2hunter2", "display_name": "Bob"},
    )
    assert r1.status_code == 201
    # Different client to avoid carrying the first session cookie.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c2:
        r2 = await c2.post(
            "/auth/signup",
            json={
                "email": "BOB@example.com",
                "password": "hunter2hunter2",
                "display_name": "Bob2",
            },
        )
    assert r2.status_code == 409
    assert r2.json()["detail"] == "email_in_use"


@pytest.mark.asyncio
async def test_signup_rejects_short_password(client):
    r = await client.post(
        "/auth/signup",
        json={"email": "c@example.com", "password": "short", "display_name": "C"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_signup_rejects_invalid_email(client):
    r = await client.post(
        "/auth/signup",
        json={"email": "not-an-email", "password": "hunter2hunter2", "display_name": "X"},
    )
    assert r.status_code == 422
```

- [ ] **Step 3: Run tests, verify failures**

Run: `task test -- tests/test_auth_routes.py -v`
Expected: 404 on `/auth/signup` (router not mounted yet).

- [ ] **Step 4: Implement the auth router**

Create `src/tvbf/routers/auth.py`:

```python
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.auth import (
    create_session,
    hash_password,
    new_csrf_token,
)
from tvbf.app.dto import SignupRequest, UserOut
from tvbf.app.models import User
from tvbf.config import Settings, get_settings
from tvbf.deps import get_session


router = APIRouter(prefix="/auth", tags=["auth"])


def _set_auth_cookies(
    response: Response,
    *,
    session_id: str,
    csrf: str,
    settings: Settings,
) -> None:
    max_age = settings.session_ttl_days * 86400
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_id,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
        path="/",
    )
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=csrf,
        max_age=max_age,
        httponly=False,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
        path="/",
    )


@router.post("/signup", status_code=status.HTTP_201_CREATED, response_model=UserOut)
async def signup(
    payload: SignupRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> UserOut:
    user = User(
        email=str(payload.email),
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email_in_use")

    sess_id = await create_session(
        db,
        user_id=user.id,
        ttl_days=settings.session_ttl_days,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    csrf = new_csrf_token()
    await db.commit()
    await db.refresh(user)
    _set_auth_cookies(response, session_id=sess_id, csrf=csrf, settings=settings)
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        created_at=user.created_at,
    )
```

- [ ] **Step 5: Mount the auth router**

In `src/tvbf/main.py`, add to the import block at the top:

```python
from tvbf.routers import admin, auth, browse, health
```

And inside `create_app()`, after `app.include_router(browse.router)`, add:

```python
    app.include_router(auth.router)
```

- [ ] **Step 6: Run tests, verify pass**

Run: `task test -- tests/test_auth_routes.py -v`
Expected: 4 tests pass.

---

### Task 11: Auth router — login + logout

**Files:**
- Modify: `tvbf-backend/src/tvbf/routers/auth.py`
- Modify: `tvbf-backend/tests/test_auth_routes.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_auth_routes.py`:

```python
@pytest.mark.asyncio
async def test_login_succeeds_with_correct_credentials(client):
    await client.post(
        "/auth/signup",
        json={"email": "lo@example.com", "password": "hunter2hunter2", "display_name": "Lo"},
    )
    # Logout first to clear cookies, then re-login.
    csrf = client.cookies["csrf_token"]
    await client.post("/auth/logout", headers={"X-CSRF-Token": csrf})

    r = await client.post(
        "/auth/login",
        json={"email": "lo@example.com", "password": "hunter2hunter2"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "lo@example.com"
    assert "tvbf_session" in {c.name for c in r.cookies.jar}


@pytest.mark.asyncio
async def test_login_rejects_wrong_password(client):
    await client.post(
        "/auth/signup",
        json={"email": "wp@example.com", "password": "hunter2hunter2", "display_name": "WP"},
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c2:
        r = await c2.post(
            "/auth/login",
            json={"email": "wp@example.com", "password": "wrong"},
        )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"


@pytest.mark.asyncio
async def test_login_rejects_unknown_email(client):
    r = await client.post(
        "/auth/login",
        json={"email": "ghost@example.com", "password": "hunter2hunter2"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"


@pytest.mark.asyncio
async def test_logout_clears_cookies_and_invalidates_session(client):
    await client.post(
        "/auth/signup",
        json={"email": "out@example.com", "password": "hunter2hunter2", "display_name": "Out"},
    )
    csrf = client.cookies["csrf_token"]
    r = await client.post("/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204

    # /me should now 401 (route not yet defined; we'll verify this in Task 13).
    # For now, just verify the cookies have been cleared on the response.
    cookies = {c.name: c.value for c in r.cookies.jar}
    # set_cookie with empty value + expired age clears it; httpx sometimes drops it,
    # but the response Set-Cookie header should be present:
    set_cookie_headers = [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]
    assert any("tvbf_session=" in h for h in set_cookie_headers)


@pytest.mark.asyncio
async def test_logout_requires_csrf(client):
    await client.post(
        "/auth/signup",
        json={"email": "cs@example.com", "password": "hunter2hunter2", "display_name": "CS"},
    )
    r = await client.post("/auth/logout")
    assert r.status_code == 403
```

- [ ] **Step 2: Run, verify failures**

Run: `task test -- tests/test_auth_routes.py -v`
Expected: 404 on `/auth/login` and `/auth/logout`.

- [ ] **Step 3: Implement login + logout**

Append to `src/tvbf/routers/auth.py`:

```python
from tvbf.app.auth import (
    delete_session,
    verify_password,
)
from tvbf.app.dto import LoginRequest
from tvbf.deps import require_csrf


@router.post("/login", response_model=UserOut)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> UserOut:
    result = await db.execute(select(User).where(User.email == str(payload.email)))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials"
        )
    sess_id = await create_session(
        db,
        user_id=user.id,
        ttl_days=settings.session_ttl_days,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    csrf = new_csrf_token()
    await db.commit()
    _set_auth_cookies(response, session_id=sess_id, csrf=csrf, settings=settings)
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        created_at=user.created_at,
    )


def _clear_auth_cookies(response: Response, settings: Settings) -> None:
    for name in (settings.session_cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(
            key=name,
            path="/",
            secure=settings.cookie_secure,
            samesite=settings.cookie_samesite,  # type: ignore[arg-type]
            httponly=name == settings.session_cookie_name,
        )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    sess_id = request.cookies.get(settings.session_cookie_name)
    if sess_id:
        await delete_session(db, session_id=sess_id)
        await db.commit()
    _clear_auth_cookies(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
```

- [ ] **Step 4: Run tests, verify pass**

Run: `task test -- tests/test_auth_routes.py -v`
Expected: all auth-route tests pass.

---

### Task 12: Auth router — change password

**Files:**
- Modify: `tvbf-backend/src/tvbf/routers/auth.py`
- Modify: `tvbf-backend/tests/test_auth_routes.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_auth_routes.py`:

```python
@pytest.mark.asyncio
async def test_change_password_requires_correct_current_password(client):
    await client.post(
        "/auth/signup",
        json={"email": "pc@example.com", "password": "hunter2hunter2", "display_name": "PC"},
    )
    csrf = client.cookies["csrf_token"]
    r = await client.post(
        "/auth/password",
        headers={"X-CSRF-Token": csrf},
        json={"current_password": "wrong", "new_password": "newpassword99"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_change_password_rotates_session(client):
    await client.post(
        "/auth/signup",
        json={"email": "rot@example.com", "password": "hunter2hunter2", "display_name": "Rot"},
    )
    old_session = client.cookies["tvbf_session"]
    csrf = client.cookies["csrf_token"]
    r = await client.post(
        "/auth/password",
        headers={"X-CSRF-Token": csrf},
        json={"current_password": "hunter2hunter2", "new_password": "newpassword99"},
    )
    assert r.status_code == 204
    new_session = client.cookies["tvbf_session"]
    assert new_session != old_session

    # Verify the old session no longer authenticates by hitting /me later — but for
    # now, just verify that login with the new password works and the old fails.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c2:
        bad = await c2.post(
            "/auth/login",
            json={"email": "rot@example.com", "password": "hunter2hunter2"},
        )
        assert bad.status_code == 401
        good = await c2.post(
            "/auth/login",
            json={"email": "rot@example.com", "password": "newpassword99"},
        )
        assert good.status_code == 200
```

- [ ] **Step 2: Run, verify failures**

Run: `task test -- tests/test_auth_routes.py::test_change_password_rotates_session -v`
Expected: 404 on `/auth/password`.

- [ ] **Step 3: Implement change password**

Append to `src/tvbf/routers/auth.py`:

```python
from tvbf.app.auth import delete_user_sessions
from tvbf.app.dto import PasswordChangeRequest
from tvbf.deps import get_current_user


@router.post(
    "/password",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials"
        )
    user.password_hash = hash_password(payload.new_password)
    await delete_user_sessions(db, user_id=user.id)
    sess_id = await create_session(
        db,
        user_id=user.id,
        ttl_days=settings.session_ttl_days,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    csrf = new_csrf_token()
    await db.commit()
    _set_auth_cookies(response, session_id=sess_id, csrf=csrf, settings=settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
```

- [ ] **Step 4: Run, verify pass**

Run: `task test -- tests/test_auth_routes.py -v`
Expected: all auth-route tests pass (now 9).

---

### Task 13: Test fixtures — `make_user` and `authed_client`

**Files:**
- Create: `tvbf-backend/tests/fixtures/users.py`
- Modify: `tvbf-backend/tests/fixtures/__init__.py` (if needed; the dir already exists)

- [ ] **Step 1: Create the user fixtures**

Create `tests/fixtures/users.py`:

```python
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.auth import create_session, hash_password, new_csrf_token
from tvbf.app.models import User
from tvbf.main import app


@pytest.fixture
async def make_user(session: AsyncSession):
    """Factory that creates and returns an `app.user` row, committed."""

    async def _make(
        email: str = "user@example.com",
        password: str = "hunter2hunter2",
        display_name: str = "Test User",
    ) -> User:
        user = User(
            email=email,
            password_hash=hash_password(password),
            display_name=display_name,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user

    return _make


@pytest.fixture
async def authed_client(session: AsyncSession, make_user) -> AsyncIterator[AsyncClient]:
    """An AsyncClient with a freshly created user, valid session, and CSRF cookies."""
    user = await make_user()
    sess_id = await create_session(session, user_id=user.id, ttl_days=30)
    csrf = new_csrf_token()
    await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
    ) as c:
        c.cookies.set("tvbf_session", sess_id, domain="test")
        c.cookies.set("csrf_token", csrf, domain="test")
        c.headers["X-CSRF-Token"] = csrf
        c.user = user  # type: ignore[attr-defined]
        yield c
```

- [ ] **Step 2: Make the fixtures discoverable**

Append to `tests/conftest.py`:

```python
pytest_plugins = ["tests.fixtures.users"]
```

(If `pytest_plugins` already exists, append the entry to its list.)

- [ ] **Step 3: Smoke-test the fixture**

Add a smoke test by appending to `tests/test_app_auth.py`:

```python
@pytest.mark.asyncio
async def test_make_user_fixture(make_user):
    u = await make_user(email="fixture@example.com")
    assert u.email == "fixture@example.com"
    assert u.password_hash.startswith("$argon2id$")
```

Run: `task test -- tests/test_app_auth.py::test_make_user_fixture -v`
Expected: pass.

---

### Task 14: `/me` GET and DELETE

**Files:**
- Create: `tvbf-backend/src/tvbf/routers/me.py`
- Modify: `tvbf-backend/src/tvbf/main.py`
- Create: `tvbf-backend/tests/test_me_routes.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_me_routes.py`:

```python
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tvbf.app.models import User
from tvbf.main import app


@pytest.mark.asyncio
async def test_me_returns_current_user(authed_client):
    r = await authed_client.get("/me")
    assert r.status_code == 200
    assert r.json()["email"] == "user@example.com"


@pytest.mark.asyncio
async def test_me_returns_401_when_no_session():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/me")
    assert r.status_code == 401
    assert r.json()["detail"] == "auth_required"


@pytest.mark.asyncio
async def test_delete_me_requires_password(authed_client):
    r = await authed_client.request(
        "DELETE",
        "/me",
        json={"password": "wrong"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_me_succeeds_and_cascades(authed_client, session):
    user_id = authed_client.user.id  # type: ignore[attr-defined]
    r = await authed_client.request(
        "DELETE",
        "/me",
        json={"password": "hunter2hunter2"},
    )
    assert r.status_code == 204
    found = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    assert found is None


@pytest.mark.asyncio
async def test_delete_me_requires_csrf(session, make_user):
    from tvbf.app.auth import create_session
    user = await make_user(email="nocsrf@example.com")
    sess_id = await create_session(session, user_id=user.id, ttl_days=30)
    await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        c.cookies.set("tvbf_session", sess_id, domain="test")
        # No CSRF cookie / header.
        r = await c.request("DELETE", "/me", json={"password": "hunter2hunter2"})
    assert r.status_code == 403
```

- [ ] **Step 2: Run, verify failures**

Run: `task test -- tests/test_me_routes.py -v`
Expected: 404s.

- [ ] **Step 3: Implement the `me` router**

Create `src/tvbf/routers/me.py`:

```python
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.auth import verify_password
from tvbf.app.dto import AccountDeleteRequest, UserOut
from tvbf.app.models import User
from tvbf.deps import get_current_user, get_session, require_csrf


router = APIRouter(tags=["me"])


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        created_at=user.created_at,
    )


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def delete_me(
    payload: AccountDeleteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials"
        )
    await db.execute(delete(User).where(User.id == user.id))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

- [ ] **Step 4: Mount the me router**

In `src/tvbf/main.py`, update the import:

```python
from tvbf.routers import admin, auth, browse, health, me
```

and inside `create_app()`, after `app.include_router(auth.router)`:

```python
    app.include_router(me.router)
```

- [ ] **Step 5: Run tests, verify pass**

Run: `task test -- tests/test_me_routes.py -v`
Expected: 5 tests pass.

---

### Task 15: Watchlist queries — `app/watchlist.py`

**Files:**
- Create: `tvbf-backend/src/tvbf/app/watchlist.py`
- Create: `tvbf-backend/tests/test_watchlist_queries.py`

- [ ] **Step 1: Write failing query tests**

Create `tests/test_watchlist_queries.py`:

```python
import pytest

from tvbf.app.models import UserEpisodeWatch, UserShowWatch
from tvbf.app.watchlist import (
    list_watchlist_for_user,
    next_episode_for_user_show,
    set_user_show_status,
    unset_user_show_status,
)
from tvbf.tvmaze.models import Episode, Show


async def _seed_show(session, *, show_id: int, name: str = "S", episodes: int = 3) -> Show:
    show = Show(id=show_id, name=name, tvmaze_updated=1)
    session.add(show)
    await session.flush()
    for i in range(1, episodes + 1):
        session.add(Episode(id=show_id * 100 + i, show_id=show.id, season=1, number=i))
    await session.flush()
    return show


@pytest.mark.asyncio
async def test_set_user_show_status_inserts(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=910001)
    await session.commit()

    await set_user_show_status(session, user_id=user.id, show_id=show.id, status="watching")
    await session.commit()

    rows = (await session.execute(
        UserShowWatch.__table__.select().where(UserShowWatch.user_id == user.id)
    )).all()
    assert len(rows) == 1
    assert rows[0].status == "watching"


@pytest.mark.asyncio
async def test_set_user_show_status_updates_existing(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=910002)
    await session.commit()

    await set_user_show_status(session, user_id=user.id, show_id=show.id, status="watching")
    await session.commit()
    await set_user_show_status(session, user_id=user.id, show_id=show.id, status="dropped")
    await session.commit()

    rows = (await session.execute(
        UserShowWatch.__table__.select().where(UserShowWatch.user_id == user.id)
    )).all()
    assert len(rows) == 1
    assert rows[0].status == "dropped"


@pytest.mark.asyncio
async def test_unset_user_show_status_preserves_episode_watches(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=910003)
    await session.commit()

    await set_user_show_status(session, user_id=user.id, show_id=show.id, status="watching")
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=910003 * 100 + 1))
    await session.commit()

    await unset_user_show_status(session, user_id=user.id, show_id=show.id)
    await session.commit()

    show_rows = (await session.execute(
        UserShowWatch.__table__.select().where(UserShowWatch.user_id == user.id)
    )).all()
    ep_rows = (await session.execute(
        UserEpisodeWatch.__table__.select().where(UserEpisodeWatch.user_id == user.id)
    )).all()
    assert show_rows == []
    assert len(ep_rows) == 1


@pytest.mark.asyncio
async def test_next_episode_returns_first_unwatched(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=910004, episodes=3)
    await session.commit()

    # No watches: next is episode 1.
    nxt = await next_episode_for_user_show(session, user_id=user.id, show_id=show.id)
    assert nxt is not None
    assert nxt.number == 1

    # Mark episode 1 watched: next is episode 2.
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=910004 * 100 + 1))
    await session.commit()
    nxt = await next_episode_for_user_show(session, user_id=user.id, show_id=show.id)
    assert nxt is not None
    assert nxt.number == 2

    # Mark all watched: next is None.
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=910004 * 100 + 2))
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=910004 * 100 + 3))
    await session.commit()
    nxt = await next_episode_for_user_show(session, user_id=user.id, show_id=show.id)
    assert nxt is None


@pytest.mark.asyncio
async def test_list_watchlist_includes_counts_and_next_episode(session, make_user):
    user = await make_user()
    show_a = await _seed_show(session, show_id=910005, name="A", episodes=2)
    show_b = await _seed_show(session, show_id=910006, name="B", episodes=2)
    await session.commit()

    await set_user_show_status(session, user_id=user.id, show_id=show_a.id, status="watching")
    await set_user_show_status(session, user_id=user.id, show_id=show_b.id, status="want_to_watch")
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=910005 * 100 + 1))
    await session.commit()

    rows = await list_watchlist_for_user(session, user_id=user.id, status_filter=None)
    by_id = {e.show.id: e for e in rows}
    assert by_id[910005].status == "watching"
    assert by_id[910005].watched_episode_count == 1
    assert by_id[910005].total_episode_count == 2
    assert by_id[910005].next_episode is not None
    assert by_id[910005].next_episode.number == 2
    assert by_id[910006].status == "want_to_watch"
    assert by_id[910006].watched_episode_count == 0


@pytest.mark.asyncio
async def test_list_watchlist_watched_filter(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=910007, name="W", episodes=2)
    await session.commit()
    await set_user_show_status(session, user_id=user.id, show_id=show.id, status="watching")
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=910007 * 100 + 1))
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=910007 * 100 + 2))
    await session.commit()

    rows = await list_watchlist_for_user(session, user_id=user.id, status_filter="watched")
    assert len(rows) == 1
    assert rows[0].show.id == show.id

    rows = await list_watchlist_for_user(session, user_id=user.id, status_filter="watching")
    # The 'watching' filter excludes shows where every episode is watched.
    assert rows == []
```

- [ ] **Step 2: Run, verify failures**

Run: `task test -- tests/test_watchlist_queries.py -v`
Expected: ImportError on `tvbf.app.watchlist`.

- [ ] **Step 3: Implement the watchlist module**

Create `src/tvbf/app/watchlist.py`:

```python
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.dto import EpisodeWatchOut, ShowStatusFilter, WatchlistEntry
from tvbf.app.models import UserEpisodeWatch, UserShowWatch
from tvbf.tvmaze.dto import EpisodeOut, ShowSummary, build_show_summary
from tvbf.tvmaze.models import Episode, Show


async def set_user_show_status(
    db: AsyncSession,
    *,
    user_id: UUID,
    show_id: int,
    status: str,
) -> None:
    stmt = insert(UserShowWatch).values(
        user_id=user_id,
        show_id=show_id,
        status=status,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "show_id"],
        set_={"status": status, "updated_at": datetime.now(timezone.utc)},
    )
    await db.execute(stmt)


async def unset_user_show_status(
    db: AsyncSession,
    *,
    user_id: UUID,
    show_id: int,
) -> None:
    await db.execute(
        UserShowWatch.__table__.delete().where(
            and_(
                UserShowWatch.user_id == user_id,
                UserShowWatch.show_id == show_id,
            )
        )
    )


async def next_episode_for_user_show(
    db: AsyncSession,
    *,
    user_id: UUID,
    show_id: int,
) -> Episode | None:
    watched_subq = (
        select(UserEpisodeWatch.episode_id)
        .where(UserEpisodeWatch.user_id == user_id)
    ).subquery()
    stmt = (
        select(Episode)
        .where(Episode.show_id == show_id)
        .where(Episode.id.notin_(select(watched_subq)))
        .order_by(Episode.season.asc(), Episode.number.asc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_watchlist_for_user(
    db: AsyncSession,
    *,
    user_id: UUID,
    status_filter: ShowStatusFilter | None,
) -> list[WatchlistEntry]:
    base_stmt = (
        select(UserShowWatch, Show)
        .join(Show, Show.id == UserShowWatch.show_id)
        .where(UserShowWatch.user_id == user_id)
        .order_by(UserShowWatch.updated_at.desc())
    )
    if status_filter is not None and status_filter != "watched":
        base_stmt = base_stmt.where(UserShowWatch.status == status_filter)

    rows = (await db.execute(base_stmt)).all()
    if not rows:
        return []

    show_ids = [show.id for _watch, show in rows]

    total_counts_stmt = (
        select(Episode.show_id, func.count(Episode.id))
        .where(Episode.show_id.in_(show_ids))
        .group_by(Episode.show_id)
    )
    total_counts = {sid: c for sid, c in (await db.execute(total_counts_stmt)).all()}

    watched_counts_stmt = (
        select(Episode.show_id, func.count(UserEpisodeWatch.episode_id))
        .join(UserEpisodeWatch, UserEpisodeWatch.episode_id == Episode.id)
        .where(
            Episode.show_id.in_(show_ids),
            UserEpisodeWatch.user_id == user_id,
        )
        .group_by(Episode.show_id)
    )
    watched_counts = {sid: c for sid, c in (await db.execute(watched_counts_stmt)).all()}

    entries: list[WatchlistEntry] = []
    for watch, show in rows:
        total = total_counts.get(show.id, 0)
        watched = watched_counts.get(show.id, 0)
        is_watched = total > 0 and watched >= total

        derived_status: ShowStatusFilter
        if watch.status == "dropped":
            derived_status = "dropped"
        elif is_watched and watch.status in ("watching", "want_to_watch"):
            derived_status = "watched"
        else:
            derived_status = watch.status  # type: ignore[assignment]

        if status_filter == "watched" and derived_status != "watched":
            continue
        if status_filter == "watching" and derived_status != "watching":
            continue
        if status_filter == "want_to_watch" and derived_status != "want_to_watch":
            continue

        next_ep = await next_episode_for_user_show(db, user_id=user_id, show_id=show.id)
        next_ep_out = (
            EpisodeOut(
                id=next_ep.id,
                show_id=next_ep.show_id,
                season=next_ep.season,
                number=next_ep.number,
                name=next_ep.name,
                airdate=next_ep.airdate,
                airtime=next_ep.airtime,
                runtime=next_ep.runtime,
                summary=next_ep.summary,
            )
            if next_ep is not None
            else None
        )
        entries.append(
            WatchlistEntry(
                show=build_show_summary(show),
                status=derived_status,
                watched_episode_count=watched,
                total_episode_count=total,
                next_episode=next_ep_out,
                updated_at=watch.updated_at,
            )
        )
    return entries
```

Note: `EpisodeOut` and `build_show_summary` come from the existing `tvbf.tvmaze.dto`. Check that `EpisodeOut` exists there with the fields shown; if any field name differs (e.g., `airdate` is named `air_date`), align both the DTO mapping and the test.

- [ ] **Step 4: Run tests, verify pass**

Run: `task test -- tests/test_watchlist_queries.py -v`
Expected: 6 tests pass.

If `EpisodeOut` is missing fields the snippet uses, add them to `tvbf/tvmaze/dto.py` first or trim the mapping above. Mismatched field names will fail at import or first call, not at test-discovery time.

---

### Task 16: `/me/shows` endpoints

**Files:**
- Modify: `tvbf-backend/src/tvbf/routers/me.py`
- Modify: `tvbf-backend/tests/test_me_routes.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_me_routes.py`:

```python
from tvbf.tvmaze.models import Episode, Show


async def _seed_show(session, *, show_id: int, name: str = "S", episodes: int = 2) -> Show:
    show = Show(id=show_id, name=name, tvmaze_updated=1)
    session.add(show)
    await session.flush()
    for i in range(1, episodes + 1):
        session.add(Episode(id=show_id * 100 + i, show_id=show.id, season=1, number=i))
    await session.flush()
    return show


@pytest.mark.asyncio
async def test_put_show_status_creates_entry(authed_client, session):
    show = await _seed_show(session, show_id=920001)
    await session.commit()

    r = await authed_client.put(
        f"/me/shows/{show.id}",
        json={"status": "watching"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "watching"
    assert body["show"]["id"] == show.id


@pytest.mark.asyncio
async def test_put_show_status_idempotent(authed_client, session):
    show = await _seed_show(session, show_id=920002)
    await session.commit()
    r1 = await authed_client.put(f"/me/shows/{show.id}", json={"status": "watching"})
    r2 = await authed_client.put(f"/me/shows/{show.id}", json={"status": "dropped"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    r3 = await authed_client.get("/me/shows")
    assert len(r3.json()) == 1


@pytest.mark.asyncio
async def test_put_show_status_404_for_unknown_show(authed_client):
    r = await authed_client.put("/me/shows/999999999", json={"status": "watching"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_put_show_status_422_for_invalid_status(authed_client, session):
    show = await _seed_show(session, show_id=920003)
    await session.commit()
    r = await authed_client.put(f"/me/shows/{show.id}", json={"status": "watched"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_get_my_shows_filters_by_status(authed_client, session):
    a = await _seed_show(session, show_id=920004, name="A")
    b = await _seed_show(session, show_id=920005, name="B")
    await session.commit()
    await authed_client.put(f"/me/shows/{a.id}", json={"status": "watching"})
    await authed_client.put(f"/me/shows/{b.id}", json={"status": "want_to_watch"})

    r = await authed_client.get("/me/shows?status=watching")
    body = r.json()
    assert len(body) == 1
    assert body[0]["show"]["id"] == a.id


@pytest.mark.asyncio
async def test_delete_show_preserves_episode_history(authed_client, session):
    show = await _seed_show(session, show_id=920006)
    await session.commit()
    await authed_client.put(f"/me/shows/{show.id}", json={"status": "watching"})
    # Mark an episode watched.
    await authed_client.post(f"/me/episodes/{show.id * 100 + 1}/watched")

    r = await authed_client.request("DELETE", f"/me/shows/{show.id}")
    assert r.status_code == 204

    r = await authed_client.get("/me/shows")
    assert r.json() == []

    # Re-add and verify episode count survived.
    await authed_client.put(f"/me/shows/{show.id}", json={"status": "watching"})
    r = await authed_client.get("/me/shows")
    body = r.json()
    assert body[0]["watched_episode_count"] == 1
```

- [ ] **Step 2: Run, verify failures**

Run: `task test -- tests/test_me_routes.py -v`
Expected: 404s on `/me/shows*`.

- [ ] **Step 3: Implement the routes**

Append to `src/tvbf/routers/me.py`:

```python
from typing import Annotated

from fastapi import Path, Query

from tvbf.app.dto import (
    ShowStatusFilter,
    StatusUpdateRequest,
    WatchlistEntry,
)
from tvbf.app.watchlist import (
    list_watchlist_for_user,
    set_user_show_status,
    unset_user_show_status,
)
from tvbf.tvmaze.models import Show as TvmazeShow


@router.get("/me/shows", response_model=list[WatchlistEntry])
async def list_my_shows(
    status_filter: Annotated[ShowStatusFilter | None, Query(alias="status")] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[WatchlistEntry]:
    return await list_watchlist_for_user(db, user_id=user.id, status_filter=status_filter)


@router.put(
    "/me/shows/{show_id}",
    response_model=WatchlistEntry,
    dependencies=[Depends(require_csrf)],
)
async def put_show_status(
    show_id: Annotated[int, Path()],
    payload: StatusUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> WatchlistEntry:
    show = await db.get(TvmazeShow, show_id)
    if show is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    await set_user_show_status(
        db, user_id=user.id, show_id=show_id, status=payload.status
    )
    await db.commit()
    entries = await list_watchlist_for_user(
        db, user_id=user.id, status_filter=None
    )
    for e in entries:
        if e.show.id == show_id:
            return e
    # Should not happen, but guard:
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="watchlist_inconsistent"
    )


@router.delete(
    "/me/shows/{show_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def delete_show_from_my_list(
    show_id: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    await unset_user_show_status(db, user_id=user.id, show_id=show_id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `task test -- tests/test_me_routes.py -v`
Expected: all `/me/shows*` tests pass. (`test_delete_show_preserves_episode_history` also exercises the `/me/episodes/...` route added in Task 17 — if you run only this task's tests in isolation, that one will fail until Task 17 is done.)

---

### Task 17: `/me/episodes/{id}/watched` endpoints

**Files:**
- Modify: `tvbf-backend/src/tvbf/routers/me.py`
- Modify: `tvbf-backend/tests/test_me_routes.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_me_routes.py`:

```python
@pytest.mark.asyncio
async def test_mark_episode_watched(authed_client, session):
    show = await _seed_show(session, show_id=921001)
    await session.commit()
    ep_id = 921001 * 100 + 1
    r = await authed_client.post(f"/me/episodes/{ep_id}/watched")
    assert r.status_code == 201
    assert r.json()["episode_id"] == ep_id


@pytest.mark.asyncio
async def test_mark_episode_watched_idempotent(authed_client, session):
    show = await _seed_show(session, show_id=921002)
    await session.commit()
    ep_id = 921002 * 100 + 1
    await authed_client.post(f"/me/episodes/{ep_id}/watched")
    r = await authed_client.post(f"/me/episodes/{ep_id}/watched")
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_unmark_episode_watched(authed_client, session):
    show = await _seed_show(session, show_id=921003)
    await session.commit()
    ep_id = 921003 * 100 + 1
    await authed_client.post(f"/me/episodes/{ep_id}/watched")
    r = await authed_client.request("DELETE", f"/me/episodes/{ep_id}/watched")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_mark_unknown_episode_404(authed_client):
    r = await authed_client.post("/me/episodes/999999999/watched")
    assert r.status_code == 404
```

- [ ] **Step 2: Run, verify failures**

Run: `task test -- tests/test_me_routes.py::test_mark_episode_watched -v`
Expected: 404 (route not yet defined).

- [ ] **Step 3: Implement the routes**

Append to `src/tvbf/routers/me.py`:

```python
from datetime import datetime, timezone

from sqlalchemy import and_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from tvbf.app.dto import EpisodeWatchOut
from tvbf.app.models import UserEpisodeWatch
from tvbf.tvmaze.models import Episode as TvmazeEpisode


@router.post(
    "/me/episodes/{episode_id}/watched",
    status_code=status.HTTP_201_CREATED,
    response_model=EpisodeWatchOut,
    dependencies=[Depends(require_csrf)],
)
async def mark_episode_watched(
    episode_id: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> EpisodeWatchOut:
    ep = await db.get(TvmazeEpisode, episode_id)
    if ep is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    now = datetime.now(timezone.utc)
    stmt = pg_insert(UserEpisodeWatch).values(
        user_id=user.id, episode_id=episode_id, watched_at=now
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "episode_id"])
    await db.execute(stmt)
    await db.commit()
    # Fetch the actual stored watched_at (may differ from `now` on duplicate).
    existing = await db.get(
        UserEpisodeWatch,
        (user.id, episode_id),
        populate_existing=True,
    )
    return EpisodeWatchOut(
        episode_id=episode_id,
        watched_at=existing.watched_at if existing else now,
    )


@router.delete(
    "/me/episodes/{episode_id}/watched",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def unmark_episode_watched(
    episode_id: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    await db.execute(
        UserEpisodeWatch.__table__.delete().where(
            and_(
                UserEpisodeWatch.user_id == user.id,
                UserEpisodeWatch.episode_id == episode_id,
            )
        )
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `task test -- tests/test_me_routes.py -v`
Expected: all me tests pass.

---

### Task 18: Bulk season mark/unmark

**Files:**
- Modify: `tvbf-backend/src/tvbf/routers/me.py`
- Modify: `tvbf-backend/tests/test_me_routes.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_me_routes.py`:

```python
async def _seed_show_with_seasons(session, *, show_id: int, seasons: dict[int, int]) -> Show:
    show = Show(id=show_id, name=f"Show{show_id}", tvmaze_updated=1)
    session.add(show)
    await session.flush()
    ep_id = show_id * 1000
    for season, count in seasons.items():
        for n in range(1, count + 1):
            ep_id += 1
            session.add(Episode(id=ep_id, show_id=show.id, season=season, number=n))
    await session.flush()
    return show


@pytest.mark.asyncio
async def test_bulk_mark_season(authed_client, session):
    show = await _seed_show_with_seasons(session, show_id=922001, seasons={1: 3, 2: 2})
    await session.commit()
    r = await authed_client.post(f"/me/shows/{show.id}/season/1/watched")
    assert r.status_code == 201
    assert r.json()["marked"] == 3

    list_r = await authed_client.get("/me/shows")
    # Even without an explicit watchlist add, episode tracking is independent.
    # The show won't appear in /me/shows (no user_show_watch row) yet.
    assert list_r.json() == []


@pytest.mark.asyncio
async def test_bulk_unmark_season(authed_client, session):
    show = await _seed_show_with_seasons(session, show_id=922002, seasons={1: 3})
    await session.commit()
    await authed_client.post(f"/me/shows/{show.id}/season/1/watched")
    r = await authed_client.request("DELETE", f"/me/shows/{show.id}/season/1/watched")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_bulk_mark_404_for_unknown_season(authed_client, session):
    show = await _seed_show_with_seasons(session, show_id=922003, seasons={1: 1})
    await session.commit()
    r = await authed_client.post(f"/me/shows/{show.id}/season/99/watched")
    assert r.status_code == 404
```

- [ ] **Step 2: Run, verify failures**

Run: `task test -- tests/test_me_routes.py::test_bulk_mark_season -v`
Expected: 404.

- [ ] **Step 3: Implement bulk endpoints**

Append to `src/tvbf/routers/me.py`:

```python
from sqlalchemy import select as sa_select

from tvbf.app.dto import BulkSeasonResult


@router.post(
    "/me/shows/{show_id}/season/{season_number}/watched",
    status_code=status.HTTP_201_CREATED,
    response_model=BulkSeasonResult,
    dependencies=[Depends(require_csrf)],
)
async def bulk_mark_season(
    show_id: Annotated[int, Path()],
    season_number: Annotated[int, Path(alias="season_number")],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> BulkSeasonResult:
    ep_ids = (
        await db.execute(
            sa_select(TvmazeEpisode.id).where(
                TvmazeEpisode.show_id == show_id,
                TvmazeEpisode.season == season_number,
            )
        )
    ).scalars().all()
    if not ep_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    now = datetime.now(timezone.utc)
    rows = [
        {"user_id": user.id, "episode_id": ep_id, "watched_at": now} for ep_id in ep_ids
    ]
    stmt = pg_insert(UserEpisodeWatch).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "episode_id"])
    await db.execute(stmt)
    await db.commit()
    return BulkSeasonResult(marked=len(ep_ids))


@router.delete(
    "/me/shows/{show_id}/season/{season_number}/watched",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def bulk_unmark_season(
    show_id: Annotated[int, Path()],
    season_number: Annotated[int, Path(alias="season_number")],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    ep_ids = (
        await db.execute(
            sa_select(TvmazeEpisode.id).where(
                TvmazeEpisode.show_id == show_id,
                TvmazeEpisode.season == season_number,
            )
        )
    ).scalars().all()
    if not ep_ids:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    await db.execute(
        UserEpisodeWatch.__table__.delete().where(
            and_(
                UserEpisodeWatch.user_id == user.id,
                UserEpisodeWatch.episode_id.in_(ep_ids),
            )
        )
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `task test -- tests/test_me_routes.py -v`
Expected: all me tests pass.

- [ ] **Step 5: Run full backend suite**

Run: `task test`
Expected: full suite passes.

- [ ] **Step 6: Lint + typecheck**

Run: `task lint && task typecheck`
Expected: clean.

---

## Frontend

### Task 19: API client — `apiFetch` wrapper + types

**Files:**
- Modify: `tvbf-frontend/src/api/client.ts`
- Modify: `tvbf-frontend/src/api/types.ts`
- Modify: `tvbf-frontend/src/api/client.test.ts`

- [ ] **Step 1: Inspect the current client**

Read `tvbf-frontend/src/api/client.ts` to see the existing `apiFetch` (or whatever it's called) and `ApiError`. The browse code already does fetches — adapt rather than replace.

- [ ] **Step 2: Add types for new entities**

Append to `src/api/types.ts`:

```ts
export type ShowStatus = "watching" | "want_to_watch" | "dropped";
export type ShowStatusFilter = ShowStatus | "watched";

export interface User {
  id: string;
  email: string;
  display_name: string;
  created_at: string;
}

export interface WatchlistEntry {
  show: ShowSummary; // existing type, re-used
  status: ShowStatusFilter;
  watched_episode_count: number;
  total_episode_count: number;
  next_episode: EpisodeSummary | null; // existing type
  updated_at: string;
}

export interface EpisodeWatchOut {
  episode_id: number;
  watched_at: string;
}
```

If `ShowSummary` or `EpisodeSummary` are named differently in the existing `types.ts`, use the existing names instead of these.

- [ ] **Step 3: Update the fetch wrapper**

In `src/api/client.ts`, ensure the wrapper:

1. Always sets `credentials: 'include'`.
2. For non-`GET` requests, reads `csrf_token` from `document.cookie` and sets `X-CSRF-Token`.
3. Throws `ApiError` with status + parsed body on non-2xx.

If the existing wrapper isn't doing #1 and #2, modify it:

```ts
function readCookie(name: string): string | null {
  const match = document.cookie
    .split("; ")
    .find((row) => row.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.split("=").slice(1).join("=")) : null;
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  const method = (init.method ?? "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") {
    const csrf = readCookie("csrf_token");
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    method,
    credentials: "include",
    headers,
  });
  if (!res.ok) {
    let body: unknown;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    throw new ApiError(res.status, body);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
```

`API_BASE_URL` and `ApiError` are existing — re-use them.

- [ ] **Step 4: Add tests for the wrapper**

Append to `src/api/client.test.ts`:

```ts
import { describe, it, expect, beforeEach } from "vitest";
import { apiFetch, ApiError } from "./client";
import { server } from "../test/msw-server";
import { http, HttpResponse } from "msw";

describe("apiFetch", () => {
  beforeEach(() => {
    document.cookie = "";
  });

  it("includes credentials on every request", async () => {
    let observedCredentials: string | undefined;
    server.use(
      http.get("*/probe", ({ request }) => {
        observedCredentials = request.credentials;
        return HttpResponse.json({ ok: true });
      }),
    );
    await apiFetch("/probe");
    expect(observedCredentials).toBe("include");
  });

  it("attaches X-CSRF-Token from csrf_token cookie on POST", async () => {
    document.cookie = "csrf_token=abc123";
    let observedHeader: string | null = null;
    server.use(
      http.post("*/echo", ({ request }) => {
        observedHeader = request.headers.get("X-CSRF-Token");
        return HttpResponse.json({});
      }),
    );
    await apiFetch("/echo", { method: "POST", body: "{}" });
    expect(observedHeader).toBe("abc123");
  });

  it("throws ApiError with body on 4xx", async () => {
    server.use(
      http.post("*/fail", () => HttpResponse.json({ detail: "nope" }, { status: 401 })),
    );
    await expect(apiFetch("/fail", { method: "POST" })).rejects.toBeInstanceOf(ApiError);
  });
});
```

(Adjust import paths to match the existing test setup if they differ.)

- [ ] **Step 5: Run frontend tests**

Run: `task -d ../tvbf-frontend test -- src/api/client.test.ts`
Expected: 3 tests pass plus any pre-existing client tests.

- [ ] **Step 6: Typecheck**

Run: `task -d ../tvbf-frontend typecheck`
Expected: clean.

---

### Task 20: AuthContext + auth API hooks

**Files:**
- Create: `tvbf-frontend/src/api/auth.ts`
- Create: `tvbf-frontend/src/components/AuthContext.tsx`
- Create: `tvbf-frontend/src/components/AuthContext.test.tsx`
- Create: `tvbf-frontend/src/api/auth.test.ts`
- Modify: `tvbf-frontend/src/main.tsx`

- [ ] **Step 1: Write failing tests for `AuthContext`**

Create `src/components/AuthContext.test.tsx`:

```tsx
import { describe, it, expect } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { server } from "../test/msw-server";
import { AuthProvider, useAuth } from "./AuthContext";

function ProbeUser() {
  const { user, loading } = useAuth();
  if (loading) return <div>loading</div>;
  return <div>{user ? user.email : "anon"}</div>;
}

function renderWithProviders(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider>{node}</AuthProvider>
    </QueryClientProvider>,
  );
}

describe("AuthContext", () => {
  it("initial fetch populates user when /me returns 200", async () => {
    server.use(
      http.get("*/me", () =>
        HttpResponse.json({
          id: "u1",
          email: "a@b.com",
          display_name: "A",
          created_at: new Date().toISOString(),
        }),
      ),
    );
    renderWithProviders(<ProbeUser />);
    await waitFor(() => expect(screen.getByText("a@b.com")).toBeInTheDocument());
  });

  it("treats 401 as anonymous", async () => {
    server.use(http.get("*/me", () => HttpResponse.json({ detail: "auth_required" }, { status: 401 })));
    renderWithProviders(<ProbeUser />);
    await waitFor(() => expect(screen.getByText("anon")).toBeInTheDocument());
  });
});
```

- [ ] **Step 2: Run, verify failures**

Run: `task -d ../tvbf-frontend test -- src/components/AuthContext.test.tsx`
Expected: ImportError on `./AuthContext`.

- [ ] **Step 3: Implement the auth API module**

Create `src/api/auth.ts`:

```ts
import { apiFetch } from "./client";
import type { User } from "./types";

export const me = () => apiFetch<User>("/me");

export const signup = (body: { email: string; password: string; display_name: string }) =>
  apiFetch<User>("/auth/signup", { method: "POST", body: JSON.stringify(body) });

export const login = (body: { email: string; password: string }) =>
  apiFetch<User>("/auth/login", { method: "POST", body: JSON.stringify(body) });

export const logout = () => apiFetch<void>("/auth/logout", { method: "POST" });

export const changePassword = (body: { current_password: string; new_password: string }) =>
  apiFetch<void>("/auth/password", { method: "POST", body: JSON.stringify(body) });

export const deleteAccount = (body: { password: string }) =>
  apiFetch<void>("/me", { method: "DELETE", body: JSON.stringify(body) });
```

- [ ] **Step 4: Implement `AuthContext`**

Create `src/components/AuthContext.tsx`:

```tsx
import { createContext, useCallback, useContext, useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as authApi from "../api/auth";
import { ApiError } from "../api/client";
import type { User } from "../api/types";

type AuthContextValue = {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (email: string, password: string, displayName: string) => Promise<void>;
  logout: () => Promise<void>;
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>;
  deleteAccount: (password: string) => Promise<void>;
  refresh: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const qc = useQueryClient();

  const meQuery = useQuery<User | null>({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        return await authApi.me();
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }
    },
    staleTime: 60_000,
  });

  const refresh = useCallback(async () => {
    await qc.invalidateQueries({ queryKey: ["me"] });
  }, [qc]);

  const loginMut = useMutation({
    mutationFn: (vars: { email: string; password: string }) => authApi.login(vars),
    onSuccess: (user) => qc.setQueryData(["me"], user),
  });
  const signupMut = useMutation({
    mutationFn: (vars: { email: string; password: string; display_name: string }) =>
      authApi.signup(vars),
    onSuccess: (user) => qc.setQueryData(["me"], user),
  });
  const logoutMut = useMutation({
    mutationFn: () => authApi.logout(),
    onSuccess: () => {
      qc.setQueryData(["me"], null);
      qc.removeQueries({ queryKey: ["my-shows"] });
    },
  });
  const changePwMut = useMutation({
    mutationFn: (vars: { current_password: string; new_password: string }) =>
      authApi.changePassword(vars),
    onSuccess: () => refresh(),
  });
  const deleteMut = useMutation({
    mutationFn: (vars: { password: string }) => authApi.deleteAccount(vars),
    onSuccess: () => {
      qc.setQueryData(["me"], null);
      qc.clear();
    },
  });

  const value = useMemo<AuthContextValue>(
    () => ({
      user: meQuery.data ?? null,
      loading: meQuery.isLoading,
      login: async (email, password) => {
        await loginMut.mutateAsync({ email, password });
      },
      signup: async (email, password, displayName) => {
        await signupMut.mutateAsync({ email, password, display_name: displayName });
      },
      logout: async () => {
        await logoutMut.mutateAsync();
      },
      changePassword: async (cur, next) => {
        await changePwMut.mutateAsync({ current_password: cur, new_password: next });
      },
      deleteAccount: async (password) => {
        await deleteMut.mutateAsync({ password });
      },
      refresh,
    }),
    [
      meQuery.data,
      meQuery.isLoading,
      loginMut,
      signupMut,
      logoutMut,
      changePwMut,
      deleteMut,
      refresh,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
```

- [ ] **Step 5: Wrap the app in `AuthProvider`**

In `src/main.tsx`, wrap whatever currently is wrapped by `QueryClientProvider`:

```tsx
import { AuthProvider } from "./components/AuthContext";
// ...
<QueryClientProvider client={queryClient}>
  <AuthProvider>
    <RouterProvider router={router} />
  </AuthProvider>
</QueryClientProvider>
```

- [ ] **Step 6: Run tests, verify pass**

Run: `task -d ../tvbf-frontend test -- src/components/AuthContext.test.tsx`
Expected: 2 tests pass.

---

### Task 21: Login page

**Files:**
- Create: `tvbf-frontend/src/pages/LoginPage.tsx`
- Create: `tvbf-frontend/src/pages/LoginPage.test.tsx`
- Modify: `tvbf-frontend/src/router.tsx`

- [ ] **Step 1: Write failing test**

Create `src/pages/LoginPage.test.tsx`:

```tsx
import { describe, it, expect } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { server } from "../test/msw-server";
import { AuthProvider } from "../components/AuthContext";
import { LoginPage } from "./LoginPage";

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/my-list" element={<div>my list page</div>} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("LoginPage", () => {
  it("logs in and redirects to /my-list", async () => {
    server.use(
      http.get("*/me", () => HttpResponse.json({ detail: "auth_required" }, { status: 401 })),
      http.post("*/auth/login", () =>
        HttpResponse.json({
          id: "u1",
          email: "a@b.com",
          display_name: "A",
          created_at: new Date().toISOString(),
        }),
      ),
    );
    renderAt("/login?next=/my-list");
    await userEvent.type(screen.getByLabelText(/email/i), "a@b.com");
    await userEvent.type(screen.getByLabelText(/password/i), "hunter2hunter2");
    await userEvent.click(screen.getByRole("button", { name: /log in/i }));
    await waitFor(() => expect(screen.getByText("my list page")).toBeInTheDocument());
  });

  it("shows an error message on invalid credentials", async () => {
    server.use(
      http.get("*/me", () => HttpResponse.json({ detail: "auth_required" }, { status: 401 })),
      http.post("*/auth/login", () =>
        HttpResponse.json({ detail: "invalid_credentials" }, { status: 401 }),
      ),
    );
    renderAt("/login");
    await userEvent.type(screen.getByLabelText(/email/i), "a@b.com");
    await userEvent.type(screen.getByLabelText(/password/i), "wrong");
    await userEvent.click(screen.getByRole("button", { name: /log in/i }));
    await waitFor(() => expect(screen.getByText(/incorrect/i)).toBeInTheDocument());
  });
});
```

- [ ] **Step 2: Run, verify failures**

Run: `task -d ../tvbf-frontend test -- src/pages/LoginPage.test.tsx`
Expected: ImportError.

- [ ] **Step 3: Implement `LoginPage`**

Create `src/pages/LoginPage.tsx`:

```tsx
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "../components/AuthContext";
import { ApiError } from "../api/client";

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
      const next = params.get("next") || "/my-list";
      navigate(next);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("Email or password is incorrect.");
      } else {
        setError("Something went wrong. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto max-w-sm py-12">
      <h1 className="text-2xl font-semibold mb-6">Log in</h1>
      <form onSubmit={onSubmit} className="space-y-4">
        <div>
          <label htmlFor="email" className="block text-sm">Email</label>
          <input
            id="email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            className="mt-1 w-full rounded border px-3 py-2"
          />
        </div>
        <div>
          <label htmlFor="password" className="block text-sm">Password</label>
          <input
            id="password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            className="mt-1 w-full rounded border px-3 py-2"
          />
        </div>
        {error && <p className="text-sm text-red-600">{error}</p>}
        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded bg-black text-white py-2 disabled:opacity-50"
        >
          {submitting ? "Logging in…" : "Log in"}
        </button>
      </form>
      <p className="mt-4 text-sm">
        New here? <Link to="/signup" className="underline">Sign up</Link>
      </p>
    </div>
  );
}
```

(The styling assumes Tailwind. Match the existing `LoginPage` aesthetic if your codebase has shadcn/ui `Button` / `Input` components in use; replace the raw `<input>` and `<button>` with those.)

- [ ] **Step 4: Add the route**

In `src/router.tsx`, add `/login`:

```tsx
{ path: "/login", element: <LoginPage /> },
```

(import `LoginPage` at the top.)

- [ ] **Step 5: Run tests, verify pass**

Run: `task -d ../tvbf-frontend test -- src/pages/LoginPage.test.tsx`
Expected: 2 tests pass.

---

### Task 22: Signup page

**Files:**
- Create: `tvbf-frontend/src/pages/SignupPage.tsx`
- Create: `tvbf-frontend/src/pages/SignupPage.test.tsx`
- Modify: `tvbf-frontend/src/router.tsx`

- [ ] **Step 1: Write failing test**

Create `src/pages/SignupPage.test.tsx` mirroring the LoginPage test pattern. Tests:

- Submitting valid signup → redirects to `/my-list`.
- 409 (`email_in_use`) → shows "This email is already registered."
- Validation: short password shows inline error before request.

```tsx
import { describe, it, expect } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { server } from "../test/msw-server";
import { AuthProvider } from "../components/AuthContext";
import { SignupPage } from "./SignupPage";

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/signup" element={<SignupPage />} />
            <Route path="/my-list" element={<div>my list page</div>} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("SignupPage", () => {
  it("creates an account and redirects", async () => {
    server.use(
      http.get("*/me", () => HttpResponse.json({ detail: "auth_required" }, { status: 401 })),
      http.post("*/auth/signup", () =>
        HttpResponse.json(
          {
            id: "u1",
            email: "x@y.com",
            display_name: "X",
            created_at: new Date().toISOString(),
          },
          { status: 201 },
        ),
      ),
    );
    renderAt("/signup");
    await userEvent.type(screen.getByLabelText(/email/i), "x@y.com");
    await userEvent.type(screen.getByLabelText(/display name/i), "X");
    await userEvent.type(screen.getByLabelText(/password/i), "hunter2hunter2");
    await userEvent.click(screen.getByRole("button", { name: /sign up/i }));
    await waitFor(() => expect(screen.getByText("my list page")).toBeInTheDocument());
  });

  it("surfaces email_in_use", async () => {
    server.use(
      http.get("*/me", () => HttpResponse.json({ detail: "auth_required" }, { status: 401 })),
      http.post("*/auth/signup", () =>
        HttpResponse.json({ detail: "email_in_use" }, { status: 409 }),
      ),
    );
    renderAt("/signup");
    await userEvent.type(screen.getByLabelText(/email/i), "x@y.com");
    await userEvent.type(screen.getByLabelText(/display name/i), "X");
    await userEvent.type(screen.getByLabelText(/password/i), "hunter2hunter2");
    await userEvent.click(screen.getByRole("button", { name: /sign up/i }));
    await waitFor(() => expect(screen.getByText(/already registered/i)).toBeInTheDocument());
  });
});
```

- [ ] **Step 2: Implement `SignupPage`**

Create `src/pages/SignupPage.tsx`:

```tsx
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../components/AuthContext";
import { ApiError } from "../api/client";

export function SignupPage() {
  const { signup } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    setSubmitting(true);
    try {
      await signup(email, password, displayName);
      navigate("/my-list");
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError("This email is already registered.");
      } else if (err instanceof ApiError && err.status === 422) {
        setError("Please check your input and try again.");
      } else {
        setError("Something went wrong. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto max-w-sm py-12">
      <h1 className="text-2xl font-semibold mb-6">Sign up</h1>
      <form onSubmit={onSubmit} className="space-y-4">
        <div>
          <label htmlFor="email" className="block text-sm">Email</label>
          <input
            id="email" type="email" required
            value={email} onChange={(e) => setEmail(e.target.value)}
            className="mt-1 w-full rounded border px-3 py-2"
          />
        </div>
        <div>
          <label htmlFor="display_name" className="block text-sm">Display name</label>
          <input
            id="display_name" type="text" required maxLength={100}
            value={displayName} onChange={(e) => setDisplayName(e.target.value)}
            className="mt-1 w-full rounded border px-3 py-2"
          />
        </div>
        <div>
          <label htmlFor="password" className="block text-sm">Password</label>
          <input
            id="password" type="password" required minLength={8}
            value={password} onChange={(e) => setPassword(e.target.value)}
            className="mt-1 w-full rounded border px-3 py-2"
          />
          <p className="text-xs text-gray-500 mt-1">At least 8 characters.</p>
        </div>
        {error && <p className="text-sm text-red-600">{error}</p>}
        <button type="submit" disabled={submitting}
          className="w-full rounded bg-black text-white py-2 disabled:opacity-50">
          {submitting ? "Creating account…" : "Sign up"}
        </button>
      </form>
      <p className="mt-4 text-sm">
        Already have an account? <Link to="/login" className="underline">Log in</Link>
      </p>
    </div>
  );
}
```

- [ ] **Step 3: Add the route**

In `src/router.tsx`, add `/signup`:

```tsx
{ path: "/signup", element: <SignupPage /> },
```

- [ ] **Step 4: Run tests, verify pass**

Run: `task -d ../tvbf-frontend test -- src/pages/SignupPage.test.tsx`
Expected: 2 tests pass.

---

### Task 23: `RequireAuth` route guard + Header authed/unauthed

**Files:**
- Create: `tvbf-frontend/src/components/RequireAuth.tsx`
- Create: `tvbf-frontend/src/components/RequireAuth.test.tsx`
- Modify: `tvbf-frontend/src/components/AppShell.tsx`

- [ ] **Step 1: Write failing tests**

Create `src/components/RequireAuth.test.tsx`:

```tsx
import { describe, it, expect } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { server } from "../test/msw-server";
import { AuthProvider } from "./AuthContext";
import { RequireAuth } from "./RequireAuth";

function setup(initial = "/secret") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <MemoryRouter initialEntries={[initial]}>
          <Routes>
            <Route element={<RequireAuth />}>
              <Route path="/secret" element={<div>secret content</div>} />
            </Route>
            <Route path="/login" element={<div>login page</div>} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("RequireAuth", () => {
  it("redirects to /login when unauthenticated", async () => {
    server.use(http.get("*/me", () => HttpResponse.json({ detail: "auth_required" }, { status: 401 })));
    setup();
    await waitFor(() => expect(screen.getByText("login page")).toBeInTheDocument());
  });

  it("renders children when authenticated", async () => {
    server.use(
      http.get("*/me", () =>
        HttpResponse.json({
          id: "u1",
          email: "a@b.com",
          display_name: "A",
          created_at: new Date().toISOString(),
        }),
      ),
    );
    setup();
    await waitFor(() => expect(screen.getByText("secret content")).toBeInTheDocument());
  });
});
```

- [ ] **Step 2: Implement `RequireAuth`**

Create `src/components/RequireAuth.tsx`:

```tsx
import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "./AuthContext";

export function RequireAuth() {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return null;
  if (!user) {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?next=${next}`} replace />;
  }
  return <Outlet />;
}
```

- [ ] **Step 3: Update `AppShell` header**

Open `src/components/AppShell.tsx`. In the header, replace the static nav with a conditional. Add at the top:

```tsx
import { useAuth } from "./AuthContext";
import { UserMenu } from "./UserMenu";
import { Link } from "react-router-dom";
```

In the header JSX, where current nav lives:

```tsx
const { user } = useAuth();
// ...
<nav className="flex items-center gap-4">
  {user ? (
    <>
      <Link to="/my-list">My List</Link>
      <UserMenu />
    </>
  ) : (
    <>
      <Link to="/login">Log in</Link>
      <Link to="/signup">Sign up</Link>
    </>
  )}
</nav>
```

`UserMenu` is created in Task 24; for now, leave a placeholder export `export function UserMenu() { return null; }` if you want this task to compile in isolation. Otherwise, save the AppShell change for the end of Task 24.

- [ ] **Step 4: Run RequireAuth tests**

Run: `task -d ../tvbf-frontend test -- src/components/RequireAuth.test.tsx`
Expected: 2 tests pass.

---

### Task 24: User menu — logout, change password, delete account

**Files:**
- Create: `tvbf-frontend/src/components/UserMenu.tsx`
- Create: `tvbf-frontend/src/components/ChangePasswordDialog.tsx`
- Create: `tvbf-frontend/src/components/DeleteAccountDialog.tsx`

- [ ] **Step 1: Implement `UserMenu`**

Create `src/components/UserMenu.tsx`:

```tsx
import { useState } from "react";
import { useAuth } from "./AuthContext";
import { ChangePasswordDialog } from "./ChangePasswordDialog";
import { DeleteAccountDialog } from "./DeleteAccountDialog";

export function UserMenu() {
  const { user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const [pwOpen, setPwOpen] = useState(false);
  const [delOpen, setDelOpen] = useState(false);
  if (!user) return null;
  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        className="px-3 py-1 rounded border"
      >
        {user.display_name} ▾
      </button>
      {open && (
        <ul role="menu" className="absolute right-0 mt-2 w-48 rounded border bg-white shadow">
          <li>
            <button role="menuitem" onClick={() => { setOpen(false); setPwOpen(true); }} className="w-full text-left px-3 py-2 hover:bg-gray-50">
              Change password
            </button>
          </li>
          <li>
            <button role="menuitem" onClick={() => { setOpen(false); setDelOpen(true); }} className="w-full text-left px-3 py-2 hover:bg-gray-50">
              Delete account
            </button>
          </li>
          <li>
            <button role="menuitem" onClick={async () => { setOpen(false); await logout(); }} className="w-full text-left px-3 py-2 hover:bg-gray-50">
              Log out
            </button>
          </li>
        </ul>
      )}
      <ChangePasswordDialog open={pwOpen} onClose={() => setPwOpen(false)} />
      <DeleteAccountDialog open={delOpen} onClose={() => setDelOpen(false)} />
    </div>
  );
}
```

- [ ] **Step 2: Implement `ChangePasswordDialog`**

Create `src/components/ChangePasswordDialog.tsx`:

```tsx
import { useState } from "react";
import { useAuth } from "./AuthContext";
import { ApiError } from "../api/client";

export function ChangePasswordDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { changePassword } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  if (!open) return null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (next.length < 8) { setErr("New password must be at least 8 characters."); return; }
    setSubmitting(true);
    try {
      await changePassword(current, next);
      onClose();
      setCurrent(""); setNext("");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setErr("Current password is incorrect.");
      else setErr("Something went wrong.");
    } finally { setSubmitting(false); }
  }

  return (
    <div role="dialog" aria-label="Change password" className="fixed inset-0 bg-black/30 flex items-center justify-center">
      <div className="bg-white rounded p-6 w-96">
        <h2 className="text-lg font-semibold mb-4">Change password</h2>
        <form onSubmit={submit} className="space-y-3">
          <input type="password" placeholder="Current password" required value={current} onChange={(e) => setCurrent(e.target.value)} className="w-full rounded border px-3 py-2" />
          <input type="password" placeholder="New password" required minLength={8} value={next} onChange={(e) => setNext(e.target.value)} className="w-full rounded border px-3 py-2" />
          {err && <p className="text-sm text-red-600">{err}</p>}
          <div className="flex gap-2 justify-end">
            <button type="button" onClick={onClose} className="px-3 py-1">Cancel</button>
            <button type="submit" disabled={submitting} className="rounded bg-black text-white px-3 py-1">
              {submitting ? "Saving…" : "Save"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Implement `DeleteAccountDialog`**

Create `src/components/DeleteAccountDialog.tsx`:

```tsx
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "./AuthContext";
import { ApiError } from "../api/client";

export function DeleteAccountDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { deleteAccount } = useAuth();
  const navigate = useNavigate();
  const [pw, setPw] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  if (!open) return null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setSubmitting(true);
    try {
      await deleteAccount(pw);
      onClose();
      navigate("/");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setErr("Password is incorrect.");
      else setErr("Something went wrong.");
    } finally { setSubmitting(false); }
  }

  return (
    <div role="dialog" aria-label="Delete account" className="fixed inset-0 bg-black/30 flex items-center justify-center">
      <div className="bg-white rounded p-6 w-96">
        <h2 className="text-lg font-semibold mb-2">Delete account</h2>
        <p className="text-sm text-gray-600 mb-4">
          This permanently deletes your account, your watchlist, and all watch history. This cannot be undone.
        </p>
        <form onSubmit={submit} className="space-y-3">
          <input type="password" placeholder="Confirm with your password" required value={pw} onChange={(e) => setPw(e.target.value)} className="w-full rounded border px-3 py-2" />
          {err && <p className="text-sm text-red-600">{err}</p>}
          <div className="flex gap-2 justify-end">
            <button type="button" onClick={onClose} className="px-3 py-1">Cancel</button>
            <button type="submit" disabled={submitting} className="rounded bg-red-600 text-white px-3 py-1">
              {submitting ? "Deleting…" : "Delete"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Smoke-test in the browser**

Run: `task up` (from `tvbf-backend/`) and `task -d ../tvbf-frontend up`. Visit `https://tvbf.localhost`, sign up, log out, log in, change password, log out, log in with new password, delete account.

- [ ] **Step 5: Verify lint + typecheck**

Run: `task -d ../tvbf-frontend lint && task -d ../tvbf-frontend typecheck`
Expected: clean.

---

### Task 25: My-list API hooks + watchlist mutations

**Files:**
- Create: `tvbf-frontend/src/api/me.ts`
- Create: `tvbf-frontend/src/api/me.test.ts`

- [ ] **Step 1: Implement the `me` API module**

Create `src/api/me.ts`:

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "./client";
import type { ShowStatus, ShowStatusFilter, WatchlistEntry, EpisodeWatchOut } from "./types";

const myShowsKey = (filter: ShowStatusFilter | undefined) =>
  filter ? ["my-shows", filter] : ["my-shows"];

export function useMyShows(filter?: ShowStatusFilter) {
  return useQuery<WatchlistEntry[]>({
    queryKey: myShowsKey(filter),
    queryFn: () =>
      apiFetch<WatchlistEntry[]>(`/me/shows${filter ? `?status=${filter}` : ""}`),
  });
}

export function useSetShowStatus() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { showId: number; status: ShowStatus }) =>
      apiFetch<WatchlistEntry>(`/me/shows/${vars.showId}`, {
        method: "PUT",
        body: JSON.stringify({ status: vars.status }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["my-shows"] }),
  });
}

export function useRemoveShow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (showId: number) =>
      apiFetch<void>(`/me/shows/${showId}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["my-shows"] }),
  });
}

export function useMarkEpisode() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (episodeId: number) =>
      apiFetch<EpisodeWatchOut>(`/me/episodes/${episodeId}/watched`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["my-shows"] }),
  });
}

export function useUnmarkEpisode() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (episodeId: number) =>
      apiFetch<void>(`/me/episodes/${episodeId}/watched`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["my-shows"] }),
  });
}

export function useMarkSeason() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { showId: number; season: number }) =>
      apiFetch<{ marked: number }>(
        `/me/shows/${vars.showId}/season/${vars.season}/watched`,
        { method: "POST" },
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["my-shows"] }),
  });
}

export function useUnmarkSeason() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { showId: number; season: number }) =>
      apiFetch<void>(
        `/me/shows/${vars.showId}/season/${vars.season}/watched`,
        { method: "DELETE" },
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["my-shows"] }),
  });
}
```

- [ ] **Step 2: Smoke-test types**

Run: `task -d ../tvbf-frontend typecheck`
Expected: clean.

---

### Task 26: My List page

**Files:**
- Create: `tvbf-frontend/src/pages/MyListPage.tsx`
- Create: `tvbf-frontend/src/pages/MyListPage.test.tsx`
- Modify: `tvbf-frontend/src/router.tsx`

- [ ] **Step 1: Write failing test**

Create `src/pages/MyListPage.test.tsx`:

```tsx
import { describe, it, expect } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { server } from "../test/msw-server";
import { AuthProvider } from "../components/AuthContext";
import { MyListPage } from "./MyListPage";

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <MemoryRouter initialEntries={["/my-list"]}>
          <Routes>
            <Route path="/my-list" element={<MyListPage />} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("MyListPage", () => {
  it("renders entries returned by /me/shows under the watching tab", async () => {
    server.use(
      http.get("*/me", () =>
        HttpResponse.json({
          id: "u1", email: "a@b.com", display_name: "A",
          created_at: new Date().toISOString(),
        }),
      ),
      http.get("*/me/shows", ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get("status") === "watching") {
          return HttpResponse.json([
            {
              show: { id: 1, name: "Some Show", image_medium: null },
              status: "watching",
              watched_episode_count: 1,
              total_episode_count: 5,
              next_episode: { id: 9, show_id: 1, season: 1, number: 2, name: "Ep 2", airdate: null, airtime: null, runtime: null, summary: null },
              updated_at: new Date().toISOString(),
            },
          ]);
        }
        return HttpResponse.json([]);
      }),
    );
    setup();
    await waitFor(() => expect(screen.getByText("Some Show")).toBeInTheDocument());
    expect(screen.getByText(/Next: S1E2/)).toBeInTheDocument();
  });

  it("switches tabs", async () => {
    server.use(
      http.get("*/me", () =>
        HttpResponse.json({ id: "u1", email: "a@b.com", display_name: "A", created_at: new Date().toISOString() }),
      ),
      http.get("*/me/shows", ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get("status") === "want_to_watch") {
          return HttpResponse.json([
            { show: { id: 2, name: "Wanted", image_medium: null }, status: "want_to_watch", watched_episode_count: 0, total_episode_count: 3, next_episode: null, updated_at: new Date().toISOString() },
          ]);
        }
        return HttpResponse.json([]);
      }),
    );
    setup();
    await userEvent.click(screen.getByRole("tab", { name: /want to watch/i }));
    await waitFor(() => expect(screen.getByText("Wanted")).toBeInTheDocument());
  });
});
```

- [ ] **Step 2: Implement `MyListPage`**

Create `src/pages/MyListPage.tsx`:

```tsx
import { useState } from "react";
import { Link } from "react-router-dom";
import { useMyShows } from "../api/me";
import type { ShowStatusFilter } from "../api/types";

const TABS: { key: ShowStatusFilter; label: string }[] = [
  { key: "watching", label: "Watching" },
  { key: "want_to_watch", label: "Want to watch" },
  { key: "watched", label: "Watched" },
  { key: "dropped", label: "Dropped" },
];

export function MyListPage() {
  const [tab, setTab] = useState<ShowStatusFilter>("watching");
  const { data, isLoading } = useMyShows(tab);
  return (
    <div className="mx-auto max-w-5xl py-8">
      <h1 className="text-2xl font-semibold mb-6">My List</h1>
      <div role="tablist" className="flex gap-2 mb-6 border-b">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            onClick={() => setTab(t.key)}
            className={`px-3 py-2 ${tab === t.key ? "border-b-2 border-black font-semibold" : "text-gray-500"}`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {isLoading && <p>Loading…</p>}
      {!isLoading && data && data.length === 0 && (
        <p className="text-gray-500">Nothing here yet.</p>
      )}
      {!isLoading && data && data.length > 0 && (
        <ul className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
          {data.map((entry) => (
            <li key={entry.show.id} className="border rounded p-3">
              <Link to={`/shows/${entry.show.id}`} className="block">
                {entry.show.image_medium && (
                  <img src={entry.show.image_medium} alt="" className="w-full aspect-[2/3] object-cover rounded mb-2" />
                )}
                <h3 className="font-semibold">{entry.show.name}</h3>
                <p className="text-xs text-gray-500">
                  {entry.watched_episode_count}/{entry.total_episode_count} watched
                </p>
                {entry.next_episode && (
                  <p className="text-xs mt-1">
                    Next: S{entry.next_episode.season}E{entry.next_episode.number}
                    {entry.next_episode.name ? ` — ${entry.next_episode.name}` : ""}
                  </p>
                )}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Add the route under `RequireAuth`**

In `src/router.tsx`:

```tsx
import { RequireAuth } from "./components/RequireAuth";
import { MyListPage } from "./pages/MyListPage";

// ...inside the route tree:
{
  element: <RequireAuth />,
  children: [
    { path: "/my-list", element: <MyListPage /> },
  ],
},
```

- [ ] **Step 4: Run tests, verify pass**

Run: `task -d ../tvbf-frontend test -- src/pages/MyListPage.test.tsx`
Expected: 2 tests pass.

---

### Task 27: Show detail — watchlist status control + next-episode card

**Files:**
- Create: `tvbf-frontend/src/components/WatchlistStatusSelect.tsx`
- Create: `tvbf-frontend/src/components/NextEpisodeCard.tsx`
- Modify: `tvbf-frontend/src/pages/ShowDetailPage.tsx`

- [ ] **Step 1: Implement `WatchlistStatusSelect`**

Create `src/components/WatchlistStatusSelect.tsx`:

```tsx
import { useAuth } from "./AuthContext";
import { useMyShows, useRemoveShow, useSetShowStatus } from "../api/me";
import type { ShowStatus } from "../api/types";

export function WatchlistStatusSelect({ showId }: { showId: number }) {
  const { user } = useAuth();
  const { data } = useMyShows();
  const setStatus = useSetShowStatus();
  const remove = useRemoveShow();

  if (!user) return null;
  const current = data?.find((e) => e.show.id === showId);

  function onChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const v = e.target.value;
    if (v === "remove") remove.mutate(showId);
    else if (v === "none") return;
    else setStatus.mutate({ showId, status: v as ShowStatus });
  }

  const value = current ? current.status : "none";
  return (
    <label className="inline-flex items-center gap-2">
      <span className="text-sm">Status:</span>
      <select
        aria-label="Watchlist status"
        value={value === "watched" ? "watching" : value}
        onChange={onChange}
        className="rounded border px-2 py-1"
      >
        <option value="none">— Not on list —</option>
        <option value="watching">Watching</option>
        <option value="want_to_watch">Want to watch</option>
        <option value="dropped">Dropped</option>
        {current && <option value="remove">Remove from list</option>}
      </select>
    </label>
  );
}
```

- [ ] **Step 2: Implement `NextEpisodeCard`**

Create `src/components/NextEpisodeCard.tsx`:

```tsx
import { useMyShows } from "../api/me";

export function NextEpisodeCard({ showId }: { showId: number }) {
  const { data } = useMyShows();
  const entry = data?.find((e) => e.show.id === showId);
  if (!entry || entry.status !== "watching" || !entry.next_episode) return null;
  const ep = entry.next_episode;
  return (
    <aside className="rounded border p-3 bg-amber-50">
      <h3 className="text-sm font-semibold">Next up</h3>
      <p className="text-sm">
        S{ep.season}E{ep.number}
        {ep.name ? ` — ${ep.name}` : ""}
      </p>
      {ep.airdate && <p className="text-xs text-gray-500">Aired {ep.airdate}</p>}
    </aside>
  );
}
```

- [ ] **Step 3: Integrate into `ShowDetailPage`**

In `src/pages/ShowDetailPage.tsx`, near the show header section, add:

```tsx
import { WatchlistStatusSelect } from "../components/WatchlistStatusSelect";
import { NextEpisodeCard } from "../components/NextEpisodeCard";

// inside the rendered detail, somewhere near the title:
<div className="flex items-center gap-4 my-4">
  <WatchlistStatusSelect showId={show.id} />
</div>
<NextEpisodeCard showId={show.id} />
```

(Adapt placement to match the current layout.)

- [ ] **Step 4: Smoke-test in browser**

Run: `task -d ../tvbf-frontend up` (and backend if not already up). Log in. Visit a show detail. Set status to Watching. Reload page — status persists. Switch to Want to watch. Remove. Confirm `My List` reflects each change.

---

### Task 28: Episode watch checkboxes + season bulk toggle

**Files:**
- Create: `tvbf-frontend/src/components/EpisodeWatchCheckbox.tsx`
- Create: `tvbf-frontend/src/components/SeasonWatchToggle.tsx`
- Modify: `tvbf-frontend/src/pages/EpisodesPage.tsx` (or wherever episode lists render)

- [ ] **Step 1: Implement `EpisodeWatchCheckbox`**

Create `src/components/EpisodeWatchCheckbox.tsx`:

```tsx
import { useState } from "react";
import { useAuth } from "./AuthContext";
import { useMarkEpisode, useUnmarkEpisode } from "../api/me";

export function EpisodeWatchCheckbox({
  episodeId,
  initiallyWatched,
}: {
  episodeId: number;
  initiallyWatched: boolean;
}) {
  const { user } = useAuth();
  const [checked, setChecked] = useState(initiallyWatched);
  const mark = useMarkEpisode();
  const unmark = useUnmarkEpisode();

  if (!user) return null;

  async function onToggle(e: React.ChangeEvent<HTMLInputElement>) {
    const next = e.target.checked;
    setChecked(next); // optimistic
    try {
      if (next) await mark.mutateAsync(episodeId);
      else await unmark.mutateAsync(episodeId);
    } catch {
      setChecked(!next); // rollback
    }
  }

  return (
    <label className="inline-flex items-center gap-2">
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        aria-label="Mark episode watched"
      />
      <span className="text-xs text-gray-500">Watched</span>
    </label>
  );
}
```

`initiallyWatched` is sourced by the caller — for now, derive it from a new field on the episode list response. Since the existing browse API doesn't return per-user watch state, the simplest path is to fetch the user's watched-episode IDs once via a new helper. **However** — to keep this milestone scoped — pass `initiallyWatched={false}` everywhere and let the optimistic toggle work. The page then refetches `my-shows` after each toggle so counts update; per-episode state across reload is restored when we add a `useMyEpisodeWatches(showId)` hook.

If you'd rather restore state across reload now, add a small helper:

```ts
// in src/api/me.ts
export function useWatchedEpisodes(showId: number) {
  return useQuery<number[]>({
    queryKey: ["watched-episodes", showId],
    queryFn: () =>
      apiFetch<{ episode_id: number }[]>(`/me/shows/${showId}/episodes/watched`).then(
        (rows) => rows.map((r) => r.episode_id),
      ),
  });
}
```

…and add a corresponding backend route `GET /me/shows/{show_id}/episodes/watched`. **This is out of scope for the spec as written.** Recommendation: ship this task with `initiallyWatched={false}` and add the read endpoint + hook as the first follow-up issue after merge. Document in `docs/superpowers/specs/2026-04-25-user-service-watchlist-design.md` "Out of scope" the read endpoint as deferred.

- [ ] **Step 2: Implement `SeasonWatchToggle`**

Create `src/components/SeasonWatchToggle.tsx`:

```tsx
import { useAuth } from "./AuthContext";
import { useMarkSeason, useUnmarkSeason } from "../api/me";

export function SeasonWatchToggle({ showId, season }: { showId: number; season: number }) {
  const { user } = useAuth();
  const mark = useMarkSeason();
  const unmark = useUnmarkSeason();
  if (!user) return null;
  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        onClick={() => mark.mutate({ showId, season })}
        className="text-xs underline"
      >
        Mark season watched
      </button>
      <button
        type="button"
        onClick={() => unmark.mutate({ showId, season })}
        className="text-xs underline"
      >
        Unmark season
      </button>
    </div>
  );
}
```

- [ ] **Step 3: Integrate into the episodes view**

In `src/pages/EpisodesPage.tsx` (or wherever per-season episode lists render), add a `SeasonWatchToggle` next to each season header and an `EpisodeWatchCheckbox` next to each episode row.

- [ ] **Step 4: Smoke-test in browser**

Mark a few episodes watched. Confirm:
- The watched/total count on the My List card increases.
- "Next episode" advances on the show detail page after marking the current next episode.
- "Mark season watched" updates the count to N/N for that season.

- [ ] **Step 5: Lint + typecheck + full test sweep**

Run:

```
task -d ../tvbf-frontend lint
task -d ../tvbf-frontend typecheck
task -d ../tvbf-frontend test
```

Expected: all clean / passing.

---

## Final verification

- [ ] **Backend full sweep**

Run:

```
cd tvbf-backend
task lint
task typecheck
task test
task coverage
```

Expected: lint + typecheck clean. Tests pass. Coverage report writes to `tvbf-backend/htmlcov/`. Spot-check that `app/auth.py`, `app/watchlist.py`, `routers/auth.py`, and `routers/me.py` have substantive coverage (>85%).

- [ ] **Frontend full sweep**

Run:

```
task -d ../tvbf-frontend lint
task -d ../tvbf-frontend typecheck
task -d ../tvbf-frontend test
```

Expected: all clean / passing.

- [ ] **Live smoke test**

Bring infra + both services up:

```
task infra:up           # if not running
task up                 # backend
task -d ../tvbf-frontend up
```

In a browser at `https://tvbf.localhost`:
- Sign up. Land on My List (empty).
- Browse to a show. Add to "Watching." See it appear in My List.
- Mark an episode watched. See count increment, Next Up update.
- Mark season watched. See count jump.
- Change password. Log out. Log back in with new password.
- Delete account.

If any step fails, fix-forward — the test suite should already cover most regressions.

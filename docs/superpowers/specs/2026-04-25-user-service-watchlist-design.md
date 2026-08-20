# User Service + Watchlist — Design Spec

- **Date**: 2026-04-25
- **Status**: Approved (pending implementation plan)
- **Predecessors**: [2026-04-19 browse-api](2026-04-19-browse-api-design.md), [2026-04-19 frontend-mvp](2026-04-19-frontend-mvp-design.md)

## Goal

Add user accounts and per-user TV tracking to TV Binge Friend. Users can sign up, log in, mark shows as `watching` / `want_to_watch` / `dropped`, mark individual episodes as watched, and see a "next episode" suggestion for each show they're watching. This unblocks every downstream feature (friends, social feed, recommendations) by establishing a user identity layer.

## Scope

### In scope

- Email + password signup, login, logout.
- Logged-in password change.
- Account deletion (cascades user data).
- Show-level watchlist with statuses `watching`, `want_to_watch`, `dropped`. `watched` is derived (all episodes seen).
- Per-episode watch tracking (one row per watched episode per user).
- "Next episode" computation per tracked show.
- Bulk "mark season watched / unwatched."
- Frontend pages: `/login`, `/signup`, `/my-list`. Show-detail page gains watchlist controls and episode checkboxes when authenticated.

### Out of scope

- Email verification.
- Password reset (forgot-password flow).
- Social / OAuth login.
- Friends, friend activity, social feed (subsequent milestone).
- Ratings, notes, custom lists.
- Calendar / "what's airing this week."
- Email notifications.
- Rate limiting on auth endpoints (deferred — pre-launch concern).

## Architecture

Single FastAPI service (`tvbf-backend`). The previously-reserved `app` schema in the `tvbf` Postgres database gets four new tables. Cross-schema foreign keys (`app.user_show_watch.show_id → tvmaze.show.id`, `app.user_episode_watch.episode_id → tvmaze.episode.id`) provide referential integrity. No new infrastructure: no separate user service, no cache server, no session store outside Postgres.

### Module map (new + changed)

```
src/tvbf/
  routers/
    auth.py                 # NEW: /auth/* (signup, login, logout, password)
    me.py                   # NEW: /me, /me/shows, /me/episodes, /me/shows/{id}/season/{n}/watched
  app/
    __init__.py
    models.py               # REPLACES placeholder: User, Session, UserShowWatch, UserEpisodeWatch
    auth.py                 # NEW: password hashing, session create/lookup/rotate/delete
    watchlist.py            # NEW: query helpers (list_watchlist, next_episode, set_status, ...)
    dto.py                  # NEW: response schemas (UserOut, WatchlistEntry, EpisodeOut)
  deps.py                   # CHANGED: add get_current_user, require_csrf
  main.py                   # CHANGED: CORS allow_credentials=True; mount new routers

tests/
  fixtures/
    users.py                # NEW: user + authenticated-client factories
  test_auth_routes.py       # NEW: signup/login/logout/password-change/account-delete
  test_me_routes.py         # NEW: watchlist + episode tracking
  test_app_models.py        # NEW: model-level invariants
  test_csrf.py              # NEW: CSRF middleware behavior
```

## Data model

All tables in the `app` schema. Two Postgres extensions are required: `citext` (case-insensitive email uniqueness) and `pgcrypto` (`gen_random_uuid()`). Both are added in the migration that introduces these tables.

```
app.user
  id              uuid          primary key default gen_random_uuid()
  email           citext        not null unique
  password_hash   text          not null
  display_name    text          not null
  created_at      timestamptz   not null default now()
  updated_at      timestamptz   not null default now()

app.session
  id              text          primary key                     -- 32 random bytes, base64url
  user_id         uuid          not null references app.user(id) on delete cascade
  created_at      timestamptz   not null default now()
  last_seen_at    timestamptz   not null default now()
  expires_at      timestamptz   not null
  user_agent      text
  ip              inet
  index on (user_id)
  index on (expires_at)

app.user_show_watch
  user_id         uuid          references app.user(id) on delete cascade
  show_id         integer       references tvmaze.show(id) on delete cascade
  status          text          not null check (status in ('watching','want_to_watch','dropped'))
  created_at      timestamptz   not null default now()
  updated_at      timestamptz   not null default now()
  primary key (user_id, show_id)
  index on (user_id, status)

app.user_episode_watch
  user_id         uuid          references app.user(id) on delete cascade
  episode_id      integer       references tvmaze.episode(id) on delete cascade
  watched_at      timestamptz   not null default now()
  primary key (user_id, episode_id)
```

Notes:

- `watched` is derived: a show is "watched" for a user when every episode of that show has a `user_episode_watch` row for the user. Stored statuses encode user *intent* (`watching`, `want_to_watch`, `dropped`) and never include `watched`.
- Removing a show from the watchlist (`DELETE /me/shows/{id}`) does **not** delete `user_episode_watch` rows. Re-adding the show preserves prior progress. Episode rows only cascade away when the user account is deleted or the show itself is removed from `tvmaze.show` (which only happens if TV Maze drops the show entirely).
- No `relationship()` declarations on the SQLAlchemy models, matching the existing `tvmaze` convention. Tests that insert related rows in one transaction must `await session.flush()` between them.

## Authentication

### Password hashing

`argon2-cffi` (`argon2id`, library defaults). Added to `pyproject.toml`. `app.auth.hash_password(plaintext) -> str` and `app.auth.verify_password(plaintext, hash) -> bool` wrap the library and centralize parameter choice.

### Sessions

Server-side, opaque-token sessions stored in `app.session`. On login or signup the backend generates a 32-byte random token (`secrets.token_urlsafe(32)`), inserts a session row with `expires_at = now() + settings.session_ttl_days`, and sets it as a cookie. On every authenticated request, `get_current_user` looks up the row, rejects if `expires_at <= now()`, and bumps `last_seen_at` (sliding expiration is *not* implemented in this milestone; absolute expiry is enough). Logout deletes the row.

Expired-row cleanup is a non-goal here; expired rows simply fail the lookup. A scheduled cleanup job is a follow-up.

### Cookies

Session cookie attributes: `HttpOnly; Secure; SameSite=None; Path=/; Max-Age=<session_ttl_days * 86400>`. SameSite=None is required because the SPA origin (`tvbf.localhost`) is cross-site to the API origin (`tvbf-backend.localhost`); production will keep the same shape (`tvbf.com` / `api.tvbf.com`) unless we later collapse to a single origin with path-based routing. The CSRF cookie is **not** HttpOnly so the SPA can read it.

### CORS

Existing `CORSMiddleware` is extended with `allow_credentials=True`. The allowlist (`CORS_ALLOWED_ORIGINS`) is unchanged. Frontend `fetch` calls use `credentials: 'include'`.

### CSRF

Double-submit cookie pattern. On any auth response (signup, login, password change) the backend sets a non-HttpOnly `csrf_token` cookie containing a fresh 32-byte random value. A new FastAPI dependency `require_csrf` is attached to every state-changing route (`POST`, `PUT`, `PATCH`, `DELETE`) and verifies that the `X-CSRF-Token` request header equals the `csrf_token` cookie. Mismatch → `403 {"detail": "csrf_invalid"}`. The frontend's fetch wrapper reads the cookie and attaches the header automatically.

GET requests do not require CSRF (they are safe and idempotent by HTTP contract).

## API surface

All routes other than `/auth/signup` and `/auth/login` require a valid session. State-changing routes additionally require a valid CSRF header.

```
POST   /auth/signup
       body:    {email, password, display_name}
       result:  201 {user}, sets session + csrf cookies

POST   /auth/login
       body:    {email, password}
       result:  200 {user}, sets session + csrf cookies
                401 if credentials invalid

POST   /auth/logout
       result:  204, clears cookies, deletes session row

POST   /auth/password
       body:    {current_password, new_password}
       result:  204, rotates session (old session id deleted, new one set)
                401 if current_password wrong

GET    /me
       result:  200 {user}
                401 if no session

DELETE /me
       body:    {password}
       result:  204, cascades all user data
                401 if password wrong

GET    /me/shows
       query:   ?status=watching|want_to_watch|dropped|watched (optional)
       result:  200 [{show, status, next_episode | null, watched_count, total_episode_count}]

PUT    /me/shows/{show_id}
       body:    {status}            -- one of watching|want_to_watch|dropped
       result:  200 {show, status, ...}, idempotent upsert
                404 if show_id does not exist in tvmaze.show

DELETE /me/shows/{show_id}
       result:  204; removes user_show_watch row only.
                user_episode_watch rows are preserved.

POST   /me/episodes/{episode_id}/watched
       result:  201 {watched_at}, idempotent (re-marking is a no-op)
                404 if episode_id does not exist

DELETE /me/episodes/{episode_id}/watched
       result:  204, idempotent

POST   /me/shows/{show_id}/season/{n}/watched
       result:  201 {marked: <count>}, marks every episode in (show, season) watched
                404 if show or season has no episodes

DELETE /me/shows/{show_id}/season/{n}/watched
       result:  204, removes episode-watch rows for that season
```

### `next_episode` computation

For a single show the query is:

```sql
SELECT e.* FROM tvmaze.episode e
WHERE e.show_id = :show_id
  AND NOT EXISTS (
    SELECT 1 FROM app.user_episode_watch w
    WHERE w.user_id = :user_id AND w.episode_id = e.id
  )
ORDER BY e.season ASC, e.number ASC
LIMIT 1;
```

For `GET /me/shows`, this is run once per show in the page (N+1). Page sizes will be small; users with hundreds of tracked shows are an outlier worth optimizing for only after measurement. If it becomes a bottleneck, the follow-up is a single `LEFT JOIN LATERAL` query or a denormalized `last_watched_episode_id` column on `user_show_watch` updated by triggers — both are clean additions, neither earns its keep today.

### `watched` filter

Implemented as: `user_show_watch.status IN ('watching','want_to_watch')` AND every episode of the show has a watch row for the user. The query uses `NOT EXISTS` against unwatched episodes. `dropped` shows do not promote to `watched` even if every episode happens to be watched — dropped is explicit user intent.

## Frontend

### New routes & components

- `/login` — `LoginPage` + `LoginForm`.
- `/signup` — `SignupPage` + `SignupForm`.
- `/my-list` — `MyListPage` with tabs (`Watching | Want to watch | Watched | Dropped`).
- `RequireAuth` route guard wraps `/my-list` and redirects to `/login?next=<path>` when there is no current user.

### Auth state

`AuthContext` provides `{user, loading, login, signup, logout, changePassword, deleteAccount, refresh}`. On app mount it calls `GET /me` once; a 401 is treated as "logged out," not an error. All API calls go through a `apiFetch(path, options)` wrapper that:

1. Sets `credentials: 'include'`.
2. For non-`GET` requests, reads the `csrf_token` cookie and attaches `X-CSRF-Token`.
3. Treats a 401 response as "session expired": clears the user from context and redirects to `/login`.

### Header

When logged out: `Login` and `Sign up` links. When logged in: `My List` link, plus a user menu (display name with dropdown for `Change password`, `Delete account`, `Log out`).

### Show detail page

When authenticated:

- Adds a status dropdown (`Add to My List` / `Watching` / `Want to watch` / `Dropped` / `Remove`) below the show metadata.
- When status is `watching`, a "Next episode" card appears at the top of the detail card showing the next unwatched episode.
- Each episode row gets a checkbox; checked = watched. Per-episode toggles are optimistic updates with rollback on error.
- Each season header gets a "Mark season watched" / "Mark season unwatched" toggle.

When unauthenticated, the show detail page is unchanged from the current MVP.

### My List page

Four tabs corresponding to the statuses (with `Watched` derived). Each card shows poster, name, status, and (for `Watching`) the next-episode line. Cards link to the show detail page.

## Error handling

| Condition                                  | Response                              |
| ------------------------------------------ | ------------------------------------- |
| Missing or invalid session cookie          | `401 {"detail": "auth_required"}`     |
| Wrong password (login, change, delete)     | `401 {"detail": "invalid_credentials"}` |
| CSRF header missing or mismatched          | `403 {"detail": "csrf_invalid"}`      |
| Email already taken at signup              | `409 {"detail": "email_in_use"}`      |
| `show_id` / `episode_id` not in catalog    | `404 {"detail": "not_found"}`         |
| Validation (bad status, malformed body)    | FastAPI default `422`                 |
| Status not one of the three allowed        | `422` via Pydantic enum               |

The frontend `apiFetch` wrapper surfaces these to the calling component. AuthContext handles 401 globally; everything else propagates.

## Testing

### Backend

- **Unit** — password hash/verify roundtrip; session token entropy & expiry check; `next_episode` query against the seeded catalog; `watched`-status derivation logic.
- **Integration** (ASGITransport, same pattern as `test_browse_routes.py`):
  - Auth flows: signup → login → me → logout. Re-login. Wrong-password 401. Duplicate email 409.
  - CSRF: missing header on POST → 403. Mismatched header → 403. Header present and matching → success.
  - `/me/*` without session → 401.
  - Password change rotates session (old session id no longer authenticates).
  - Account delete cascades `user_show_watch` and `user_episode_watch` rows.
  - Watchlist status PUT is idempotent (PUT twice → one row).
  - Episode watch POST/DELETE is idempotent.
  - Bulk season mark/unmark counts match expected episode count.
  - Removing a show from watchlist preserves episode-watch rows; re-adding the show shows the previous progress.
- **Fixtures** — `tests/fixtures/users.py` provides:
  - `make_user(session, email=..., password=...)` factory.
  - `authed_client(user)` — async client with a valid session + csrf cookie pre-attached.

### Frontend

- `LoginForm` / `SignupForm` happy path + validation errors.
- `AuthContext`: initial `GET /me` populates user; 401 leaves user null; `login()` updates state.
- `RequireAuth`: unauthenticated visit redirects to `/login?next=…`.
- `MyListPage`: renders four tabs, switches based on status query.
- Show detail: status dropdown updates state; episode checkbox optimistic update + rollback on simulated 500.
- `apiFetch`: attaches `X-CSRF-Token` from cookie on `POST`/`PUT`/`DELETE`; 401 clears auth context.

## Migration

A single Alembic migration `XXXX_user_service_initial.py` does:

1. `CREATE EXTENSION IF NOT EXISTS citext;`
2. `CREATE EXTENSION IF NOT EXISTS pgcrypto;`
3. `CREATE TABLE app.user, app.session, app.user_show_watch, app.user_episode_watch` with FKs and indexes as specified.

Downgrade drops the four tables (extensions are left in place — they're cheap and dropping them might affect other databases sharing the cluster).

## Configuration

New env vars (added to `Settings` in `src/tvbf/config.py` and documented in `tvbf-backend/README.md`):

- `SESSION_COOKIE_NAME` (default `tvbf_session`)
- `CSRF_COOKIE_NAME` (default `csrf_token`)
- `SESSION_TTL_DAYS` (default `30`)
- `COOKIE_SECURE` (default `True`; allows tests to override)
- `COOKIE_SAMESITE` (default `none`)

`ADMIN_TOKEN` and `CORS_ALLOWED_ORIGINS` are unchanged.

## Risks & mitigations

| Risk                                                              | Mitigation                                                                                  |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `next_episode` N+1 query becomes slow under heavy watchlists      | Page sizes are small; defer optimization until measured. Clean follow-ups exist (LATERAL).  |
| Cookies blocked by browser due to SameSite=None over plain HTTP   | All local + prod traffic is HTTPS via Traefik; documented as a hard requirement.            |
| CSRF cookie readable by JS → XSS in SPA exposes CSRF token        | The session cookie remains HttpOnly; CSRF only protects against cross-site forgery, not XSS. We rely on React's escaping + CSP (future) for XSS defense. |
| Forgotten passwords with no reset flow                            | Documented as out-of-scope. Users can email tom@tomboone.com for a manual reset; reset flow is the next milestone if needed. |
| Test database leaks data between tests                            | Existing pattern: `session` fixture in `conftest.py` truncates between tests. Extended to cover the four new tables. |

## Open questions resolved during brainstorm

- **Auth strategy**: session cookies (chose A over JWT, OAuth).
- **Service boundary**: single FastAPI service (chose A over separate user service).
- **Episode tracking shape**: per-episode rows (chose A over "up-to" pointer).
- **Account lifecycle scope**: signup + login + logout + logged-in password change + account delete; verification, reset, social login, email change all out of scope.
- **Removing show preserves episode history**: yes.
- **`watched` is derived, not stored**: yes; stored statuses are intent only.

## References

- `tvbf-backend/CLAUDE.md` (parent repo `.claude/CLAUDE.md`) — non-obvious patterns and conventions.
- `docs/superpowers/specs/2026-04-19-browse-api-design.md` — read-only browse layer this builds on.
- `docs/superpowers/specs/2026-04-19-frontend-mvp-design.md` — SPA structure being extended.

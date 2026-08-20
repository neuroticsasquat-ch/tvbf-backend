# Watch Next / Upcoming Client-Supplied "Today" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix NEU-90: episodes appear in Watch Next at ~8pm the day before they air. Replace the server-side `date.today()` (UTC) used to bucket episodes into Watch Next / Upcoming / aired counts with a client-supplied calendar date, so the boundary tracks the user's local "today" instead of the server's UTC day.

**Architecture:** Three `/me/*` endpoints (`/me/watch-next`, `/me/upcoming`, `/me/shows`) gain an optional `today` query parameter (ISO `YYYY-MM-DD`). The router parses + validates and passes a `date` through to the service layer, which already accepts `today` as a parameter on the underlying repo functions. When `today` is absent, behavior falls back to today's date in UTC (preserves curl/admin tooling). The frontend computes the device's local calendar date once per query and includes it on every call. No threat model concern — these endpoints only affect the requesting user's own bucketing of their own shows.

**Tech Stack:** FastAPI / Pydantic v2, SQLAlchemy async, React 19 + TanStack Query, Vitest + MSW. All Python runs in `tvbf_backend` container; all Node runs in `tvbf_frontend` container.

---

## File Structure

**Backend — modify only:**
- `tvbf-backend/src/tvbf/routers/me.py` — add `today` query param to `list_my_shows_route`, `watch_next_route`, `upcoming_route`. Parse to `date`, pass to service.
- `tvbf-backend/src/tvbf/app/services/my_shows_service.py` — `list_my_shows`, `list_watch_next`, `list_upcoming` accept `today: date | None = None`; default to `date.today()` when None. Replace internal `date.today()` calls with the parameter.
- `tvbf-backend/tests/integration/routers/test_me.py` — add boundary tests for `today` parameter.
- `tvbf-backend/tests/integration/app/services/test_my_shows_service.py` — add boundary tests for service-level `today` plumbing.

**Frontend — create:**
- `tvbf-frontend/src/api/today.ts` — pure helper `localToday()` returning `YYYY-MM-DD` for the browser's local date.
- `tvbf-frontend/src/api/today.test.ts` — unit test the helper.

**Frontend — modify:**
- `tvbf-frontend/src/api/me.ts` — `useMyShows`, `useWatchNext`, `useUpcoming` include `today=...` in URL and query key.
- `tvbf-frontend/src/api/me.test.tsx` (create if missing) — verify hooks send the param.

No DB migration. No new env var. No type changes in `api/types.ts`.

---

## Task 1: Backend — service layer accepts optional `today`

**Files:**
- Modify: `tvbf-backend/src/tvbf/app/services/my_shows_service.py`
- Test: `tvbf-backend/tests/integration/app/services/test_my_shows_service.py`

The repo functions already take `today: date` — this task only changes the three service entry points. Goal: `today` flows in from the caller; if absent, `date.today()` (UTC, current behavior) is used so existing call sites and tests don't break.

- [ ] **Step 1: Write failing service test for `list_watch_next` with explicit `today`**

Append to `tvbf-backend/tests/integration/app/services/test_my_shows_service.py`:

```python
@pytest.mark.asyncio
async def test_list_watch_next_uses_supplied_today_as_upper_bound(session, make_user):
    """An episode airing on the supplied `today` is included; airing the next day is not."""
    user = await make_user(email="wn-today@example.com")
    show = await _seed_show(session, show_id=950100, name="Today Show")
    await session.flush()
    await _seed_season(session, show_id=show.id, season_id=950100, number=1)
    await session.flush()
    # Two episodes: one airing 2026-05-06, one airing 2026-05-07.
    await _seed_episode(
        session, episode_id=950101, show_id=show.id, season_id=950100,
        season=1, number=1, airdate=date(2026, 5, 6),
    )
    await _seed_episode(
        session, episode_id=950102, show_id=show.id, season_id=950100,
        season=1, number=2, airdate=date(2026, 5, 7),
    )
    await show_membership_repo.add(session, user_id=user.id, show_id=show.id)
    await session.commit()

    # With today=2026-05-06: only episode 950101 is "aired".
    rows = await my_shows_service.list_watch_next(
        session, user_id=user.id, today=date(2026, 5, 6)
    )
    assert [e.episode.id for e in rows] == [950101]

    # With today=2026-05-05: nothing aired yet.
    rows = await my_shows_service.list_watch_next(
        session, user_id=user.id, today=date(2026, 5, 5)
    )
    assert rows == []
```

Confirm imports at top of the test file include `from datetime import date` (it's already imported in this file — verify with `grep "from datetime" tests/integration/app/services/test_my_shows_service.py` before adding).

- [ ] **Step 2: Run test to verify it fails**

```bash
task test -- tests/integration/app/services/test_my_shows_service.py::test_list_watch_next_uses_supplied_today_as_upper_bound -v
```

Expected: FAIL with `TypeError: list_watch_next() got an unexpected keyword argument 'today'`.

- [ ] **Step 3: Add `today` parameter to the three service functions**

Edit `tvbf-backend/src/tvbf/app/services/my_shows_service.py`:

In `list_my_shows` (around line 153), change signature and replace `today = date.today()` (around line 172):

```python
async def list_my_shows(
    db: AsyncSession,
    *,
    user_id: UUID,
    sort: MyShowsSort = "recent_activity",
    today: date | None = None,
) -> list[MyShowEntry]:
    pairs = await show_membership_repo.list_with_added_at(db, user_id)
    if not pairs:
        return []

    shows = [show for show, _added in pairs]
    show_ids = [show.id for show in shows]
    added_at_by_show = {show.id: added for show, added in pairs}

    genres_by_show, networks_by_id, wcs_by_id = await hydrate_show_refs(db, shows)
    total_counts = await episode_repo.count_per_show(db, show_ids)
    watched_counts = await episode_watch_repo.count_watched_per_show(
        db, user_id=user_id, show_ids=show_ids
    )
    today_d = today if today is not None else date.today()
    latest_aired = await episode_repo.latest_aired_per_show(db, show_ids, today_d)
    aired_counts = await episode_repo.count_aired_per_show(db, show_ids, today_d)
    last_watched = await episode_watch_repo.latest_watched_per_show(
        db, user_id=user_id, show_ids=show_ids
    )
    # ... rest unchanged
```

In `list_watch_next` (around line 206), change signature and replace `today = date.today()` (around line 214):

```python
async def list_watch_next(
    db: AsyncSession,
    *,
    user_id: UUID,
    sort: WatchNextSort = "airdate_desc",
    today: date | None = None,
) -> list[WatchNextEntry]:
    """Per show in My Shows, the earliest unwatched episode whose airdate has
    already passed. Shows with nothing unwatched-and-aired are omitted."""
    today_d = today if today is not None else date.today()
    episodes = await episode_repo.earliest_aired_unwatched_per_show(
        db, user_id=user_id, today=today_d
    )
    # ... rest unchanged; replace any further `today` references with `today_d`
```

In `list_upcoming` (around line 272), change signature and replace `today = date.today()` (around line 280):

```python
async def list_upcoming(
    db: AsyncSession,
    *,
    user_id: UUID,
    sort: UpcomingSort = "airdate_asc",
    today: date | None = None,
) -> list[UpcomingEntry]:
    """Per show in My Shows, the earliest episode whose airdate is in the
    future. Shows with no scheduled future episodes are omitted."""
    today_d = today if today is not None else date.today()
    episodes = await episode_repo.earliest_future_per_show(db, user_id=user_id, today=today_d)
    # ... rest unchanged; replace any further `today` references with `today_d`
```

Within each function body, every existing reference to the local `today` variable must become `today_d`. Search the function body for `today` (whole-word) and update each call site (`count_aired_per_show(db, show_ids, today)` → `..., today_d`, etc.). Don't rename `Episode.airdate <= today` in repo files — those repos are unaffected.

- [ ] **Step 4: Run test to verify it passes**

```bash
task test -- tests/integration/app/services/test_my_shows_service.py::test_list_watch_next_uses_supplied_today_as_upper_bound -v
```

Expected: PASS.

- [ ] **Step 5: Run the full service test file to confirm no regressions**

```bash
task test -- tests/integration/app/services/test_my_shows_service.py -v
```

Expected: all tests PASS (existing tests still rely on `date.today()` default).

- [ ] **Step 6: Add an `Upcoming` boundary test for symmetry**

Append to the same test file:

```python
@pytest.mark.asyncio
async def test_list_upcoming_uses_supplied_today_as_lower_bound(session, make_user):
    """Episode airing on `today` is NOT upcoming; airing the next day IS."""
    user = await make_user(email="up-today@example.com")
    show = await _seed_show(session, show_id=950110, name="Upcoming Today")
    await session.flush()
    await _seed_season(session, show_id=show.id, season_id=950110, number=1)
    await session.flush()
    await _seed_episode(
        session, episode_id=950111, show_id=show.id, season_id=950110,
        season=1, number=1, airdate=date(2026, 5, 6),
    )
    await _seed_episode(
        session, episode_id=950112, show_id=show.id, season_id=950110,
        season=1, number=2, airdate=date(2026, 5, 7),
    )
    await show_membership_repo.add(session, user_id=user.id, show_id=show.id)
    await session.commit()

    rows = await my_shows_service.list_upcoming(
        session, user_id=user.id, today=date(2026, 5, 6)
    )
    assert [e.episode.id for e in rows] == [950112]
```

- [ ] **Step 7: Run new test, confirm pass**

```bash
task test -- tests/integration/app/services/test_my_shows_service.py::test_list_upcoming_uses_supplied_today_as_lower_bound -v
```

Expected: PASS.

---

## Task 2: Backend — router accepts optional `today` query parameter

**Files:**
- Modify: `tvbf-backend/src/tvbf/routers/me.py`
- Test: `tvbf-backend/tests/integration/routers/test_me.py`

FastAPI parses ISO date strings to `date` automatically when the parameter is annotated `date | None`. Invalid strings produce a 422.

- [ ] **Step 1: Write failing router test for `today` query param on watch-next**

Find the existing `test_watch_next_route_returns_list` in `tvbf-backend/tests/integration/routers/test_me.py` (around line 343) and append a new test after it:

```python
@pytest.mark.asyncio
async def test_watch_next_route_accepts_today_param(session, make_user):
    """The router accepts an explicit `today` and forwards it to the service.
    With today < episode airdate, the episode is excluded from Watch Next."""
    from datetime import date as _date

    user = await make_user(email="wn-rt-today@example.com")
    show = await _seed_show(session, show_id=940500, name="Router Today Show")
    await session.flush()
    await _seed_season(session, show_id=show.id, season_id=940500, number=1)
    await session.flush()
    await _seed_episode(
        session, episode_id=940501, show_id=show.id, season_id=940500,
        season=1, number=1, airdate=_date(2026, 5, 6),
    )
    await session.commit()
    await me_router.add_show_route(show_id=show.id, user=user, db=session)

    # today = day before airdate → not yet aired → empty list
    rows = await me_router.watch_next_route(
        sort="airdate_desc", today=_date(2026, 5, 5), user=user, db=session
    )
    assert rows == []

    # today = airdate → aired → included
    rows = await me_router.watch_next_route(
        sort="airdate_desc", today=_date(2026, 5, 6), user=user, db=session
    )
    assert [e.episode.id for e in rows] == [940501]
```

Use whatever `_seed_show` / `_seed_season` / `_seed_episode` helpers already exist in this file (check via `grep "_seed_" tests/integration/routers/test_me.py`). If they don't exist, copy the imports + helpers from `tests/integration/app/services/test_my_shows_service.py`.

- [ ] **Step 2: Run test, expect TypeError on missing param**

```bash
task test -- tests/integration/routers/test_me.py::test_watch_next_route_accepts_today_param -v
```

Expected: FAIL with `TypeError: watch_next_route() got an unexpected keyword argument 'today'`.

- [ ] **Step 3: Add `today` query parameter to all three routes**

Edit `tvbf-backend/src/tvbf/routers/me.py`. Add `date` import at top:

```python
from datetime import date
```

Update `list_my_shows_route` (around line 69):

```python
@router.get("/me/shows", response_model=list[MyShowEntry])
async def list_my_shows_route(
    sort: Annotated[MyShowsSort, Query()] = "recent_activity",
    today: Annotated[date | None, Query()] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[MyShowEntry]:
    return await my_shows_service.list_my_shows(db, user_id=user.id, sort=sort, today=today)
```

Update `watch_next_route` (around line 113):

```python
@router.get("/me/watch-next", response_model=list[WatchNextEntry])
async def watch_next_route(
    sort: Annotated[WatchNextSort, Query()] = "airdate_desc",
    today: Annotated[date | None, Query()] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[WatchNextEntry]:
    return await my_shows_service.list_watch_next(
        db, user_id=user.id, sort=sort, today=today
    )
```

Update `upcoming_route` (around line 122):

```python
@router.get("/me/upcoming", response_model=list[UpcomingEntry])
async def upcoming_route(
    sort: Annotated[UpcomingSort, Query()] = "airdate_asc",
    today: Annotated[date | None, Query()] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[UpcomingEntry]:
    return await my_shows_service.list_upcoming(
        db, user_id=user.id, sort=sort, today=today
    )
```

- [ ] **Step 4: Run failing test, confirm pass**

```bash
task test -- tests/integration/routers/test_me.py::test_watch_next_route_accepts_today_param -v
```

Expected: PASS.

- [ ] **Step 5: Add an HTTP-level test for invalid `today` returning 422**

Append to the same test file:

```python
@pytest.mark.asyncio
async def test_watch_next_route_invalid_today_returns_422(client, make_user):
    """`?today=garbage` is rejected by FastAPI's date validator."""
    user = await make_user(email="wn-rt-bad-today@example.com")
    # Authenticated client setup matches existing tests in this file — reuse the
    # `client` fixture pattern. If the file uses a `make_client(user)` helper,
    # use that instead.
    auth_client = await _login_client(client, user)  # use existing helper, or inline a /login call
    resp = await auth_client.get("/me/watch-next?today=not-a-date")
    assert resp.status_code == 422
```

Before writing this, run `grep -n "client\|_login_client\|async def test_login" tests/integration/routers/test_me.py | head -20` to find the existing authenticated-client pattern. If `test_me.py` already uses a fixture that returns a logged-in `AsyncClient` (likely named something like `auth_client` or `client_with_session`), use that fixture name in the test signature instead of constructing one manually. If no such helper exists in `test_me.py`, look in the conftest at `tests/integration/conftest.py` and use whatever pattern other authenticated tests in this file use.

- [ ] **Step 6: Run the new test**

```bash
task test -- tests/integration/routers/test_me.py::test_watch_next_route_invalid_today_returns_422 -v
```

Expected: PASS.

- [ ] **Step 7: Run the full me-router test file**

```bash
task test -- tests/integration/routers/test_me.py -v
```

Expected: all tests PASS.

- [ ] **Step 8: Run full backend test suite + lint + typecheck**

```bash
task test
task lint
task typecheck
```

Expected: all green. If lint complains about `from datetime import date` ordering, run `task format`.

---

## Task 3: Frontend — `localToday()` helper

**Files:**
- Create: `tvbf-frontend/src/api/today.ts`
- Test: `tvbf-frontend/src/api/today.test.ts`

The browser's local calendar date as `YYYY-MM-DD`. We use the device's local components (year/month/day from `Date`) — *not* `toISOString()` which returns UTC.

- [ ] **Step 1: Write failing test**

Create `tvbf-frontend/src/api/today.test.ts`:

```typescript
import { describe, expect, it, afterEach, vi } from "vitest";
import { localToday } from "./today";

describe("localToday", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns YYYY-MM-DD using the device's local date components", () => {
    // 2026-05-06 18:14 local time on the test machine. We don't fake the TZ;
    // we fake the Date and assert against its local components.
    const fake = new Date(2026, 4, 6, 18, 14, 0); // months are 0-indexed: 4 = May
    vi.useFakeTimers();
    vi.setSystemTime(fake);
    expect(localToday()).toBe("2026-05-06");
  });

  it("zero-pads month and day", () => {
    const fake = new Date(2026, 0, 3, 9, 0, 0); // Jan 3
    vi.useFakeTimers();
    vi.setSystemTime(fake);
    expect(localToday()).toBe("2026-01-03");
  });
});
```

- [ ] **Step 2: Run test, expect failure (module missing)**

From host:

```bash
cd tvbf-frontend
task test -- src/api/today.test.ts
```

Expected: FAIL — cannot resolve `./today`.

- [ ] **Step 3: Implement helper**

Create `tvbf-frontend/src/api/today.ts`:

```typescript
export function localToday(now: Date = new Date()): string {
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}
```

- [ ] **Step 4: Run test, expect pass**

```bash
task test -- src/api/today.test.ts
```

Expected: PASS (both cases).

---

## Task 4: Frontend — wire `localToday()` into `useWatchNext`, `useUpcoming`, `useMyShows`

**Files:**
- Modify: `tvbf-frontend/src/api/me.ts`
- Test: `tvbf-frontend/src/api/me.test.tsx` (create)

Each hook reads `localToday()` once per render and includes it in both the URL and the React Query key (so day-rollover triggers a fresh fetch when the user revisits the tab).

- [ ] **Step 1: Write a failing hook test using MSW**

Create `tvbf-frontend/src/api/me.test.tsx`:

```typescript
import { describe, expect, it, beforeAll, afterAll, afterEach, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import type { ReactNode } from "react";
import { useWatchNext, useUpcoming, useMyShows } from "./me";
import { env } from "@/env";

const requested: { path: string; today: string | null }[] = [];

const server = setupServer(
  http.get(`${env.apiBase}/me/watch-next`, ({ request }) => {
    const url = new URL(request.url);
    requested.push({ path: "/me/watch-next", today: url.searchParams.get("today") });
    return HttpResponse.json([]);
  }),
  http.get(`${env.apiBase}/me/upcoming`, ({ request }) => {
    const url = new URL(request.url);
    requested.push({ path: "/me/upcoming", today: url.searchParams.get("today") });
    return HttpResponse.json([]);
  }),
  http.get(`${env.apiBase}/me/shows`, ({ request }) => {
    const url = new URL(request.url);
    requested.push({ path: "/me/shows", today: url.searchParams.get("today") });
    return HttpResponse.json([]);
  }),
);

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  requested.length = 0;
  vi.useRealTimers();
});
afterAll(() => server.close());

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

describe("/me hooks send local today", () => {
  it("useWatchNext sends today=YYYY-MM-DD from device local date", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 4, 6, 18, 14, 0)); // 2026-05-06 local

    const { result } = renderHook(() => useWatchNext(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const hit = requested.find((r) => r.path === "/me/watch-next");
    expect(hit?.today).toBe("2026-05-06");
  });

  it("useUpcoming sends today on the request", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 4, 6, 18, 14, 0));

    const { result } = renderHook(() => useUpcoming(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const hit = requested.find((r) => r.path === "/me/upcoming");
    expect(hit?.today).toBe("2026-05-06");
  });

  it("useMyShows sends today on the request", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 4, 6, 18, 14, 0));

    const { result } = renderHook(() => useMyShows(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const hit = requested.find((r) => r.path === "/me/shows");
    expect(hit?.today).toBe("2026-05-06");
  });
});
```

If `env.apiBase` doesn't match the existing MSW pattern, copy the import path used in `tvbf-frontend/src/test/msw/handlers.ts` (`grep -n "import" src/test/msw/handlers.ts | head -3`) and use the same `base` constant.

- [ ] **Step 2: Run, expect failures (URL won't include `today`)**

```bash
task test -- src/api/me.test.tsx
```

Expected: FAIL — `hit?.today` is `null`.

- [ ] **Step 3: Update the hooks to include `today`**

Edit `tvbf-frontend/src/api/me.ts`. Add the import near the top:

```typescript
import { localToday } from "./today";
```

Replace `useMyShows`:

```typescript
export function useMyShows(sort: MyShowsSort = "recent_activity") {
  const today = localToday();
  return useQuery<MyShowEntry[]>({
    queryKey: ["my-shows", sort, today],
    queryFn: () => apiFetch<MyShowEntry[]>(`/me/shows?sort=${sort}&today=${today}`),
    staleTime: FIVE_MINUTES,
  });
}
```

Replace `useWatchNext`:

```typescript
export function useWatchNext() {
  const today = localToday();
  return useQuery<WatchNextEntry[]>({
    queryKey: ["watch-next", today],
    queryFn: () => apiFetch<WatchNextEntry[]>(`/me/watch-next?today=${today}`),
  });
}
```

Replace `useUpcoming`:

```typescript
export function useUpcoming(sort: UpcomingSort = "airdate_asc") {
  const today = localToday();
  return useQuery<UpcomingEntry[]>({
    queryKey: ["upcoming", sort, today],
    queryFn: () => apiFetch<UpcomingEntry[]>(`/me/upcoming?sort=${sort}&today=${today}`),
  });
}
```

The existing `invalidateAll` helper invalidates by the *first* element of the query key (`["my-shows"]`, `["watch-next"]`, `["upcoming"]`) — TanStack Query treats those as prefix matches, so the longer keys above still get invalidated. No changes needed to `invalidateAll`.

- [ ] **Step 4: Run hook tests, confirm pass**

```bash
task test -- src/api/me.test.tsx
```

Expected: all three tests PASS.

- [ ] **Step 5: Run the full frontend suite + lint + typecheck**

```bash
task test
task lint
task typecheck
```

Expected: all green. The existing MSW handlers in `src/test/msw/handlers.ts` register `/me/watch-next`, `/me/upcoming`, and `/me/shows` without query-string assertions, so they continue to match the new requests.

---

## Task 5: Manual smoke test in the running app

**Files:** none — verification only.

- [ ] **Step 1: Confirm the dev stack is running**

```bash
cd tvbf-backend && task up
cd ../tvbf-frontend && task up
```

- [ ] **Step 2: Verify the URL in browser DevTools includes `today`**

Open `https://app.tvbf.localhost/`. In Network tab, filter on `me/`. Confirm:
- `GET /me/shows?sort=...&today=YYYY-MM-DD`
- `GET /me/watch-next?today=YYYY-MM-DD`
- `GET /me/upcoming?sort=...&today=YYYY-MM-DD`

The `today` value should match your machine's current local calendar date.

- [ ] **Step 3: Verify the original bug is fixed**

If it's currently after 8pm in your local TZ but before midnight, an episode whose `airdate` is *tomorrow's* local date should NOT appear in Watch Next. Confirm via the UI on a show that has an episode airing tomorrow. (If no such episode exists right now, this step is a sanity check rather than a reproduction.)

You can also direct-test with curl:

```bash
# Replace <session> + <csrf> with values from a logged-in browser session.
# Pick a `today` deliberately one day before tomorrow's airdate to confirm it's excluded:
curl -s 'https://api.tvbf.localhost/me/watch-next?today=2026-05-05' \
  -H 'Cookie: tvbf_session=<session>'
```

- [ ] **Step 4: Confirm 422 on bad `today`**

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  'https://api.tvbf.localhost/me/watch-next?today=garbage' \
  -H 'Cookie: tvbf_session=<session>'
```

Expected: `422`.

---

## Self-Review Notes

- **Spec coverage:** there is no formal spec for NEU-90; this plan is the design + implementation. Goal mapped to tasks: backend service accepts today (Task 1), router accepts + validates today (Task 2), frontend computes local today (Task 3) and sends it on every relevant request (Task 4), manual smoke (Task 5).
- **Backwards compatibility:** `today` is optional on the API; admin tooling and curl with no param fall back to UTC `date.today()` (current behavior). Existing service-level tests don't pass `today` and still work.
- **Cache invalidation:** the existing `invalidateAll` uses prefix-match keys (`["watch-next"]`, etc.), which still match the new longer keys (`["watch-next", today]`).
- **Day rollover:** including `today` in the React Query key means crossing midnight in the user's local TZ will cause a re-fetch on the next render that calls one of the hooks. This is desired behavior — at midnight, "today" changes and Upcoming → Watch Next transitions become visible without a hard refresh.
- **What this does not do:** still no per-episode airtime gating (episodes flip at user-local midnight, not at the show's actual broadcast time). That's a separate axis and out of scope here.

# NEU-1197 — Admin read route for user reports

**Ticket:** [NEU-1197](https://linear.app/neuroticsasquatch/issue/NEU-1197/backend-admin-read-route-for-user-reports)
**Project:** TVBF: Open Registration · Milestone 1, Trust boundary
**Blocked by:** [NEU-1162](https://linear.app/neuroticsasquatch/issue/NEU-1162/backend-add-user-disable-flag-admin-route-and-report-user-endpoint) (merged — `547d278`)
**Repo:** `tvbf-backend` only. No frontend half is ticketed; `curl` with the session cookie closes the gap this spec is about.
**Written:** 2026-08-20

`POST /reports` persists every report and then notifies Tom **best-effort**
(NEU-1162 §8.1). That is the right durability contract — a report must not be
lost because Linear is down, and `linear_feedback_enabled` defaults to `False`,
so a mirrored contract would refuse reports outright in local dev and CI.

The consequence is that the notification is the **only** way a report becomes
visible, and it is the part allowed to fail. This spec makes the rows readable
over HTTP. It adds no workflow, no state and no migration: it is one `SELECT`
behind the admin session gate.

---

## 1. What this route is and is not

It is a **queue reader**. It answers three questions and nothing else:

- What has been reported that I have not seen? (the notification-failed case)
- Has this account been reported by more than one person?
- What did the reporter actually say?

It is **not triage.** NEU-1162 §5 refused a `handled_at` / status column
deliberately: Linear is the workflow, with states, assignment and comments, and
a coarse copy of that in Postgres gives two records that immediately disagree.
Nothing here writes. The one piece of workflow state the queue does show —
whether the reported account is currently disabled — is a **live join** to
`app.user.disabled_at`, not a flag stored on the report, for the same reason
NEU-1162's four invisibility predicates are live joins: it cannot drift, and
clearing the flag restores the truth with no backfill.

## 2. The contract

`GET /admin/reports`

**Gate:** `require_admin_user` at the router level — the cookie-session admin
gate, mirroring `routers/admin_users.py`, not the bearer-token
`routers/admin.py`. This is a surface an admin reads in the SPA, not a scripting
endpoint. **No `require_csrf`**: it is a GET, and CSRF is on mutating routes
only.

| Query param | Type | Default | Bounds |
|---|---|---|---|
| `page` | int | `1` | `ge=1, le=1000` |
| `per_page` | int | `50` | `ge=1, le=100` |
| `reported_user_id` | UUID \| None | `None` | — |

| Status | Detail | When |
|---|---|---|
| `200` | — | always, for an admin session |
| `401` | `auth_required` | no session, or a disabled admin's session (NEU-1162 §2.1) |
| `403` | `admin_required` | authenticated non-admin |
| `422` | — | malformed UUID or out-of-bounds paging, from FastAPI's own validation |

**Response header:** `Cache-Control: private, no-store` (§7).

### 2.1 Response shape

```
AdminReportPage {
  items: list[AdminReportOut]
  page: int
  per_page: int
  total: int
  total_pages: int
}

AdminReportOut {
  id: UUID
  reporter: AdminReportUserRef
  reported_user: AdminReportUserRef
  reason: str
  created_at: datetime
}

AdminReportUserRef { id: UUID, display_name: str, disabled_at: datetime | None }
```

`page` / `per_page` / `total` / `total_pages` is the `ShowListPage` and
`PersonListPage` shape, which is the only pagination vocabulary this API has.
The ticket's phrase "the existing offset convention" describes offset
*semantics* — `(page - 1) * per_page` — which this is; taking it literally as
`limit`/`offset` would introduce a second pagination vocabulary on the API's
smallest surface.

`total` is load-bearing rather than decorative. It is what makes the ticket's
headline case — "five people reported this account" — answerable in one request.
Without it, `?reported_user_id=X` reports a count only while the count fits
under the page cap, so the answer silently degrades to "at least N" exactly when
the number is alarming.

**Not `list[AdminReportOut]`**, despite `GET /admin/users` being a bare
unpaginated list. That route gets away with it because the user table is small
and self-limiting; `app.user_report` is append-only and adversarially fed — a
griefer at NEU-1162 §6's ceiling adds 5 rows a day forever — so it is the one
admin list with no natural bound, and the DoD's "the page size is bounded" is
what stops an unbounded scan of it.

## 3. The row

Six of the fields are the ticket's. Three decisions beyond it:

- **The report's own `id` is included.** It costs nothing, it is the React key a
  future Reports tab needs, and it is the only stable handle on a row whose
  other identifying fields are duplicated or attacker-authored.
- **Neither party's `email` is included.** An admin who needs it has
  `GET /admin/users`, which already exposes email for every account. Carrying it
  here widens what leaks if this route is ever mis-gated, for a field nobody
  triages on. The id is the identity; the display name is the label.
- **The *reporter's* `disabled_at` is included**, though the ticket names only
  the reported user's. Same join, one more column, and it answers the question
  the headline case invites: `?reported_user_id=X` returning five rows means
  *five reports*, not *five people*, and if three came from an account already
  disabled as a griefer, that is the difference between a pile-on and a
  campaign. Without it the queue is gameable by the exact behaviour §6's
  throttle exists to bound.

**Nested refs, not flat prefixes.** `AdminUserOut` is flat because it *is* one
user; this row is a relationship between two, and `reporter_id` /
`reporter_display_name` / `reporter_disabled_at` at three fields each is where
that starts reading as two structs pretending not to be. It also hands the SPA
one object per party for a shared user-ref component.

### 3.1 `reason` is returned in full, and verbatim

1–5000 chars, untruncated. The rejected alternative was a truncated list plus a
`GET /admin/reports/{id}` detail route: the ticket's stated problem is that
reading the reason text requires a database session, and truncating does not
close that — it moves the psql session to a second HTTP round trip and adds a
route nobody asked for. The worst-case page (100 × 5000 chars ≈ 500 KB) requires
every report on it to be a maximal essay, which is not a shape a table fed at 5
rows/day/reporter takes.

**Verbatim** means no escaping or sanitising on the way out. It is JSON;
escaping belongs to the renderer, and pre-escaping would corrupt the text an
admin is reading to make a decision. `reason` is the same class of data as the
display names NEU-1162 §8.2 refused to trust as structure — whatever renders
this must not render it as HTML.

## 4. The queue filters nothing

NEU-1162 §4 put four predicates on `disabled_at IS NULL`
(`accepted_friend_ids`, `are_connected`, `user_repo.search`,
`list_pending_for_user`). This route joins `app.user` twice and carries none of
them. That is deliberate, and stated here so it does not later read as an
oversight someone helpfully fixes.

**§4's predicates are about what a _stranger_ may see. This is the one read path
whose entire purpose is to see what strangers cannot.**

Four cases, all kept:

- **A report about a disabled account stays listed.** Filtering it would hide
  reports about precisely the accounts under moderation — NEU-1162 §7.2's "a
  disabled user can be reported; §4 makes them invisible, not nonexistent", read
  from the other end. It is also the DoD's own case: the queue answers "has this
  already been dealt with?" only by showing the reports *and* the `disabled_at`
  beside them.
- **A report by a disabled account stays listed.** Disabling a griefer must not
  retroactively erase the reports that justified it.
- **Reports about, or by, an admin stay listed.** §7.1 already established that
  an admin may disable another admin; a queue blind to reports about admins
  would be the one hole in the moderation surface.
- **Self-reports do not exist** — `400 cannot_report_self` at the write path —
  so there is no same-party case to handle.

### 4.1 Deleted accounts need no handling

Both FKs are `NOT NULL` with `ON DELETE CASCADE`, so an `INNER JOIN` to
`app.user` on either side can neither drop a row nor fan one out. There is no
orphaned-row case to render, no `LEFT JOIN`, and no `DISTINCT`. NEU-1162 §5
already stated the consequence this rests on: an abuser who self-deletes takes
their reports with them, and the Linear issue is what survives.

## 5. No migration

**This ticket ships no Alembic migration.** It is pure read code.

The existing `ix_user_report_reporter_created` on `(reporter_id, created_at)`
serves neither of this route's queries — the unfiltered `ORDER BY created_at
DESC`, nor `WHERE reported_user_id = ? ORDER BY created_at DESC`. That is
accepted rather than fixed. The row count is bounded by construction at 5/day
/reporter; the route is human-triggered a few times a week, not on any write
path; and NEU-1162 added its one index under the standard "this is the exact
query, on every write", which an admin's manual read does not meet. Adding
`(reported_user_id, created_at DESC)` now is speculative index-building against
a table holding zero rows in prod, and it taxes every report write to serve a
read nobody has measured.

**The revisit threshold, so it is a decision and not folklore:** when
`app.user_report` passes ~100k rows, or when the unfiltered first page stops
returning in well under a second, add `(reported_user_id, created_at DESC)` —
additive, no rewrite.

### 5.1 One filter, not two

`?reported_user_id=` ships; **`?reporter_id=` does not.** The case for it is
real — it is the mirror-image "is this account a serial reporter?" query, and
about two lines — but at the volume this route actually sees, the whole queue
fits on one page and every reporter is visible without filtering. It starts
earning its place at exactly the size where §5's index does. Shipping the filter
now while refusing the index would be doing the cheap half of a problem we have
agreed we do not yet have.

**When the threshold in §5 trips, `reporter_id` and the index land together.**

### 5.2 An unknown `reported_user_id` is an empty page, not a 404

`200` with `total: 0`, and no existence check. A filter is a filter, not a
lookup: `total: 0` *is* the true answer to "has this account been reported?",
which is the question the parameter exists to ask. Distinguishing "no reports"
from "no such user" would cost a second query against `app.user` on every
filtered request to produce a difference the caller cannot act on. The
enumeration argument that usually forces 404-vs-empty does not apply behind
`require_admin_user` — an admin can already list every account.

## 6. Ordering

`ORDER BY created_at DESC, id DESC`.

The tiebreak is not decoration. `created_at` defaults to `now()`, which is
transaction-start time, so two reports committed from concurrent transactions
can carry byte-identical timestamps; without a tiebreak such a pair can appear
on both page 1 and page 2, or on neither. Deterministic paging for free.

## 7. `Cache-Control: private, no-store`

Set per-route. **This is the first admin route in the codebase to carry a cache
header**, and the divergence is deliberate.

CLAUDE.md's cache pattern is scoped to the browse router and says admin routes
are unaffected — but the *reason* browse's `_SHOW_EP_CACHE` override exists
applies here on its merits. That override is not about fan-out; it is that a
payload carrying a field which mutates through a **different** route can be
served stale from the browser cache with no way to invalidate it, and the user
reads the stale body as "my action didn't take". This payload carries
`disabled_at` for both parties, mutated by `PATCH /admin/users/{user_id}/disabled`
— same SPA, same admin, often seconds apart. Disable someone in the Users tab,
open the report queue, and a heuristically-cached body shows `disabled_at: null`
beside their name: the queue answering "has this been dealt with?" with the
wrong answer. That is a wrong decision, not a staleness nuisance.

**`GET /admin/users` has the identical exposure and is deliberately not changed
here.** Noted as a known gap rather than silently diverged from; closing it is a
one-line change in a ticket that owns that route.

## 8. Where the code lives

- **`src/tvbf/routers/admin_reports.py`** — new module, `prefix="/admin/reports"`,
  `tags=["admin"]`, `dependencies=[Depends(require_admin_user)]` at the router
  level (`invites_admin.py`'s shape — one route, no reason to repeat the dep).
  Registered in `main.py` beside `admin_users.router`.

  **The filename encodes the gate**, following the pattern already in this repo:
  `admin_invites.py` is the cookie-session invite surface, `invites_admin.py` is
  the bearer-token one, and the word order is the distinction. This is a
  cookie-session route, so it is `admin_reports.py`.

  **Not folded into `routers/reports.py`.** That module is the *user's*
  report-filing surface with a per-route `get_current_user`; merging a
  `require_admin_user` route into it would put two different gates in one file
  for the first time in this codebase.

- **`src/tvbf/app/repos/user_report_repo.py`** — gains one function returning
  `(rows, total)`, the pair `browse_queries.list_shows` and `search_people`
  already return. Both `app.user` joins use `aliased(User)` so one round trip
  hydrates both parties; **two queries per request** (page + count), the same
  budget as `GET /shows`.

  `admin_users.py` gets away with an inline `select(User)` because it is a bare
  unfiltered scan; a two-sided join with a filter and a count is query-layer
  work, and this repo module already owns this table's vocabulary. Its docstring
  says it is "the ledger the report throttle counts" — that grows a second
  sentence naming the queue reader rather than the file being split. Splitting
  would put two queries against one five-column table in two modules.

- **`src/tvbf/app/schemas.py`** — `AdminReportPage`, `AdminReportOut`,
  `AdminReportUserRef`, beside `AdminUserOut`. Response models, not a request
  body, so they follow `AdminUserOut`'s home rather than `ReportIn`'s
  beside-its-route placement.

- **No service layer.** There is no business logic — no commit, no notification,
  no throttle. A `report_service` function that only forwards to the repo would
  be a seam with nothing in it.

## 9. Acceptance criteria

1. `GET /admin/reports` returns `200` for an admin session, `403
   admin_required` for an authenticated non-admin, and `401` for no session.
2. A report filed while `linear_feedback_enabled=false` — one that produced no
   Linear issue at all — appears in the response. This is the ticket's reason
   for existing.
3. Each row carries `id`, `reporter {id, display_name, disabled_at}`,
   `reported_user {id, display_name, disabled_at}`, `reason` (in full) and
   `created_at`.
4. `GET /admin/reports?reported_user_id=<uuid>` returns only that account's
   reports, and `total` counts them.
5. Results are newest-first and paginated; `per_page=101` is `422`, and
   `total` / `total_pages` are correct across a multi-page set.
6. **A report *about* a disabled user and a report *by* a disabled user are both
   listed** (§4).
7. **An unknown `reported_user_id` returns `200` with `total: 0`**, not `404`
   (§5.2).
8. **Two reports sharing a `created_at` order by `id DESC`**, so a page boundary
   duplicates and drops nothing (§6).
9. The response carries `Cache-Control: private, no-store` (§7).
10. `.claude/docs/architecture-endpoints.md` and CLAUDE.md's path index list the
    route; `.claude/docs/patterns-auth-and-abuse.md` records §4's rule. Both
    files currently assert that no such route exists — see §11.
11. `task test`, `task lint`, `task typecheck` all green.

## 10. Out of scope

- **The admin UI for the queue.** A Reports tab beside `AdminUsersTab` /
  `AdminInvitesTab` is the natural follow-on — `AdminUsersTab.tsx` already
  carries a comment saying "NEU-1197's report queue is what will really answer
  'who needs attention'" — but `curl` with the session cookie closes the gap
  this ticket is about. The payload is shaped to be consumed by that tab
  (§2.1, §3) without being blocked on it.
- **Any triage state** — `handled_at`, assignment, dismissal. NEU-1162 §5's
  refusal stands: Linear is the workflow.
- **Auto-disable on N reports.** Nothing reads this table to make a decision;
  disabling stays a human act (NEU-1162 §11).
- **`?reporter_id=` and `(reported_user_id, created_at DESC)`** — deferred
  together to one threshold (§5, §5.1).
- **A `GET /admin/reports/{id}` detail route** (§3.1).
- **`Cache-Control` on `GET /admin/users`** (§7).
- **Pruning `app.user_report`.** Same standing gap as `app.auth_attempt`
  (NEU-1160 §10) and as NEU-1162 §11 left it.
- **Reports in `GET /me/export`.** NEU-1162 §11 omitted them deliberately —
  including them hands a griefer a tidy record of who they have reported — and
  nothing here changes that.

## 11. Notes for the implementation

- **Two documents currently assert this route does not exist**, and both
  sentences become false. Verified 2026-08-20:
  `.claude/docs/architecture-endpoints.md` line 13 ends *"There is deliberately
  no `GET /admin/reports` yet; that is NEU-1197, which this blocks."*, and
  `.claude/docs/patterns-auth-and-abuse.md` line 22 says a notification failure
  logs at ERROR *"until `GET /admin/reports` exists (NEU-1197) a dropped
  notification is the only way a report goes unseen"*. Rewrite both in place —
  the ERROR-level rule survives the edit and gains a second half (the queue is
  now the backstop the log line was standing in for); the endpoints line gains
  the route's own bullet under **Admin users**' neighbour. Do not merely append.
- **CLAUDE.md's path index has no `/admin/reports` mention at all** (verified),
  so its **Admin users** bullet is where the route joins the cookie-session
  admin surface — not the bearer-token **Admin** bullet below it.
- **`.claude/docs/architecture-database.md` needs no edit.** No migration, no
  column, no index.
- **Tests go in a new `tests/integration/routers/test_admin_reports.py`**,
  following NEU-1162's precedent of giving a distinct surface its own file
  (`test_admin_users_disabled.py`) rather than growing `test_admin_users.py`.
- AC 6 is the §12-style test NEU-1162 asked for on its own predicates: it is the
  one a future reader "fixing" the missing `disabled_at IS NULL` join would
  break, and the only thing between that reader and a queue that hides the
  reports it exists to surface. Write it so the failure message says why.
- `app/errors.py` gains nothing. There is no refusal this route raises that
  FastAPI's own validation and the two existing gates do not already produce.
- The `page` / `per_page` bounds are `GET /shows`'s verbatim (`ge=1, le=1000`
  and `ge=1, le=100`) so the API carries one set of pagination bounds rather
  than two.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

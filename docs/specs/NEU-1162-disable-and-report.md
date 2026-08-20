# NEU-1162 — User disable flag, admin route, and report-user endpoint

**Ticket:** [NEU-1162](https://linear.app/neuroticsasquatch/issue/NEU-1162/backend-add-user-disable-flag-admin-route-and-report-user-endpoint)
**Story:** [NEU-1153](https://linear.app/neuroticsasquatch/issue/NEU-1153/account-moderation-disable-and-report) · **Project:** TVBF: Open Registration · Milestone 1, Trust boundary
**Repo:** `tvbf-backend` only. The frontend half is [NEU-1168](https://linear.app/neuroticsasquatch/issue/NEU-1168/frontend-admin-disable-toggle-and-report-user-action), which cites §7 for the request contract.
**Written:** 2026-08-19

There is no moderation capability. `routers/admin_users.py` exposes two
operations — list users, and toggle `is_admin`. The only remedy against an
abusive account is `DELETE /me`-shaped deletion, which destroys the person's
watch history and is irreversible, which means in practice nobody will reach for
it and there is no remedy at all.

There is also no way for a user to tell Tom that something is wrong. Blocking
exists and works, but blocking is private — it hides the problem rather than
surfacing it.

This spec resolves the ticket's one explicit open question (the Notes' "should a
disabled user's activity disappear from friends' feeds?") in favour of **yes**,
and records why in §4.

---

## 1. What "disabled" means

One nullable timestamp, `app.user.disabled_at`, and one sentence it has to make
true:

> **A disabled account cannot authenticate, cannot mutate its own identity, and
> is invisible to every user who is not already connected to it.**

Everything below is that sentence expressed at the seams that already exist. The
sentence is stated without an asterisk on purpose — §3 exists because the
approximate version ("cannot authenticate, mostly") is the one that rots.

Three properties are load-bearing:

- **Reversible.** Nothing is deleted, copied, backfilled or tombstoned at
  disable time except sessions. Every consequence in §4 is a live join, so
  clearing the flag restores the account exactly, minus the sessions — those are
  gone for good and the user logs in again. This is the whole point: `DELETE /me`
  is not reversible, and a remedy nobody dares use is not a remedy.
- **Not a cascade.** No `ON DELETE` behaviour changes, no user rows are touched
  beyond the one column, no watch history moves.
- **A timestamp, not a boolean.** It records *when*, which is the question a
  moderation action is asked about later.

### 1.1 The record is only the timestamp

No `disabled_by`, no `disabled_reason`. There is one admin, so `disabled_by`
would hold one value forever, and the grounds for the action live in the report
row and the Linear issue it created — in the reporter's own words, which is the
thing an admin actually wants to re-read three months later.

**The accepted cost:** a disable that no report triggered — spam signups, a
display name spotted directly — leaves no trace of why. If moderation ever grows
past one person, both columns are an additive migration, not a rewrite.

## 2. The access check

### 2.1 One predicate, at one seam

`account_service.resolve_session_user` has exactly one caller —
`deps.get_current_user` — so a predicate there covers browse, `/me`,
connections, friend engagement, admin and everything added later, at once. That
is what AC 2 means by "one flag revokes access everywhere at once rather than
route by route".

After `user_repo.get_by_id`, a user with `disabled_at is not None` resolves to
`None`. `get_current_user` then raises its **existing `401 auth_required`**.

### 2.2 Indistinguishable, deliberately

No new status code, no `account_disabled` detail, no new frontend contract —
NEU-1168 needs nothing for this half. A distinct refusal would be the more
honest domain model ("disabled" is not "unauthenticated"), and it is rejected
for the same reason AC 3 refuses to tell a disabled abuser at login: a
machine-readable confirmation on every request tells them precisely what
happened, and they get one per retry.

**The accepted cost:** a user disabled by mistake sees an unexplained logout
rather than a screen. The remedy is a support conversation. If a suspension that
*shows* the user a reason is ever wanted, that is a later ticket buying a
screen, not a change here.

### 2.3 `POST /auth/login`

The check goes in `account_service.authenticate` **after `verify_password`
succeeds and before `clear_for_email`**, raising `InvalidCredentials` — the same
generic `401 invalid_credentials` a wrong password gets.

**Placing it before `verify_password` would be a timing oracle.** Argon2 is
deliberately slow; skipping it would answer a disabled account in milliseconds
where every other 401 takes ~100ms, which tells the abuser exactly what AC 3
says not to tell them. Same work, same timing, same answer.

Three consequences, all deliberate:

- **No `app.login_attempt` row is recorded.** That ledger answers "is this
  *account* being guessed at?" and the guess was correct. Recording it would
  poison the brute-force signal and eventually lock the account out for a reason
  unrelated to guessing.
- **The email-keyed slate is not cleared**, because the refusal lands before
  `clear_for_email` — a disabled account cannot be used as a reset button on a
  login that never succeeded.
- **The IP throttle records an attempt, with no new code.** `InvalidCredentials`
  propagates into the router's existing `except` branch, so an abuser retrying a
  disabled account burns their address budget like anyone else (NEU-1160 §4.3).

## 3. Freezing the emailed-link paths

Three routes take a **token instead of a session**, so they are unauthenticated
and `get_current_user` never runs on them: `POST /auth/reset-password`,
`POST /auth/verify-email`, `POST /auth/email-change/confirm`. `POST
/auth/forgot-password` is unauthenticated too, which is why deleting a disabled
user's outstanding `auth_token` rows buys nothing — they mint a fresh one a
second later.

Left alone, a disabled user could still reset their own password, **verify their
email** (the flag that makes an account visible in people search), and **change
their email address**, detaching the mailbox that identifies them right before
NEU-1158 strips the archive PII.

Each of the three redemption routes therefore refuses a `disabled_at is not
None` user with **its existing generic failure** — no new status code, no new
detail string, nothing an abuser can distinguish from an expired token.
`forgot-password` **silently no-ops** (§8.2): it already returns `202`
unconditionally to avoid enumeration, so the check leaks nothing and costs
nothing, and after this section the link it would mail is dead on arrival.

None of these grant access even without the check. They are closed so the §1
invariant can be stated in one line rather than one line plus an asterisk about
emailed links.

## 4. Invisibility (the ticket's open question)

Disabling revokes *their* access and touches nobody else's read paths. Left
there, a disabled abuser keeps: their activity in every friend's feed, their
library at `/users/{id}/shows` and `/users/{id}/watched`, their name on
`/shows/{id}/friends` and both friend-ratings routes, their entry in people
search, and **any pending connection request already sitting in a stranger's
inbox**.

**That makes "do nothing" a remedy that does not remedy the thing it exists
for.** The reason to reach for disable is that someone is abusing the social
layer, and the harassment lives in its targets' surfaces, not in the harasser's
session.

Four predicates cover all of it. Each is a join to `app.user` on
`disabled_at IS NULL`, and each sits where a rule of exactly this kind already
lives:

| Seam | Covers |
|---|---|
| `connection_service.accepted_friend_ids` | the feed, all four friend-engagement routes |
| `connection_service.are_connected` | the friend library, via `_require_connected_friend` |
| `user_repo.search` | people discovery — beside the NEU-1161 verified predicate already there |
| `connection_repo.list_pending_for_user` | pending requests in a stranger's inbox |

The predicate belongs in the repo/service layer rather than the routers, for the
reason NEU-1161 §3.2 gives about `user_repo.search`: `limit` must still return a
full page.

### 4.1 Accepted connections stay listed

`GET /me/connections` is **not** filtered. Invisibility to strangers is the
goal, and an existing accepted friend is not a stranger — hiding the row makes a
connection vanish and re-appear for someone who did nothing wrong, generating a
support question aimed at the wrong person. A disabled friend reads as a quiet
friend whose library happens to 404, which is indistinguishable from a friend
with no data.

## 5. `app.user_report`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` pk | `gen_random_uuid()` server default |
| `reporter_id` | `uuid` FK → `app.user.id` | `ON DELETE CASCADE` |
| `reported_user_id` | `uuid` FK → `app.user.id` | `ON DELETE CASCADE` |
| `reason` | `text` not null | |
| `created_at` | `timestamptz` not null | `now()` server default |

Plus `Index("ix_user_report_reporter_created", "reporter_id", "created_at")` —
the exact query §6's throttle runs on every report.

**Both FKs cascade**, consistent with every other FK into `app.user` in this
schema. The consequence is stated rather than discovered: **an abuser who
self-deletes takes their reports with them**, and that is accepted because the
Linear issue is the durable record — it holds the reason text and the reporter's
identity, it has states and assignment, and it is what Tom actually reads.

`RESTRICT` on `reported_user_id` was rejected outright: it would let anyone block
another person's account deletion by reporting them, which is a GDPR-shaped
problem. `SET NULL` was rejected as leaving a row pointing at nobody.

**No triage column.** Linear *is* the workflow; a coarse copy of its state in
Postgres gives two records that immediately disagree.

## 6. Rate limiting (AC 7)

The `auth_throttle` pattern ports directly — count rows in a window, no row
lock, `429` + `Retry-After` + `detail: "rate_limited"` — with one
simplification: **the ledger is `app.user_report` itself.** Every report is
already persisted, so there is no `auth_attempt`-style side table and no
"record the attempt" step to get wrong.

The budget is **5 reports per 24 hours per reporter**, via
`REPORT_THROTTLE_MAX` (default `5`) and `REPORT_THROTTLE_WINDOW_MINUTES`
(default `1440`). A daily window because griefing is a volume problem measured
in days; 5 is far above any honest use (most users will file zero for life)
while capping a determined griefer at 5 Linear issues rather than the 72 an
hourly window would allow.

**No duplicate rule.** A second report about the same person is usually *new
evidence about an ongoing problem*, which is exactly what you want to receive.
The budget already bounds it.

The un-locked count/insert race is inherited from `auth_throttle.enforce` and
accepted for the same reason stated there: the overshoot is bounded by
concurrency, and the alternative is a serialisation point.

### 6.1 `IpThrottle` is renamed to `Throttle`

`config.IpThrottle` is a frozen dataclass of `(max_attempts, window_minutes)`
whose name says IP; this budget is keyed on a user, and **NEU-1157** (rate-limit
outgoing connection requests) is a second user-keyed budget in this same
milestone. Rename it — three files, no behaviour change — rather than grow a
second identical dataclass whose only difference is its name. The two `Settings`
properties keep their names.

## 7. Request contract (what NEU-1168 codes against)

### 7.1 `PATCH /admin/users/{user_id}/disabled`

`require_admin_user` + `require_csrf`, mirroring `/admin/users/{user_id}/admin`.

Body: `AdminUserDisabledUpdateRequest { "disabled": bool }`.
Response: `AdminUserOut`, which gains `disabled_at: datetime | None` (AC 8).

| Status | Detail | When |
|---|---|---|
| `200` | — | flag set or cleared |
| `403` | `cannot_disable_self` | `user_id == admin.id` and `disabled is true` |
| `404` | `user_not_found` | unknown id |

Three edge behaviours:

- **Re-disabling someone already disabled leaves `disabled_at` untouched.** It
  records when moderation began, and §1.1 made it the *only* record of the
  action — re-stamping is the one way to destroy the fact of when it happened.
- **Session revocation runs unconditionally whenever `disabled` is true**, not
  only on the transition. Two lines, and it closes the race where a session was
  minted between the flag being set and the delete landing.
- **An admin may disable another admin.** The self-guard covers the mistake
  worth preventing; blocking this would protect a rogue admin from the only
  remedy that exists.

### 7.2 `POST /reports`

`get_current_user` + `require_csrf`. Body `{ "reported_user_id": UUID,
"reason": str }`, reason 1–5000 chars (matching `FeedbackIn.body`).

| Status | Detail | When |
|---|---|---|
| `204` | — | report persisted |
| `400` | `cannot_report_self` | `reported_user_id == user.id` |
| `404` | `reported_user_not_found` | unknown id |
| `429` | `rate_limited` + `Retry-After` | budget spent (§6) |

**Reporting does not require a verified email.** NEU-1161's rule is that a
verified mailbox is the price of *outreach* — of touching another user — and a
report touches Tom, not the reported user. Gating it would invert the protection
in the worst case: the account most likely to be unverified is a **new** one,
which is exactly who a griefer targets, so `require_verified_user` here would
silence the newest users against the abuse they are most exposed to.

Two related rules:

- **Blocks do not suppress reporting**, in either direction. Blocking is private
  and hides the problem; reporting is the escalation path. Letting one defeat
  the other makes both weaker.
- **A disabled user can be reported.** The id resolves and the row is written —
  filtering them would 404 reports about the very accounts under moderation.
  §4 makes them invisible, not nonexistent.

## 8. The notification path

### 8.1 Persist, commit, then notify — always 204

`POST /me/feedback` persists nothing, so its failure modes are honest: `503`
when `linear_feedback_enabled` is false, `502` on `LinearError`. A report
persists, so a straight mirror would either lie ("stored, but here's a 502") or
lose data because a third party was down. Note the sharp edge:
**`linear_feedback_enabled` defaults to `False`**, so a mirrored contract would
503 every report in local dev and CI.

The order is therefore: write the row, **commit**, then attempt the Linear issue
and the maintainer email. A failure in either is logged and never raised. The
reporter is told "received" exactly when we have genuinely received it.

The notification failure is logged at **ERROR**, not the `warning`
`feedback_service` uses — Sentry is initialised in `main.py`, so an ERROR is a
real alert, and under §8.1 a dropped notification is the only way a report goes
unseen.

**The gap this leaves, stated plainly:** the row is unreadable over HTTP. There
is no `GET /admin/reports`, so a report whose notification failed sits in
Postgres until someone opens psql. Closing that is a read route, ticketed as
**NEU-1197** and blocked by this one (§11).

### 8.2 What is actually sent

A new `app/services/report_service.py` reuses the feedback flow's *components* —
the same `LinearClient` for a single `issueCreate`, the same `send_email` and
`email/templates.py` for the maintainer notification. AC 6's "second
notification path" means a second *mechanism* (a webhook, a Slack client, a
second SMTP config), and this adds none.

What it declines to reuse is the **customer modelling**. `submit_feedback` runs
`customerUpsert` on the author and attaches a `customerNeedCreate` — Linear's
"this customer wants this" signal, which feeds the customer-requests view that
exists to rank product demand. A report is a complaint about a third party, not
a need expressed by the reporter, so filing it that way corrupts the signal that
view exists to carry. **No `customerUpsert`, no `customerNeedCreate`.**

- **`LINEAR_REPORT_LABEL_ID`** (optional, default `None`) so reports are
  filterable apart from feedback. Unset means no label.
- **The recipient reuses `FEEDBACK_NOTIFY_EMAIL`.** It means "the maintainer's
  mailbox", and a second env var for the same human is a second place to get it
  wrong. The name is now slightly narrow; noting that beats renaming a live prod
  variable.
- **The route must not use `get_linear_client` as a dependency** — that dep
  raises 503 when no client is configured, which would undo §8.1. It reads
  `request.app.state.linear_client` directly and treats `None` as "skip the
  issue, still send the email".
- The issue **title carries the reported user's id**; the display name appears
  in the body but is never trusted as structure. Display names are
  attacker-authored and a report title is read under time pressure.

## 9. Derived consequences

Two follow from `disabled_at` existing and are in scope, neither named by an AC:

- **The weekly recommendations pass skips disabled users.** `user_repo.list_ids`
  returns every account and the pass spends a DeepInfra call per user with
  changed taste. A disabled account cannot see recommendations, so each one is
  money spent on nobody. One predicate, same as everywhere else. The docstring's
  "every user rather than every user with history" reasoning is untouched — this
  narrows the *universe*, not the floor.
- **`POST /auth/forgot-password` no-ops for a disabled user** (§3).

Docs to update, all read-before-editing per CLAUDE.md:

- `.claude/docs/patterns-auth-and-abuse.md` — the disabled predicate beside the
  two existing auth gates, plus §2.3's timing rule and §6.1's rename.
- `.claude/docs/architecture-endpoints.md` and CLAUDE.md's path index —
  `POST /reports` and `PATCH /admin/users/{user_id}/disabled`.
- `.claude/docs/architecture-database.md` — `app.user_report`.

## 10. Acceptance criteria

1. `app.user.disabled_at` exists as a nullable `timestamptz`; one migration adds
   it together with `app.user_report` and its `(reporter_id, created_at)` index,
   and `app/models.py` declares both so `create_all` and Alembic agree.
2. A request bearing a valid session cookie for a disabled user gets **401
   `auth_required`** — on a browse route, a `/me` route and an admin route
   alike — and the response is byte-identical to one with no cookie at all.
3. `POST /auth/login` with the **correct** password for a disabled account
   returns **401 `invalid_credentials`**, writes no `app.login_attempt` row,
   does not clear that email's existing failures, and records an IP attempt.
4. `PATCH /admin/users/{user_id}/disabled` with `{"disabled": true}` stamps
   `disabled_at`, deletes every session row for that user, and returns
   `AdminUserOut` carrying the timestamp; `{"disabled": false}` clears it.
   Re-disabling an already-disabled user leaves the original timestamp and still
   deletes sessions.
5. An admin disabling themselves gets **403 `cannot_disable_self`**; an admin
   disabling another admin succeeds.
6. `POST /auth/reset-password`, `/auth/verify-email` and
   `/auth/email-change/confirm` refuse a disabled user's valid token with their
   existing generic failure; `/auth/forgot-password` still answers `202` and
   sends nothing.
7. A disabled user disappears from a friend's feed, from
   `/shows/{id}/friends`, from `/users/{id}/shows` and `/users/{id}/watched`
   (404), from `/users/search`, and from a stranger's pending
   `/me/connection-requests` — and **every one of them returns after the flag is
   cleared**, with no backfill step.
8. A disabled user still appears in `GET /me/connections` for an accepted
   friend (§4.1).
9. `POST /reports` with a valid body returns **204** and persists a row; with
   `linear_feedback_enabled=false` it **still returns 204** and still persists.
10. A `LinearError` during notification leaves the row committed, returns 204,
    and logs at ERROR.
11. `POST /reports` returns 400 `cannot_report_self`, 404
    `reported_user_not_found`, and the 6th report inside 24 hours returns **429
    `rate_limited`** with `Retry-After`; an unverified user's report succeeds.
12. A Linear issue is created with no `customerUpsert` and no
    `customerNeedCreate`.
13. `AdminUserOut` carries `disabled_at`; `UserOut` and `AuthedUserOut` do not.
14. The weekly recommendations pass's work list excludes disabled users.
15. `task test`, `task lint`, `task typecheck` all green.

## 11. Out of scope

- **`GET /admin/reports`** — a read surface for the persisted rows. Wanted
  (§8.1), and ticketed as
  [NEU-1197](https://linear.app/neuroticsasquatch/issue/NEU-1197/backend-admin-read-route-for-user-reports),
  which this blocks. NEU-1168 does not close it — it asks for a report *action*,
  not a queue.
- **Reports in `GET /me/export`.** Arguably the reporter's data under
  portability, deliberately omitted: the export is about your library and
  account, a report is a complaint about a third party, and including it hands a
  griefer a tidy record of who they have reported.
- **`disabled_by` / `disabled_reason`** (§1.1) — additive later if moderation
  grows past one person.
- **A user-visible "your account is disabled" screen** (§2.2).
- **Auto-disable on N reports.** Nothing here reads the report table to make a
  decision; disabling stays a human act.
- **Pruning `app.user_report`.** Same standing gap as `app.auth_attempt`
  (NEU-1160 §10).
- **Rate-limiting connection requests** — NEU-1157, which §6.1's rename is
  chosen to serve.
- The admin toggle UI and the report action UI (NEU-1168).

## 12. Notes for the implementation

- `app/errors.py` gains nothing for the login path — `InvalidCredentials` is
  reused verbatim, which is the point of §2.3. The report throttle raises the
  existing `TooManyAttempts`.
- A new `app/repos/user_report_repo.py`: `record`, `count_since`. Pure DB I/O,
  no commits, matching `auth_attempt_repo`'s shape and its docstring
  convention of naming the vocabulary it owns.
- `report_service.submit_report` commits **before** the outbound calls, not
  after — the commit boundary is the contract in §8.1, not an implementation
  detail, and a test should assert the row survives a raised `LinearError`.
- Adding a table touches the `app` schema only, so none of the five
  hand-maintained schema lists change (they are about *schemas*, not tables).
- `docs/adr/` needs no new entry. The §1 invariant and §8.1's commit-then-notify
  rule belong in CLAUDE.md's **Non-obvious patterns** section, which is where
  this repo keeps rules that are load-bearing and easy to undo.
- The four §4 predicates each want a test asserting the *restore* half, not just
  the hide half — reversibility is the property the story is buying, and it is
  the one a later refactor could silently break.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

# A verified email as the price of social access

**Ticket:** [NEU-1161](https://linear.app/neuroticsasquatch/issue/NEU-1161/backend-require-a-verified-email-for-connection-requests-and-search)
**Parent:** [NEU-1152](https://linear.app/neuroticsasquatch/issue/NEU-1152/gate-social-actions-on-a-verified-email) · **Milestone:** 1. Trust boundary · **Project:** TVBF: Open Registration
**Frontend half:** [NEU-1167](https://linear.app/neuroticsasquatch/issue/NEU-1167/frontend-verification-required-states-on-connect-and-people-search) — blocked on this ticket, and cites §4 below.

## 1. Problem

`app.user.email_verified_at` is written by the verification flow and returned on every auth
response, but **no route requires it**. Grepping the backend finds it only in response builders
and in the two services that set it. Email verification is decorative.

That is survivable today, because every account arrived through an invite code Tom issued by
hand. It stops being survivable at open registration ([NEU-1165](https://linear.app/neuroticsasquatch/issue/NEU-1165) makes `invite_code` optional):
an account created with a fake or mistyped address would have full access to the social layer
and could send connection requests to real people.

NEU-1152's decision: **a verified mailbox is the price of touching other users.** Someone who
just wants to try the tracker is not blocked at the door.

## 2. What is gated, and what is not

The gate is on **outreach**, never on **consumption** and never on personal tracking.

| Surface | Gated | Why |
|---|---|---|
| `POST /connection-requests` | **Yes** — 403 `email_not_verified` | Reaching an unconsenting stranger |
| `GET /users/search` | **Yes** — subject filtered out of results | Being discoverable by strangers |
| `POST /connection-requests/{id}/accept` | No | NEU-1152's deliberate asymmetry — see §3.3 |
| `DELETE /connection-requests/{id}` | No | Cancelling or declining is not outreach |
| `POST`/`DELETE /me/blocks/{user_id}` | No | **Defensive.** An unverified user must always be able to protect themselves |
| `DELETE /me/connections/{user_id}` | No | Withdrawal, not outreach |
| `GET /users/{id}/shows`, `/users/{id}/watched` | No | Consumption — see §3.4 |
| `GET /shows/{id}/friends`, `/episodes/{id}/friends/watched` | No | Consumption — see §3.4 |
| Browse, My Shows, Watch Next, Upcoming, all watch tracking, all ratings | No | Personal tracking is never social |

**The gate goes on `POST /connection-requests` specifically, not on the connections router.**
Router-level is the easy mistake here and it would break accepting. The route already carries a
per-route `dependencies=[Depends(require_csrf)]` rather than a router-level one, so the correct
shape is already the local one.

## 3. Decisions

### 3.1 The search route stays open; only its result rows are filtered

An unverified user may **call** `GET /users/search` and gets a normal `200` with a normal
result set — one that contains no unverified people. NEU-1152 names exactly two gated things,
sending a request and *appearing* in results, and searching is neither. It also gives NEU-1167
a live search page on which to disable the connect affordance with an explanation, which is
that ticket's AC 1; a `403` on the route would leave it a dead box.

**Rejected:** `require_verified_user` on the search route as well. Simpler to state, but it
invents a third gated surface the story never listed.

### 3.2 The exclusion is blanket, including for people already connected

An unverified user never appears in `/users/search`, regardless of the caller's relationship to
them. This costs nothing real and is verified, not assumed:

- `GET /me/connections` applies no verification filter, so a connected unverified friend is
  still listed there.
- `GET /users/{id}/shows` and `/users/{id}/watched` gate on
  `connection_service.are_connected`, not on verification, so their library still resolves.

Search is the *stranger-discovery* surface; an existing friend is reachable one tab over.

**Rejected:** exempting accepted connections. It needs the search query to sub-select against
`app.connection` in both directions, and buys a search box that finds someone already visible
elsewhere.

### 3.3 The requester is gated; the addressee is never consulted

`POST /connection-requests` checks the caller's verification and never reads
`addressee.email_verified_at`. This keeps AC 4's "accepting works unverified" honest end to
end: a pending request can still be created, delivered and accepted.

**Recorded so the next reader does not rediscover it:** once §3.2 lands, the accept-while-
unverified carve-out is a **backstop, not a live flow**. Every in-app surface that hands you
another user's UUID — `/me/connections`, `/me/connection-requests`, `/me/blocks`,
`/shows/{id}/friends` — is scoped to an existing relationship, and exact-email search is
filtered too. So a verified user has no in-app way to discover an unverified one. The carve-out
survives for requests already pending when this ships, and for a UUID passed out of band. That
residual path is harmless: the *sender* is verified, which is the abuse the milestone prices.

**Rejected:** requiring both parties to be verified. It does not merely make the carve-out rare,
it deletes it — no request to an unverified user could ever exist, so AC 4 would be untestable
except against rows predating the deploy.

### 3.4 Consumption is not outreach

Reading a friend's library or seeing friend engagement needs only an accepted connection,
exactly as now. Reading data someone consented to share *by accepting* is not touching them,
and gating it would punish the accepter: they said yes, and the person they connected to can
then see nothing.

### 3.5 `email_verified_at` is monotone, and the gate relies on it

Verified against the codebase: **nothing anywhere clears `email_verified_at`.**
`email_verification_service.verify` sets it, and `email_change_service.confirm_email_change`
sets it again on confirm — it does not revoke while a change is pending, so the old address
stays verified throughout. The gate therefore cannot be lost mid-flight, and no route needs a
re-verification path.

If a future ticket ever *does* clear it (a bounce handler, [NEU-1159](https://linear.app/neuroticsasquatch/issue/NEU-1159)'s likely shape), that ticket
inherits this section: it would be introducing the first way to lose social access, and the
frontend states in NEU-1167 would have to cover a user who had it and lost it.

## 4. Error contract (cited by NEU-1167)

```
403 {"detail": "email_not_verified"}
```

- Emitted by: `POST /connection-requests`, and only that route.
- **Never** emitted by any `GET`. `GET /users/search` answers `200` with a filtered list.
- Sits in the existing `deps.py` vocabulary beside `auth_required` (401), `csrf_invalid` (403)
  and `admin_required` (403).
- Precedence at `POST /connection-requests`: CSRF (`csrf_invalid`) and session
  (`auth_required`) are resolved before verification, because `require_verified_user` is built
  on `get_current_user`.

**Correction for NEU-1167:** its AC 2 names `POST /email-verification`. That route does not
exist. The resend route is **`POST /me/email/verification`** (`routers/email_verification.py`)
— authed, CSRF-required, `202` on success, `429 {"detail": "rate_limited"}` when the user has
hit the issue rate limit, which is the 429 that ticket's AC 3 handles.

## 5. Implementation

### 5.1 `require_verified_user` in `deps.py`

Built on `get_current_user` and returning `User`, mirroring `require_admin_user` exactly:

```python
def require_verified_user(user: User = Depends(get_current_user)) -> User:
    """Social-outreach gate. A verified mailbox is the price of touching
    other users (NEU-1152). Distinct from `require_admin_user`, which gates
    on privilege rather than on identity proof."""
    if user.email_verified_at is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="email_not_verified"
        )
    return user
```

Consumed by swapping the route's own `user` parameter, not by adding a bare `dependencies=[…]`
entry — the route needs the `User` anyway:

```python
user: User = Depends(require_verified_user)   # was get_current_user
```

### 5.2 `user_repo.search` filters unconditionally

No `verified_only` flag. `search` has exactly one caller and one meaning — *find a user someone
can act on* — so a defaulted flag would be a knob nobody turns guarding a branch no test could
justify. `admin_users.list_users` builds its own `select` and is unaffected, so nothing today
wants the other behaviour. Same one-definition habit as `recommendations/exclusion.py` and
`sql_fold.py`.

The predicate goes **in the query, not the router**, so `limit` still returns a full page:

```python
stmt = stmt.where(User.email_verified_at.is_not(None))
```

The docstring states it beside the existing enumeration note.

### 5.3 No backfill migration — measured, not assumed

**Measured in prod 2026-08-19: 5 accounts, 0 with a NULL `email_verified_at`.** The gate locks
nobody out, so **no migration ships with this ticket.** This closes the ticket's own note
("Check prod before shipping"): checked, and there is nothing to backfill.

That zero does more than make a backfill unnecessary — it **inverts** the argument for one. A
`UPDATE app."user" SET email_verified_at = now() WHERE email_verified_at IS NULL` run at deploy
could now only ever touch rows created *between this measurement and the deploy*: accounts that
genuinely have not verified, and that the gate exists to catch. Shipping it would not be a
harmless no-op, it would silently exempt the deploy window. Whoever signs up in that window
verifies through `POST /me/email/verification` like everyone after them.

**What the reasoning would have been, kept because NEU-1165 can bring it back:** had the count
been non-zero, the fix was a data migration on `b7d3e02c9a41_rewrite_email_shaped_display_names.py`'s
precedent (NEU-1194 — same table, same shape: a migration fixing the rows already behind a rule,
in the same PR as the rule that closes the door), stamping `now()`. It was defensible on *that*
population specifically because every such row arrived through a hand-issued invite code, a
stronger vetting signal than an email click. Alembic rather than a script because merged ≠ run.
The cost would have been that `email_verified_at` came to mean "clicked a link" **or** "predates
the gate". None of that applies now, and none of it should be revived without re-running the
count — the justification was always about a specific, invite-vetted, already-existing set of
rows, never about NULLs in general.

### 5.4 Test fixtures

`tests/fixtures/users.py`:

- `make_user` gains `verified: bool = False`. **The default stays unverified**, because that is
  what `account_service.signup` actually produces — a factory defaulting to verified would
  create a state real signup never creates.
- `authed_client` builds its user with `verified=True`. It stands for an established logged-in
  account going about its business.
- New `unverified_client` sibling. It and `authed_client` share **one** helper for the session
  row, CSRF token and the cookie-injection event hook — that plumbing is not duplicated.

Blast radius, counted rather than estimated:

- **11** `/users/search` calls in `test_users.py` — search *targets* opt in with `verified=True`.
- **9** `POST /connection-requests` calls (`test_me_blocks.py`, `test_connections.py`) — need no
  change, since the caller is `authed_client`.
- **2** assertions that read `authed_client` as unverified — `test_email_verification.py:178`,
  `test_me_export.py:103` — move to an explicitly unverified user.
- `test_connection_requests.py` is **untouched**: it drives `connection_service` directly rather
  than HTTP.

## 6. Acceptance criteria

- [x] `require_verified_user` exists in `deps.py`, built on `get_current_user`, returning `User`, raising 403 `email_not_verified` when `email_verified_at IS NULL`.
- [x] `POST /connection-requests` requires it; a verified caller succeeds and an unverified caller gets 403 `email_not_verified`.
- [x] `POST /connection-requests` still succeeds when the **addressee** is unverified.
- [x] `GET /users/search` returns 200 for an unverified caller, and no unverified user appears in any result — including a match on exact email, and including a user the caller is accepted-connected to.
- [x] The verified predicate is in the SQL, not the router: a page of `SEARCH_LIMIT` verified users is returned even when unverified rows would otherwise have filled it.
- [x] `POST /connection-requests/{id}/accept` succeeds for an unverified accepter.
- [x] `POST /me/blocks/{user_id}` succeeds for an unverified caller.
- [x] At least one personal-tracking route (My Shows, or an episode watch) is asserted to work unverified.
- [x] `GET /users/{id}/shows` resolves for an unverified caller with an accepted connection.
- [x] Prod counted before shipping — **2026-08-19: 5 accounts, 0 unverified.** No migration is required, and per §5.3 none is written.
- [ ] Re-counted immediately before deploy if more than a few weeks pass, since §5.3's conclusion rests on the count rather than on a property of the data.
- [x] `task test`, `task lint`, `task typecheck` green.

## 7. Not in scope

- **Any change to NEU-1152's gated list.** Moderation (`NEU-1162`), the outgoing-request rate
  limit (`NEU-1157`) and handles (`NEU-1163`) are separate tickets in this milestone.
- **Revoking verification.** §3.5 — nothing clears the column and this ticket does not add a way.
- **Bounce handling.** NEU-1159.
- **The frontend states.** NEU-1167, which cites §4.

## 8. Notes for the next ticket

`NEU-1157` rate-limits outgoing connection requests on this same route. The two gates are
independent and both belong on `POST /connection-requests`; verification is an identity check
and should resolve **before** the rate limit is consulted, so an unverified caller does not burn
quota on a request that can never succeed. That is the same ordering rule NEU-1160 drew between
its IP throttle and Turnstile, in the opposite direction and for the same reason — spend the
cheap local check first.

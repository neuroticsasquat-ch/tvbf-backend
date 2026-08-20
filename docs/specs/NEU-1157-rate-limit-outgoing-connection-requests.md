# NEU-1157 — Rate-limit outgoing connection requests

**Ticket:** [NEU-1157](https://linear.app/neuroticsasquatch/issue/NEU-1157/rate-limit-outgoing-connection-requests)
**Project:** TVBF: Open Registration · Milestone 1, Trust boundary
**Blocks:** [NEU-1156](https://linear.app/neuroticsasquatch/issue/NEU-1156/make-the-invite-code-optional-at-signup)
**Repo:** `tvbf-backend` only. There is no frontend sibling ticket; §6 is the contract one would code against.
**Written:** 2026-08-20

`POST /connection-requests` has no rate limit. Combined with `GET /users/search`,
one account can walk display names and send requests to everybody it finds. Once
signup stops requiring an invite code this is the primary harassment vector, and
it is the last engineering blocker in Milestone 1.

The existing defences do not cover it. Blocking is reactive — it only helps after
the victim has already received the request. `require_verified_user` (NEU-1161)
costs an attacker a working mailbox per account but nothing per request. The IP
throttle (NEU-1160) governs signup and login, not outreach. Disabling an account
(NEU-1162) is a remedy after the fact, and one that needs a human in the loop.

This spec also **corrects a factual error in the ticket's own problem
statement**, and grows scope to close the hole it revealed. See §4.

---

## 1. What is actually being defended

Two different attacks, which the ticket's four acceptance criteria only address
one of.

**Spraying** — many requests to many strangers. Bounded by a daily cap (§3.1),
tightened for accounts whose targets visibly reject them (§3.2). This is AC 1–4.

**Targeting** — many requests to one person who has already said no. Not covered
by any of the four ACs, and not covered by the existing 409 either, contrary to
what the ticket says. Closed by §4.

Both are governed through one new table, because both need the same thing the
database does not currently retain: a record that a request existed.

## 2. The ledger — `app.connection_request_log`

### 2.1 Why a new table at all

`app.connection` is a **pair row under a unique unordered-pair index**, and it is
**deleted** on both decline and cancel — both go through the same
`DELETE /connection-requests/{id}` → `connection_service.delete_pending_request`.
Accepting keeps a row; declining and cancelling erase one. So once a request is
resolved, the database retains no evidence it ever happened.

That breaks two ACs directly:

- **AC 3** — "the cap counts requests *created*, so cancelling and re-sending
  cannot be used to reset it" — cannot be satisfied by counting `connection`
  rows, because cancelling deletes the row being counted.
- **AC 2** — repeated rejection tightens the cap — needs the *outcome* of past
  requests, and the two outcomes that matter most (declined, cancelled) are
  exactly the ones erased, through one code path that cannot tell them apart from
  the row alone. Only `caller_id` distinguishes them.

**Rejected: make `app.connection` itself the ledger** by retaining rows in new
terminal states `declined` and `cancelled`. This is the shape `app.user_report`
uses ("the ledger is the table itself, so there is no record-the-attempt step to
get wrong") and it was the first thing tried. It collides with
`uq_connection_unordered_pair`: a retained `declined` row means `find_pair`
returns non-`None` forever, so `send_request` 409s and a declined request could
**never** be re-sent. That is a product decision — it silently converts every
decline into a permanent block — and this ticket has no mandate to make it. §4
reaches a similar destination deliberately, bounded in time, rather than as a
side effect of a schema choice.

**Rejected: no stored outcome**, deriving "rejected" from the absence of a
surviving `accepted` row. It cannot distinguish "they declined me" from "I
cancelled it myself" from "still pending", which is the exact distinction AC 2
calls "the signal that separates an enthusiastic new user from a spammer".

### 2.2 The row

```
app.connection_request_log
  id              bigserial primary key
  requester_id    uuid  not null  → app.user.id  ON DELETE CASCADE
  addressee_id    uuid  not null  → app.user.id  ON DELETE CASCADE
  outcome         text  not null  ck: in ('pending','accepted','declined','cancelled','blocked')
  created_at      timestamptz not null default now()
  resolved_at     timestamptz null
  index ix_connection_request_log_requester_created (requester_id, created_at)
```

`outcome` is **`Text` + a `CheckConstraint`, not a Postgres enum**, unlike
`app.connection.state`. This follows `app.auth_attempt`, the other throttle
ledger, whose docstring states the reason: widening a check constraint is a
visible, deliberately loud migration, where `ALTER TYPE ... ADD VALUE` is a
one-liner that slips through review. A sixth outcome should be hard to add
casually.

`addressee_id` is stored even though **no throttle arithmetic ever reads it** —
every count in §3 is per-requester. It is stored because it is the single most
useful thing an admin has when a report arrives: "who did this account spray, and
how did each one go" is the question moderation actually asks, and `app.connection`
cannot answer it once rows are deleted.

**The privacy cost, stated rather than discovered:** today, cancelling a request
erases the fact you ever asked. With this table, that fact survives. It is exposed
by no route, holds no free text, and is bounded by account lifetime (§2.5) — but
NEU-1155 writes the privacy page and this needs to be true on it.

The index leads `(requester_id, created_at)` because that is the shape of both
queries in §3.

Per the repo rule, **every constraint and index is named explicitly in
`app/models.py`** — `create_all` builds them in the test suite and Alembic builds
them in prod, and the two must agree.

### 2.3 The five outcomes

Every transition that exists in the code today gets its own stored value.

| Path | Who acted | Ledger transition |
|---|---|---|
| `send_request` | requester | insert `pending`, `resolved_at` null |
| `accept` | addressee | `pending` → `accepted` |
| `delete_pending_request`, `caller_id == addressee_id` | addressee | `pending` → `declined` |
| `delete_pending_request`, `caller_id == requester_id` | requester | `pending` → `cancelled` |
| `block`, blocker is the **addressee** of a pending row | addressee | `pending` → `blocked` |
| `block`, blocker is the **requester** of a pending row | requester | `pending` → `cancelled` |

`resolved_at` is set to `now()` on every transition out of `pending`.

Two of these deserve their reason in writing.

**`block` is symmetric in the code and asymmetric in meaning.**
`connection_service.block(blocker_id, blocked_id)` deletes any existing pair row
before inserting the blocked one, so it is a request-lifecycle event whether or
not it was written as one. If the **addressee** blocks, that is the strongest
"unwanted" signal the system can observe. If the **requester** blocks someone they
had asked, they withdrew — recording that as `blocked` would charge them an
adverse outcome for their own decision to disengage.

**`cancelled` stays distinct from `declined`** rather than being folded in. The
scoring rule (§3.2) is a number to be tuned against a userbase that does not exist
yet, and every conflation made now is a measurement that can never be taken later.
Likewise `blocked` stays distinct from `declined`: if a hard rule is ever wanted
(one block and the ceiling collapses), it is unimplementable if the signal was
merged at write time.

### 2.4 Outcomes are terminal, and a missing row is normal

**Nothing ever reverts.** `unblock` writes nothing. `remove_connection`
(unfriending after an acceptance) writes nothing — the request *was* accepted, and
that stays true. Reverting a `blocked` row to `pending` on unblock was rejected as
incoherent: the `connection` row was deleted at block time, so there is no pending
request to revert *to*, and the resurrected row would immediately start aging
toward `ignored` (§3.2). Reverting it to `cancelled` avoids that incoherence but
launders an adverse signal on the strength of the *victim's* gesture, which is
backwards. The mark rolls off the reputation window in 30 days regardless, which
is the right amount of forgiveness and it is automatic.

**The outcome writers must tolerate a missing ledger row and no-op**, never raise.
Three ordinary situations hit "update the row for this pair" and find nothing: a
request created before this migration (§8), a row already cascaded away by an
account deletion, and a `block` where no request ever existed — which is the
common case for `block`. `scalar_one()` there turns an ordinary accept into a 500.
This is a normal state, not an error, and it is not logged as one.

### 2.5 Retention

**Rows are kept for the life of both accounts, and are not otherwise pruned.**

Both FKs are `ON DELETE CASCADE`, consistent with every other FK into `app.user`
and with `app.user_report`, whose docstring makes the same choice and states its
consequence out loud. That makes the retention sentence "for as long as both
accounts exist", which is a sentence NEU-1155 can publish.

The accepted cost: if a *target* deletes their account, the row recording that
they declined or ignored you disappears, nudging a spammer's adverse rate down. It
is bounded — it requires the victim to delete their entire account, which is rare
and is itself a signal something went wrong — and the alternative is a nullable
`addressee_id` plus a second code path for "the person I asked no longer exists".

**Rejected: a pruning job.** Deleting rows older than the reputation window would
mean a fifth Coolify scheduled task with its own healthcheck deadman — this repo's
rule against a shared check is emphatic — a run-row kind and a runbook entry, all
to delete a few thousand rows nobody reads. There is also a substantive argument
against pruning: this table is moderation evidence, and a griefer's history being
auto-erased on a 30-day timer is precisely the wrong property for the record an
admin consults when deciding whether to disable an account.

**Rejected: no FKs at all**, the `app.watch_archive` shape. That table's
FK-lessness serves a one-off migration backstop and its own docstring flags the
retained-PII problem as something to be dropped by hand — NEU-1158, in this same
project. Copying that shape would be copying a known liability.

## 3. The two budgets

A request is refused when the requester has already **created** `ceiling` requests
in the last 24 hours. `ceiling` is not a constant: it is one of two values,
selected by the requester's recent reputation.

### 3.1 The daily cap (AC 1, AC 3)

Count `connection_request_log` rows where `requester_id = caller` and
`created_at >= now() - 24h`. **All five outcomes count** — the count is of rows
*created*, exactly as AC 3 requires, so cancelling a request does not return its
slot and cancel-and-re-send is worthless as a reset.

Rolling 24 hours (`window_minutes = 1440`), not a calendar day: a calendar day
hands everyone a fresh allowance at midnight and makes the burst predictable.

**A refused request writes no ledger row.** A 429, a 404 (`addressee_not_found`),
a 400 (`self_connection_forbidden`), a 409 (`connection_exists`) and a cooldown
refusal (§4) all create nothing and therefore cost no budget. This matches
`report_service`, where only persisted reports count, and it is AC 3 read
literally. It differs from `auth_throttle`, which records failures — that table
genuinely holds *attempts*, and this one does not, which is also why it is not
named for them (§7).

### 3.2 The ceiling (AC 2)

Over a **30-day** window anchored on `created_at`, partition the requester's rows:

- **accepted** — `outcome = 'accepted'`.
- **adverse** — `outcome IN ('declined','blocked')`, plus **ignored**: still
  `pending` and `created_at` older than **14 days**.
- **neither** — `outcome = 'cancelled'`, and `pending` rows younger than 14 days.

`sample = accepted + adverse`. The ceiling is the **floor value** when
`sample >= 5` **and** `adverse * 100 >= sample * 50`; otherwise the **max value**.

In prose: *more of your recent requests are being rejected or ignored than are
being accepted, across at least five that have actually resolved.*

Evaluate the threshold in **integer arithmetic** (`adverse * 100 >= sample * PERCENT`),
never a float rate, so there is no rounding behaviour to argue about at the
boundary and the test can assert the boundary exactly.

Six decisions are embedded there, each with its alternative:

**Rate, not absolute count.** `ceiling = max(FLOOR, MAX − adverse)` is simpler and
needs no minimum sample, but it keys on the absolute number of rejections, so a
genuinely popular user who sends 30 requests and gets 25 accepted is demoted for
the 5 that were not. The ticket's own words are "at a high rate", and its stated
purpose is to *distinguish* the enthusiastic new user from the spammer — which is
precisely the distinction a subtraction rule cannot draw, because to it volume and
rejection are the same axis.

**Demotion, not earn-your-ceiling.** Starting everyone at the floor and letting
acceptances raise it is strictly the stronger security posture: it is the only
shape that bounds a day-one spammer *on day one*, before any signal has had time
to accrue. It was rejected as a product decision this ticket was not scoped to
make — it taxes the honest new user hardest at exactly the moment the app most
needs them to build a friend graph. **The consequence is that a fresh account has
no adverse rate at all and runs its first day at the full ceiling**, so the choice
of `MAX` (§3.3) is what actually carries the security weight.

**`cancelled` is excluded from both numerator and denominator.** AC 3 and AC 2 are
two different defences and stay that way: AC 3 already makes cancel-and-re-send
worthless, because the cancelled request consumed a slot that never comes back.
Making cancel *additionally* count as a rejection charges twice for one act, and
the person it reaches is the honest user fixing a mistake — the spammer has no
reason to cancel at all, since leaving requests sitting costs them nothing. Putting
it in the denominator only is the subtle version of the same overcharge and is
harder to explain to anybody, including future readers.

**The hole this leaves, chosen knowingly:** an account can send its daily 10, cancel
all 10, and its adverse rate is unmoved — 300 creations over 30 days with a
pristine reputation. It is acceptable *because the daily cap is the thing bounding
harm*; the reputation rule exists only to lower that cap for accounts whose
recipients are visibly rejecting them.

**Young pending rows are in neither bucket.** If they sat in the denominator, every
account's rate would be dominated by requests nobody has had a chance to answer
yet, and an honest burst would self-demote within minutes.

**14 days for "ignored".** A recipient who is annoyed declines or blocks within
minutes, and that signal is instant; "ignored" is the different case — nobody cares
enough to say no — which is a spammer's normal outcome. 7 days reads "hasn't opened
a TV-tracking app in a week" as a rejection, and for this app that is an ordinary
week; there is no notification channel that would make a week meaningful. 30 days
makes the signal vestigial.

**Anchored on `created_at`, not `resolved_at`.** A row's window membership is then
stable — it never changes except by aging out — and the rate becomes a statement
about *your sending behaviour*, which is what is being governed. The cost: a request
sent on day 29 and declined on day 31 never counts against anybody, because the row
has left the window by the time the signal arrives. Anchoring the numerator on
`resolved_at` and the denominator on `created_at` fixes that leak by drawing the two
from different populations, which is correct on the day it is written and
indefensible six months later.

**A rolling window means automatic recovery.** A demoted account returns to the full
ceiling once the bad rows age out, without changing its behaviour. That is correct
here: the real remedy for a persistent abuser is report → disable (NEU-1162), which
already exists. This throttle is a speed limit, not a sentence.

### 3.3 Known consequences of the ceiling

**A mid-day demotion binds immediately.** If the ceiling drops from 10 to 2 after 5
requests have already been sent today, the next request is refused —
`count >= ceiling`, no grandfathering. Nothing already sent is retracted; the account
is simply done for the day.

**A request sent to someone who is later disabled will age into `ignored`.**
NEU-1162 hides a disabled user's pending requests from both inboxes, so it can never
be answered, and the requester is marked for something the addressee did. Accepted
rather than fixed with a join excluding disabled addressees from the denominator: the
direction of harm is rare (it requires having requested someone who was subsequently
moderated), a single such row cannot demote anyone given the minimum sample of 5, and
the join would sit on the read path of every create.

### 3.4 The numbers

| Knob | Default |
|---|---|
| `CONNECTION_REQUEST_THROTTLE_MAX` | `10` per day |
| `CONNECTION_REQUEST_THROTTLE_FLOOR` | `2` per day |
| `CONNECTION_REQUEST_THROTTLE_WINDOW_MINUTES` | `1440` |
| `CONNECTION_REQUEST_REPUTATION_WINDOW_DAYS` | `30` |
| `CONNECTION_REQUEST_IGNORED_AFTER_DAYS` | `14` |
| `CONNECTION_REQUEST_REPUTATION_MIN_SAMPLE` | `5` |
| `CONNECTION_REQUEST_ADVERSE_PERCENT` | `50` |
| `CONNECTION_REQUEST_DECLINE_COOLDOWN_DAYS` | `30` |

**These are asserted, not measured.** There is no traffic to measure. They are
`Settings` fields with env overrides precisely so they are tuned once there is, and
nothing in this design depends on any particular value.

The reasoning behind the two that matter, against what an attacker already pays:
signup is behind Turnstile and a 5/hour IP throttle (NEU-1160); outreach requires a
verified mailbox (NEU-1161), so every throwaway account costs a real inbox;
`/users/search` needs a ≥2-character query and returns at most 20 verified,
non-disabled users, so targets are found by name one at a time — there is no bulk
path and no follower list to scrape. Honest usage is a handful of requests during
onboarding and approximately zero thereafter; there is no import flow.

**10/day** is twice the report budget, well above any honest first day given that
each request costs a name search, and well below the volume at which a stranger
receiving requests reads the app as a spam vector. **2/day** rather than 1 because
*being ignored* is one of the adverse signals, and being ignored is something a
perfectly honest user's friends do to them — a floor of 1 makes a false positive feel
like a punishment, where 2 makes it feel like a speed limit and still allows ~14
additions a week.

**Minimum sample 5, threshold 50%.** The asymmetry favours aggressiveness: a false
positive drops an honest user to 2/day for up to 30 days, and since honest usage
after onboarding is roughly zero requests, they will very likely never notice. A
minimum of 10 would be larger than the entire current userbase, making the rule dead
code — and a rule that cannot fire is worse than no rule, because it reads as
protection. A minimum of 3 triggers on 2-of-3, and three is not a sample.

## 4. The decline cooldown — correcting the ticket's premise

**The ticket's Problem statement is wrong** where it says:

> The 409 on a duplicate relationship stops re-sending to the *same* person, not
> spraying many different people.

The first half is false. Verified by reading the path, not inferred:
`delete_pending_request` **deletes** the `connection` row, so `find_pair` returns
`None` afterwards and `send_request` succeeds. No test in
`tests/integration/routers/test_connection_requests.py` or
`tests/integration/app/services/test_connection_service.py` covers re-requesting
after a decline. The 409 holds only while a request is **pending** or **accepted**.

So under §3 alone, a requester may send **10 requests a day to one person who has
explicitly said no**, every day, indefinitely. The reputation rule barely touches it:
those declines do accumulate and would demote the account, but the floor is still 2 a
day at the same unwilling person, forever. The aggregate cap does not cover the
targeted case, which is if anything the worse harassment.

**Therefore, in scope:** a new request to someone who declined the requester within
the last `CONNECTION_REQUEST_DECLINE_COOLDOWN_DAYS` (30) is refused.

It is refused with **the same vague `409 connection_exists`** the pair check already
returns. That reuse is deliberate and load-bearing: the caller cannot distinguish
"they declined me" from "we already have a relationship" from "they blocked me",
which is exactly the property the ticket's Notes demand. It leaks nothing new either
— the requester already learns of a decline by watching the request vanish from their
outgoing list in `GET /me/connection-requests`.

The cooldown applies to `declined` **only**:

- not `cancelled` — your own withdrawal must not lock you out of correcting it;
- not `blocked` — the `blocked` row persists in `app.connection`, so `find_pair`
  already 409s that pair for as long as the block stands.

Its window is **its own knob**, not a reuse of the reputation window, despite both
defaulting to 30 days. They answer different questions and will be tuned apart.

**Rejected: permanent.** One decline and that pair can never be re-requested unless
the decliner initiates. It overreaches — `block` already exists as the explicit
permanent tool and it belongs to the recipient, so making a decline permanent quietly
converts a soft "no" into a hard one the decliner never chose.

**Rejected: leave it out of scope and file a follow-up.** That ships a ticket titled
"rate-limit outgoing connection requests" which leaves the most direct harassment
vector at 10/day, while the ticket text claims that vector is already closed. The next
person to read it believes they are protected.

**The accepted cost:** someone who declines by accident — and the decline control sits
next to the accept control — cannot be re-asked for 30 days, and neither party is told
why.

## 5. Check order, and the non-oracle property

`POST /connection-requests` currently answers, in order: `403` (unverified caller, a
router dependency), `404` `addressee_not_found`, `400` `self_connection_forbidden`,
`409` `connection_exists`, `201`.

**The throttle is checked first — before the addressee lookup, before the self check,
before the pair check — and the cooldown second.**

This is the ticket's Notes made concrete. If the 429 were raised **last**, an account
at its cap would get `409` for any target it already has a relationship with and `429`
for every other target. That is a free, silent oracle: probe any user id and the status
code tells you whether they have blocked you, with no request created, nothing appearing
in anyone's inbox, and no cost. Today that probe exists but is *not* free — distinguishing
409 from 201 requires actually creating a request the target can see. Being at the cap
would convert a costly probe into a costless one.

With the throttle first, every target returns an identical `429` at the cap and the
endpoint tells a spent-out account nothing whatsoever about anybody.

The cost: a caller at their cap who requests *themselves* gets `429` instead of `400`.
Harmless, and arguably more truthful — the cap is the binding constraint at that point.

The final order is: **429 → cooldown 409 → 404 → 400 → pair-check 409 → 201.**

**The pre-existing 409-vs-201 probe stays exactly as costly as it is today and no
cheaper.** Closing that enumeration path entirely is out of scope (§10); it is already
blunted by `/users/search` excluding blocked users from its results, so the probe
requires knowing a UUID that was never shown to you.

## 6. Request contract

`POST /connection-requests`, on refusal by either budget:

```
429 Too Many Requests
Retry-After: <window_minutes * 60>
{"detail": "rate_limited"}
```

Byte-identical to `POST /reports` and the auth throttle. `Retry-After` carries the
**whole window in seconds, not a computed remainder** — the exact answer needs the
oldest row in the window, and a second query to shave seconds off a rejection buys
nothing, exactly as `auth_throttle.enforce` and `report_service.submit_report` both
already reason.

Cooldown refusals are `409 {"detail": "connection_exists"}` — indistinguishable from
the existing pair-conflict response, per §4.

**Nothing about the budget is exposed.** No `X-RateLimit-*` headers, no field on
`GET /me`, no introspection endpoint. Exposing the *limit* exposes the ceiling, and the
ceiling is where the reputation verdict is written: an account told "your limit is 2"
has been told the tightening fired, which is a signal a spammer can tune against — send
just under the floor, stay invisible, and use the demotion itself as feedback on which
behaviours trip it. The value of a reputation rule is that the person being measured
cannot see the gauge.

**The accepted cost:** the honest user meets an unexplained wall with no warning, and the
only thing a UI can honestly say is "you've sent too many requests, try again later". It
is acceptable here specifically because an honest user sends roughly ten requests in their
lifetime and will never see it — but it means a future frontend ticket has thin material,
which is better known now than discovered during NEU-1156.

## 7. Naming and placement

**`app.connection_request_log` / `ConnectionRequestLog`.**

`ConnectionRequest` was rejected: `ConnectionRequestOut` already exists in
`app/schemas.py` as the API shape for a live `app.connection` row, and two names one
character apart meaning different things is how a maintainer ships a bug.
`ConnectionAttempt` was rejected as **false** — it would mirror `auth_attempt` and
`login_attempt` nicely, but an "attempt" table implies it records tries, and this one
records only requests that were *created* (§3.1). `auth_attempt` genuinely holds
attempts including failures; naming this one to match advertises a symmetry that is not
there, and someone would eventually "fix" the missing failure rows.

- **`src/tvbf/app/services/connection_throttle.py`** — new module mirroring
  `auth_throttle.py`, owning `current_ceiling()` (the §3.2 reputation computation) and
  `enforce()`. Not inlined `report_service`-style, because unlike the report budget this
  derivation is non-trivial and worth testing on its own; not folded into
  `connection_service`, already the largest service in `app/`.
- **`src/tvbf/app/repos/connection_request_log_repo.py`** — thin, pure DB I/O, no
  commits, per the repo convention.
- **The ledger writes live in `connection_service`**, beside the lifecycle transitions
  they mirror. Putting them in the throttle module would mean `block()` imports a
  throttle in order to record a block, inverting the dependency.

`config.Throttle` is reused unchanged — NEU-1162 §6.1 renamed it from `IpThrottle`
*naming this ticket as the reason*, and this is that second user-keyed budget. Because
the ceiling varies, `Settings` exposes **two** properties of that type,
`connection_request_throttle` (at `MAX`) and `connection_request_floor_throttle` (at
`FLOOR`), and `current_ceiling` returns one of the two — so the reputation rule reads as
*selecting between two budgets*, and the count/window pair still cannot drift apart at a
call site.

The four reputation knobs get a second frozen dataclass in `config.py`,
`ReputationRule(window_days, ignored_after_days, min_sample, adverse_percent)`, exposed as
`Settings.connection_request_reputation`, for `Throttle`'s own stated reason: four loose
integers passed positionally is the thing that dataclass exists to prevent.

## 8. Migration

One Alembic migration creating the table, its check constraint and its index. **No
backfill.**

The ledger starts empty. Existing accounts have no history, so no adverse rate, so the
full ceiling — which is what they would get under any option anyway, since the minimum
sample is 5 and the entire userbase is smaller than that. Every backfill variant produces
an identical observable outcome for every real user, so backfill code, backfill tests and
a data-writing migration would be bought for a difference that cannot be observed.

Backfilling `pending` rows would be actively *worse* than nothing: it resurrects old
pending requests as aging liabilities, so someone whose friend never answered a request
from May could be demoted by a migration.

**What this gives up:** for the first 14 days after deploy there is no `ignored` signal for
anybody, because no ledger row is old enough to age. Declines and blocks work immediately.
Given the userbase, that costs nothing real.

Requests that predate the migration are the main source of the missing-row no-op in §2.4.

## 9. Acceptance criteria

1. `POST /connection-requests` returns `429` with `Retry-After` and
   `{"detail": "rate_limited"}` once the caller has created `ceiling` requests in the
   trailing 24 hours. *(ticket AC 1)*
2. An account whose recent requests are declined, blocked or ignored at or above the
   adverse threshold — over a sample of at least `MIN_SAMPLE` resolved-or-aged requests in
   the trailing reputation window — is held to `FLOOR` rather than `MAX`. *(ticket AC 2)*
3. Creating a request, cancelling it, and creating another consumes **two** slots, not one.
   *(ticket AC 3)*
4. Accepting, declining and cancelling are never rate-limited; only creation is. *(ticket AC 4)*
5. At the cap, a request to a non-existent user, to oneself, and to an existing/blocked
   relationship all return the **same** `429` — the endpoint is not an oracle. *(ticket Notes)*
6. A request to someone who declined the caller within the cooldown returns
   `409 {"detail": "connection_exists"}`, indistinguishable from an existing relationship;
   after the cooldown expires the same request succeeds. *(§4)*
7. A request refused by any of `429`, cooldown-`409`, `404`, `400` or pair-`409` writes no
   ledger row and consumes no budget.
8. `block` by the addressee of a pending request records `blocked`; `block` by the requester
   of a pending request records `cancelled`.
9. Accepting a request that has no ledger row — the pre-migration case — succeeds and writes
   nothing.
10. `task lint`, `task typecheck` and `task test` pass; `alembic heads` shows a single head.

### Test surface

- Repo: both counts, and the index-shaped queries.
- `connection_throttle.current_ceiling` at every boundary — below `MIN_SAMPLE`, at the
  threshold exactly, either side of it, and with `cancelled` rows present to prove they
  move neither numerator nor denominator.
- Aging: a `pending` row at 13 days and at 15 days.
- The four lifecycle transitions plus the `block`-by-requester-is-a-cancel case.
- The missing-ledger-row no-op.
- The `429` shape and `Retry-After` value.
- Check ordering — the `429` masks `404`, `400` and `409` at the cap.
- The decline cooldown, including its expiry.

## 10. Out of scope

- **The 409-vs-201 enumeration probe.** Pre-existing, unchanged by this ticket, and
  blunted by `/users/search` excluding blocked users. Named here so it is not mistaken for
  something this spec closed.
- **Any frontend work.** There is no sibling ticket. §6 is the contract one would code
  against; the copy it can honestly show is thin, by §6's own choice.
- **Pruning the ledger.** §2.5.
- **Rate-limiting `GET /users/search` itself.** A separate surface with a separate threat
  model.
- **Earn-your-ceiling.** Named in §3.2 as the stronger security posture and deliberately
  not taken; revisit if spraying is observed in the first days after open registration,
  which is the window this design leaves widest.
- **Admin visibility into the ledger.** NEU-1197 adds an admin read route for reports; a
  companion view of a reported account's request history is the obvious follow-up and is
  not built here.

## 11. Notes for the implementation

- `connection_service.delete()` is **dead code** — nothing in `src/` or `tests/` calls it;
  the router uses `delete_pending_request`. Do not give it a ledger write. Deleting it is
  reasonable and out of scope.
- The count-then-insert race is inherited from `auth_throttle.enforce` and accepted for the
  reason stated there: concurrent requests can read the same count and all pass, the
  overshoot is bounded by concurrency, and a row lock on every create would put a
  serialisation point on the route an attacker is already flooding. Do **not** add
  `SELECT ... FOR UPDATE`.
- Compare against the ceiling with `count >= ceiling`, matching both existing throttles.
- `resolved_at` is set by the service, not a database trigger — every other timestamp in
  `app/` that records an action is written by the code that performs it.
- Run `task format` before committing; pre-commit checks formatting but does not fix it.

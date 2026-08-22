# NEU-1160 — Verify Turnstile tokens and throttle auth by IP

**Ticket:** [NEU-1160](https://linear.app/neuroticsasquatch/issue/NEU-1160/backend-verify-turnstile-tokens-and-add-an-ip-keyed-auth-throttle)
**Story:** [NEU-1151](https://linear.app/neuroticsasquatch/issue/NEU-1151/signup-abuse-protection) · **Project:** TVBF: Open Registration · Milestone 1, Trust boundary
**Repo:** `tvbf-backend` only. The frontend half is [NEU-1166](https://linear.app/neuroticsasquatch/issue/NEU-1166/frontend-add-the-turnstile-widget-to-the-signup-form), which cites this spec for the request contract.
**Written:** 2026-08-19

`POST /auth/signup` has no abuse protection at all. The invite code is the only
thing standing between it and the open internet, and NEU-1156 removes it.

Sitting next to that is a second hole that is live **today**: the lockout in
`account_service.authenticate` is keyed on **email only**, so credential
stuffing that walks a different address on every request is entirely
unthrottled. That one is not waiting on open registration to become exploitable.

This spec supersedes the ticket in one place, recorded in §9: it does **not**
add a Turnstile *site* key to `config.py`.

---

## 1. Two mechanisms, one ticket

They are complementary and neither substitutes for the other.

| | Turnstile | IP throttle |
|---|---|---|
| Answers | "is this a browser with a human behind it?" | "how much has this address already done?" |
| Applies to | `/auth/signup` | `/auth/signup` **and** `/auth/login` |
| State | none — one outbound verification per attempt | rows in `app.auth_attempt` |
| Skippable by config | **yes** (AC 5) | no — it is local, offline, and works in tests |
| Failure surface | 400 (rejected), 503 (unverifiable) | 429 |

The existing email-keyed lockout (5 failures / 15 min, `app.login_attempt`) is
**untouched**, per AC 3. Three gates now guard `/auth/login`, and the order they
fire in is §4.3.

## 2. The client IP

Every request reaching the app has been through exactly one proxy — Traefik, in
both environments (Coolify's in prod, `tbc-localdev-infra`'s locally). Uvicorn is
started without `--proxy-headers` in `docker-compose.yml` and in the `Dockerfile`,
so `request.client.host` is that proxy for **every** request and is useless as a
throttle key. Every current use of it (`auth.py` ×3, recording `session.ip` and
`login_attempt.ip`) has therefore been recording the proxy's address, which is
worth knowing when reading those columns.

### 2.1 The rule

One helper, `src/tvbf/client_ip.py`:

```python
def client_ip(request: Request, *, trusted_proxy_hops: int) -> str | None:
```

Read `X-Forwarded-For`, split on `,`, strip, drop empties, and take the entry
`trusted_proxy_hops` from the **right**. Fall back to `request.client.host` when
the header is absent or holds fewer entries than the hop count.

Right-most, not left-most, is the whole point. The left-most entry is whatever
the client sent, which an attacker authors freely; the right-most is what the
nearest proxy observed. With one hop that is the real peer:

```
X-Forwarded-For: 1.2.3.4, 9.9.9.9
                 ^^^^^^^  forged by the client
                          ^^^^^^^ appended by Traefik — the peer it actually saw
hops=1 → 9.9.9.9
```

This holds whether Traefik appends to an incoming header or replaces it
outright, which is why the rule is expressed in hops rather than in Traefik's
configuration. `TRUSTED_PROXY_HOPS` defaults to `1` and exists so that putting
Cloudflare (or any second proxy) in front of the API later is a config change
rather than a code change. **Raising it is a trust decision**: each increment
moves the trusted boundary one entry left, and setting it higher than the number
of proxies actually in front of the app hands the key straight to the client.

### 2.2 The value must parse as an IP

`app.auth_attempt.ip` is `INET`, so a non-IP value raises `DataError` at insert
and 500s the request. Validate with `ipaddress.ip_address()` and return `None`
when it fails. `None` means **the throttle does not run** for that request —
there is no key to count against. That is the correct failure: absent a
trustworthy address the alternatives are throttling everybody together (a
single global counter, trivially weaponised into a denial of service against
every user) or refusing the request (a self-inflicted outage the first time a
proxy header changes shape).

### 2.3 IPv6 is not folded to a /64, and that is measured

Keying on a bare IPv6 address is normally a hole, because a residential
allocation is a /64 and an attacker rotates addresses inside it for free.
`api.tvbingefriend.com` has an `A` record and **no `AAAA` record** (checked
2026-08-19), so no request to the API arrives over IPv6 and the hole is not
reachable. Recorded rather than fixed: the day an `AAAA` record is added, the
throttle key must fold IPv6 to its /64 before this table means anything, and
nothing in the code will say so.

## 3. `app.auth_attempt`

A new table, not an extension of `app.login_attempt`. That table is the
email-keyed lockout's, its rows are cleared per-email on a successful login
(§4.3 explains why the IP counter must **not** do that), and widening it to hold
signups would leave a table named for logins where half the rows are not.

```python
class AuthAttempt(Base):
    __tablename__ = "auth_attempt"
    __table_args__ = (
        Index("ix_auth_attempt_kind_ip_at", "kind", "ip", "attempted_at"),
        CheckConstraint("kind IN ('signup', 'login')", name="ck_auth_attempt_kind"),
        {"schema": "app"},
    )

    id: Mapped[int]            # BigInteger, autoincrement
    kind: Mapped[str]          # Text, NOT NULL
    ip: Mapped[str]            # INET, NOT NULL
    attempted_at: Mapped[datetime]   # timestamptz, server_default now()
```

- **No FK to `app.user`** — a signup attempt precedes the user, and a throttled
  attempt has no user at all.
- The index leads `(kind, ip, attempted_at)` because every read is
  "count rows of one kind for one address since a timestamp".
- `kind` is a checked vocabulary with the two constants living in the repo
  module. A third kind is a widening of `ck_auth_attempt_kind`, deliberately
  loud.
- Constraints are **named explicitly**, as every `app` table's are, because the
  test suite builds them with `create_all` and prod builds them with Alembic.

**Pruning is out of scope and the arithmetic is why.** Rows accrue at the rate
of failed logins plus signups — a handful a day at present, bounded above by the
throttle itself at `(5/hr + 10/15min) × 24 ≈ 1,080` rows per attacking address
per day. `app.login_attempt` has carried the same unbounded shape since it
shipped. A prune job belongs with the other scheduled tasks if this ever
matters, and it is not this ticket.

## 4. What each route does

### 4.1 Shared shape

Both routes resolve the IP once, then gate before doing any work:

```python
ip = client_ip(request, trusted_proxy_hops=settings.trusted_proxy_hops)
await auth_throttle.enforce(db, kind=..., ip=ip, throttle=settings.<kind>_ip_throttle)
```

`enforce` counts rows in the window and raises `TooManyAttempts` at or above the
maximum; the router maps that to **429** with `detail="rate_limited"` (the
string `email_change.py` and `email_verification.py` already use for their 429s)
and a `Retry-After` header holding the window in seconds. That is an honest
upper bound rather than a computed one — the exact answer needs the oldest row
in the window, and a second query to shave seconds off a rejection buys nothing.

A request rejected with 429 is **not itself recorded**. The window drains and a
throttled address recovers without intervention; recording rejections would let
a script hold itself banned indefinitely, which sounds appealing until the
script belongs to a NAT'd office.

`enforce` and `record` take the budget as **one frozen dataclass**, not loose
integers:

```python
@dataclass(frozen=True)
class IpThrottle:
    max_attempts: int
    window_minutes: int
```

Same reasoning as `rate_budget.Budget` (`CLAUDE.md`, *"The budget is one `Budget`
argument, not three loose ones"*) — a call site states the budget it means, and
the pair cannot drift apart across the four config values.

### 4.2 `POST /auth/signup`

In order:

1. Resolve the IP.
2. **Enforce** the signup throttle → 429.
3. **Record** the attempt, and `await db.commit()` immediately. The commit is
   load-bearing: `account_service.signup` calls `db.rollback()` on its
   `IntegrityError` path (`EmailInUse`), which would otherwise discard the
   attempt row and make a duplicate-email spray free.
4. **Verify the Turnstile token** (§5) → 400 or 503.
5. `account_service.signup(...)` as today → 201 / 403 / 409.

Every attempt that reaches step 3 counts, whatever happens after it. A bot
spraying invalid tokens burns its five per hour on rejections, which is the
point.

Turnstile is verified **after** the throttle, so a flood cannot make us spend an
outbound request per attempt.

### 4.3 `POST /auth/login`

1. Resolve the IP.
2. **Enforce** the login throttle → 429.
3. `account_service.authenticate(...)`, unchanged, including its email-keyed
   lockout.
4. On `InvalidCredentials`, **record** a `login` attempt, commit, then raise the
   401 exactly as today.

Only failures are counted. Credential stuffing is made of failures, so the
signal is intact, while a shared office or household address whose occupants all
log in successfully never accumulates a count.

**The IP counter is never cleared on a successful login**, unlike the
email-keyed one. An attacker owns at least one valid account — their own — so a
clear-on-success rule hands them a reset button between every ten guesses.

The 429 and the 401 leak different information, and that is a deliberate
departure from the lockout's own reasoning. `authenticate` returns
`InvalidCredentials` for a locked-out email precisely so an attacker cannot tell
guessing from lockout; an IP throttle cannot hide, because the whole point is
that NEU-1166 renders a distinct message ("too many attempts from this network",
AC 5). What leaks is a fact the attacker already knows — how many requests they
have sent.

### 4.4 Not in scope

`/auth/password`, `/auth/logout`, the password-reset and email-verification
issue routes. The last two already have per-user token throttles
(`auth_token_service.can_issue`, 1/min and 5/hr). AC 2 names signup and login,
and a per-IP gate on authenticated routes is a different question with a
different blast radius.

## 5. Turnstile verification

`src/tvbf/integrations/turnstile.py`, beside `integrations/linear.py`, which is
the existing precedent for an outbound call made inside a request.

```python
_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_TIMEOUT_SECONDS = 5.0
```

The URL is a module constant, never config — a base URL is a property of the
provider rather than of a deployment, which is the rule `llm/registry.py` states
and `config.py` repeats for `DEEPINFRA_BASE_URL`.

`POST` form-encoded `secret`, `response` (the token) and `remoteip` (the address
from §2, omitted when it is `None`). Cloudflare uses `remoteip` as a
corroborating signal, so the two halves of this ticket feed each other.

**Parse the response through a Pydantic model, not `payload.get("success")`.**
A body that decodes to a list — which is what an interception page or a
misrouted response looks like — makes `.get()` raise `AttributeError`, which is
neither of the errors below and escapes the handler as a 500. This is the same
failure `llm/client.py` reads its envelope through `api_payloads.ChatCompletion`
to avoid.

Three outcomes:

| Condition | Result |
|---|---|
| `success: true` | proceed |
| `success: false` (any `error-codes`) | **400** `captcha_invalid` |
| timeout, transport error, non-2xx, unparseable body | **503** `captcha_unavailable` |
| token absent or empty while enabled | **400** `captcha_required` |

`error-codes` is logged, never returned — it distinguishes an expired token from
a bad secret, and the second is our misconfiguration rather than the user's
problem.

### 5.1 Fail closed

An unverifiable token means no account. The availability cost is close to zero:
`app.tvbingefriend.com` is Cloudflare-proxied (`104.21.x` / `172.67.x`), so
during a Cloudflare incident the SPA that renders the signup form is unreachable
anyway. Failing open would open the exact hole this ticket closes, in the one
window an attacker would most like to have it, and would do so invisibly.

The 503 is logged at `warning` with `exc_info` so Sentry captures it.

### 5.2 ADR-0002 is not violated

ADR-0002 forbids an upstream API call in a live request path. It is about
**catalog data** — the thing it forbids is fetching on a cache miss what a
mirror should already hold, so that load scales with our traffic instead of our
catalog. A captcha verification mirrors nothing and cannot be precomputed: the
token is minted per attempt and is worthless a second time. `POST /me/feedback`
already calls Linear inline on the same reading. Stated here so the next reader
does not have to re-derive it.

## 6. Configuration

All in `config.py`, all with blank/default placeholders in `.env.example` **and
a line in `docker-compose.yml`** — Coolify seeds a prod variable from a compose
`${VAR:-default}` exactly once and then shadows the file, so a variable with no
compose line is one that has to be added by hand in prod later.

| Setting | Env | Default |
|---|---|---|
| `turnstile_enabled` | `TURNSTILE_ENABLED` | `False` |
| `turnstile_secret_key` | `TURNSTILE_SECRET_KEY` | `None` |
| `trusted_proxy_hops` | `TRUSTED_PROXY_HOPS` | `1` |
| `signup_ip_throttle_max` | `SIGNUP_IP_THROTTLE_MAX` | `5` |
| `signup_ip_throttle_window_minutes` | `SIGNUP_IP_THROTTLE_WINDOW_MINUTES` | `60` |
| `login_ip_throttle_max` | `LOGIN_IP_THROTTLE_MAX` | `10` |
| `login_ip_throttle_window_minutes` | `LOGIN_IP_THROTTLE_WINDOW_MINUTES` | `15` |

`Settings` exposes `signup_ip_throttle` / `login_ip_throttle` properties
returning the `IpThrottle` dataclass, so no route assembles the pair by hand —
the same shape as the existing `cors_allowed_origins` property.

**Defaults.** Five signups per hour per address is far above any household and
far below a useful bot run. Ten login failures per fifteen minutes is twice the
per-email threshold, so one forgetful person trips their own email lockout well
before they trip the network's.

### 6.1 The switch is explicit, and it is checked at boot

`TURNSTILE_ENABLED` is a boolean rather than an inference from the secret's
presence, and `create_app()` raises when it is true with no
`TURNSTILE_SECRET_KEY`. Two knobs that can disagree are worth the startup check
because the failure they prevent is the one this ticket exists to close:
protection silently absent in production. Deriving the switch from the secret —
the shape `TMDB_READ_ACCESS_TOKEN` and `DEEPINFRA_API_KEY` use — is right for
those, where an absent credential merely disables a job that will fail loudly at
its call site; here it would mean a secret dropped from the Coolify UI turns
signup protection off with nothing anywhere saying so.

Off is the default, so tests and localdev need no network and `task test` stays
offline. **Nothing in the repo turns it on** — prod sets it in the Coolify UI.

The **throttle has no switch and is always on**: it is local, needs no network,
and the test suite truncates `app.auth_attempt` between tests along with every
other table, so it is inert unless a single test deliberately exceeds a limit.

## 7. Request contract (what NEU-1166 codes against)

`SignupRequest` gains:

```python
turnstile_token: str | None = Field(default=None, max_length=2048)
```

**Optional in the schema, required by the handler when verification is enabled.**
Optional keeps the 18 existing `/auth/signup` test call sites and every direct
`account_service.signup` caller working unchanged; the handler is where "enabled
means required" is decided, which is the only place that knows. A missing token
with verification on is a 400 `captcha_required`, not a 422 — the SPA's
`api/client.ts` renders field-level 422s against form fields, and there is no
form field for this.

`LoginRequest` is unchanged.

Responses NEU-1166 must handle:

| Status | `detail` | Meaning |
|---|---|---|
| 400 | `captcha_required` | enabled, no token sent |
| 400 | `captcha_invalid` | Cloudflare rejected the token — offer a widget retry |
| 503 | `captcha_unavailable` | we could not reach Cloudflare — retry later |
| 429 | `rate_limited` | too many attempts from this address (`Retry-After` set) |

`detail` is a plain string in all four, so `api/client.ts`'s
string-`detail`-only reader surfaces it (the gap NEU-1196 covers for 422s does
not apply).

## 8. Acceptance criteria

1. `POST /auth/signup` with `turnstile_enabled=true` and a token Cloudflare
   rejects returns **400 `captcha_invalid`** and creates no user.
2. The same with no token returns **400 `captcha_required`**; with a token
   Cloudflare accepts, signup proceeds exactly as today.
3. Siteverify timing out or answering non-2xx returns **503
   `captcha_unavailable`** and creates no user.
4. With `turnstile_enabled=false` (the default), no outbound request is made and
   signup ignores the field entirely. The full suite runs with no network.
5. `create_app()` raises when `turnstile_enabled=true` and
   `turnstile_secret_key` is unset.
6. The 6th signup from one address inside an hour returns **429 `rate_limited`**
   with `Retry-After`; a signup from a different address in the same window
   succeeds.
7. The 11th failed login from one address inside 15 minutes returns **429**,
   *walking a different email every time* — the case today's email-keyed lockout
   cannot see.
8. A successful login does **not** clear that address's counter.
9. The email-keyed lockout still fires at 5 failures for one email, and still
   clears on success.
10. `client_ip` returns the right-most `X-Forwarded-For` entry at
    `hops=1`, ignores a forged left-most entry, falls back to
    `request.client.host` with no header, and returns `None` for a
    non-IP value.
11. A request whose IP resolves to `None` is not throttled and does not 500.
12. `task test`, `task lint`, `task typecheck` all green.

## 9. Departures from the ticket

**AC 4 asks for the Turnstile *site* key in `config.py`; this spec omits it.**
The site key is public and is consumed by exactly one thing — the widget in the
SPA — which NEU-1166 AC 2 reads from its own `env.ts`. A copy in backend config
would be read by nothing, ever, and would sit there ready to disagree with the
one the browser actually uses. The secret key is config here, as asked. If a
`GET /config`-style endpoint is ever wanted, adding the field back is one line.

## 10. Out of scope

- Pruning `app.auth_attempt` (§3) or `app.login_attempt`.
- Throttling any route other than signup and login (§4.4).
- Disposable-email-domain checks — named in NEU-1151's problem statement,
  ticketed nowhere, and not implied by any AC here.
- Making `invite_code` optional — that is NEU-1156, which this unblocks.
- Rendering the widget or the 429 (NEU-1166).

## 11. Notes for the implementation

- `docs/adr/` needs no new entry; the client-IP trust rule and the fail-closed
  choice belong in `CLAUDE.md`'s **Non-obvious patterns** section, which is where
  this repo keeps rules that are load-bearing and easy to undo.
- `app/errors.py` gains `TooManyAttempts(DomainError)`. The Turnstile outcomes
  are raised from `integrations/turnstile.py` as its own exception pair and
  mapped in the router — they are transport facts, not domain errors, and
  `account_service` must not grow an outbound-HTTP dependency (every direct-call
  test would then need to stub it).
- `respx` is already a dev dependency; use it for the siteverify tests rather
  than monkeypatching `httpx`.
- Adding a table means the `app` schema only — no change to the five
  hand-maintained schema lists, which are about *schemas*, not tables.

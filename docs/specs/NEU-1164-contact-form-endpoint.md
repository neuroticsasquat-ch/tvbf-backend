# NEU-1164 — Contact form endpoint

**Ticket:** [NEU-1164](https://linear.app/neuroticsasquatch/issue/NEU-1164/backend-contact-form-endpoint)
**Parent:** [NEU-1155](https://linear.app/neuroticsasquatch/issue/NEU-1155/legal-and-informational-pages) · **Project:** TVBF: Open Registration · Milestone 3, Launch switch
**Blocks:** [NEU-1170](https://linear.app/neuroticsasquatch/issue/NEU-1170/frontend-terms-privacy-about-and-contact-pages)
**Blocked by:** [NEU-1160](https://linear.app/neuroticsasquatch/issue/NEU-1160/backend-verify-turnstile-tokens-and-add-an-ip-keyed-auth-throttle) ✅ Done
**Repo:** `tvbf-backend` only. The frontend half is NEU-1170, which codes the `/contact` page that POSTs to this endpoint.
**Written:** 2026-08-21

`POST /contact` lets a stranger reach the maintainer without logging in — the
page's most likely user is someone who cannot. An unauthenticated endpoint that
sends email is a spam relay by default, so it carries the same two protections
NEU-1160 put on signup: a Turnstile token and an IP-keyed throttle.

---

## 1. Route

`POST /contact` — unauthenticated, no session, no CSRF.

### 1.1 Request

```python
class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr = Field(max_length=254)
    message: str = Field(min_length=1, max_length=5000)
    turnstile_token: str | None = Field(default=None, max_length=2048)
```

`turnstile_token` is optional in the schema (keeps callers that omit it from
getting a 422) and required by the handler when verification is enabled — the
same shape NEU-1160 §7 established for `SignupRequest`.

### 1.2 Check order

In order, each blocking on success:

1. Resolve the client IP via `client_ip(request, trusted_proxy_hops=settings.trusted_proxy_hops)`.
2. **Enforce** the contact throttle → 429 `rate_limited` + `Retry-After`.
3. **Record** the attempt and `await db.commit()` immediately. The commit is load-bearing: if
   Turnstile fails, the attempt still counts against the throttle — a bot spraying invalid tokens
   burns its five per hour here.
4. **Verify the Turnstile token** (§2) → 400 `captcha_required` / `captcha_invalid`, or 503
   `captcha_unavailable`.
5. Compose and send the email (§3) → 204. An `EmailSendError` is logged at warning and the caller
   still sees 204 — same rule as `feedback_service.submit_feedback` (§3.1).

### 1.3 Responses

| Status | `detail` | Meaning |
|---|---|---|
| 204 | (empty) | delivered |
| 400 | `captcha_required` | enabled, no token sent |
| 400 | `captcha_invalid` | Cloudflare rejected the token |
| 422 | (FastAPI) | field validation failure — `name`/`email`/`message` |
| 429 | `rate_limited` | too many attempts from this address (`Retry-After` set) |
| 503 | `captcha_unavailable` | siteverify unreachable |

`detail` is a plain string in all cases, so `api/client.ts`'s string-detail-only reader surfaces it
(the gap NEU-1196 covers for 422s does not apply).

---

## 2. Turnstile and throttle — reusing NEU-1160

Both mechanisms already exist.

### 2.1 Turnstile

Reuses the same `turnstile_enabled` switch and `turnstile_secret_key`. The switch is per-site
(Cloudflare Turnstile is per-domain), not per-endpoint. Having two switches would let one be on and
the other off, which is the silent gap the explicit switch exists to prevent.

`POST /contact` calls `integrations/turnstile.verify(token=..., secret=settings.turnstile_secret_key,
remoteip=ip)` — the same function and the same config.

Off by default (`turnstile_enabled=False`), so tests and localdev need no network.

### 2.2 IP throttle

A third `kind='contact'` in `app.auth_attempt`, alongside `'signup'` and `'login'`. Same mechanism:
`auth_throttle.enforce` counts rows in the window and raises `TooManyAttempts`; `auth_throttle.record`
inserts and commits.

This means:
- Widen `ck_auth_attempt_kind` to `IN ('signup', 'login', 'contact')`.
- Add `CONTACT = 'contact'` to `auth_attempt_repo.py`'s module-level constants.
- Add `CONTACT_IP_THROTTLE_MAX` (default 5) and `CONTACT_IP_THROTTLE_WINDOW_MINUTES` (default 60) to
  `config.py`.
- Add `contact_ip_throttle` property returning a `Throttle`.

**Budget: 5 per hour per address** — the same ceiling as signup. It is the unauthenticated
precedent, and a contact form needs no higher ceiling.

All of NEU-1160's rules carry forward unchanged: rejections are not recorded, the window drains, the
counter is never cleared, `ip=None` means no throttle.

---

## 3. Email

### 3.1 Transport

Reuses the existing `send_email` path — `SmtpEmailClient` in localdev, `ResendEmailClient` in prod.
Delivers to `FEEDBACK_NOTIFY_EMAIL` (the same key that means "the maintainer's mailbox," NEU-1162
§8.2).

An `EmailSendError` is logged at warning with `exc_info` and the caller still sees 204 — same rule
as `feedback_service.submit_feedback`. A 502 would tell the caller to retry, risking duplicates if
the first send actually went through but the ack was lost; better a duplicate than silence.

### 3.2 `Reply-To`

`send_email` gains an optional `reply_to: str | None = None` parameter, threaded through the full
chain:

- `EmailClient.send` abstract signature
- `SmtpEmailClient.send` — sets `msg["Reply-To"]`
- `ResendEmailClient.send` — adds `"reply_to"` to the JSON payload
- `email/factory.py:send_email` — passes it through

The contact form passes the caller's `email` field as `reply_to`, so Tom's reply goes to the person
who wrote.

### 3.3 Template

`render_contact_notification(name, email, message)` in `email/templates.py`, returning
`(subject, html, text)`:

- **Subject:** `[Contact] message from {name}`
- **Body:** name, email, the message text, and a UTC timestamp. `name` and `message` are
  html-escaped (user input); `email` is escaped in the HTML body.

Same hand-rolled style as the existing templates.

---

## 4. Test stub

`_stub_outbound_email`'s `modules` tuple gains the contact router module. The stub is autouse and
captures all outbound email during tests, so the test suite never hits Mailpit.

---

## 5. What this does not do

- **Rate-limit anything other than contact.** The throttle's `kind='contact'` is scoped to this
  endpoint alone.
- **Store the message.** It is emailed and then gone — no database table for contact submissions.
- **Require a verified email.** The most likely user is someone who hasn't signed up yet.
- **Add a `TURNSTILE_SITE_KEY` to config.** NEU-1160 §9 already declined to do that; the site key is
  public and lives in the SPA's `env.ts`.

---

## 6. Acceptance criteria

1. `POST /contact` with valid fields, Turnstile off (default), and no prior attempts returns **204**
   and delivers an email to `FEEDBACK_NOTIFY_EMAIL` with `Reply-To` set to the caller's email.
2. With `turnstile_enabled=true` and a token Cloudflare rejects, returns **400 `captcha_invalid`**
   and sends no email.
3. With `turnstile_enabled=true` and no token, returns **400 `captcha_required`**.
4. Siteverify timing out or answering non-2xx returns **503 `captcha_unavailable`**.
5. The 6th attempt from one address inside an hour returns **429 `rate_limited`** with
   `Retry-After`; an attempt from a different address succeeds.
6. A Turnstile-rejected attempt still counts against the throttle (the commit precedes verification).
7. An `EmailSendError` is logged and the caller still sees 204.
8. `task test`, `task lint`, `task typecheck` all green.

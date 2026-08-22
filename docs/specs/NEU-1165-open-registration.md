# Open registration — make `invite_code` optional

**Ticket:** [NEU-1165](https://linear.app/neuroticsasquatch/issue/NEU-1165/backend-make-invite-code-optional-in-signuprequest)
**Parent:** [NEU-1156](https://linear.app/neuroticsasquatch/issue/NEU-1156/make-the-invite-code-optional-at-signup) · **Milestone:** 3. Launch switch · **Project:** TVBF: Open Registration
**Frontend half:** [NEU-1171](https://linear.app/neuroticsasquatch/issue/NEU-1171/frontend-signup-without-a-required-invite-code) — blocked on this ticket.

## 1. Problem

`SignupRequest.invite_code` is `str = Field(min_length=1, max_length=128)` — mandatory. Every
account arrives through a hand-issued invite code. Making it optional is the change that opens
registration.

## 2. What happens at signup, with and without a code

### 2.1 Signup with a valid invite code

An invited user gets three things an open-signup user does not:

1. **Pre-verified** — `email_verified_at` stamped to `now()` at creation, so they skip NEU-1161's
   social gate.
2. **Auto-connected** — the inviter and invitee become accepted connections in the same
   transaction. The inviter sends the request and accepts it immediately, so the new user's first
   friend feed has at least one entry.
3. **Code consumed** — the invite row is marked consumed, exactly as today.

### 2.2 Signup without a code (open registration)

- `email_verified_at` stays `NULL` — must verify before social access.
- No auto-connect.
- Turnstile still applies (NEU-1160's gate is on the route, not the code path).
- Verification email still sent best-effort.

### 2.3 Signup with an invalid code

Never falls through to open signup. A supplied code that is unknown, consumed, or mismatched on
`email_hint` raises `403 invalid_invite`, identical to today's behaviour. A typo does not become
an unintended open registration.

### 2.4 `INVITE_REQUIRED` flag

A config flag can re-close registration without a deploy. When `INVITE_REQUIRED=true`:

- Signup with no code → `403 invalid_invite` (same error surface as a bad code).
- Signup with a valid code still works — invites are the emergency door, and the flag must never
  disable them.

Default `false`, so open registration is the steady state. Set `true` in the Coolify UI for an
abuse spike; reset `false` when it passes.

## 3. The `app.invite` table needs an issuer

`app.invite` has no `issued_by_user_id` column. To auto-connect, we need to know who issued the
invite.

### 3.1 Migration

```sql
ALTER TABLE app.invite ADD COLUMN issued_by_user_id UUID
  REFERENCES app."user"(id) ON DELETE SET NULL;
```

Nullable — invites created before this migration have no issuer to record. `ON DELETE SET NULL`
preserves the invite row if the issuing admin's account is deleted.

### 3.2 Who sets it

Both admin invite creation routes set it:

- `POST /admin/invites/email` (cookie-session, SPA) — already has `_admin: User`, passes
  `_admin.id` to `invite_service.create_invite()`.
- `POST /admin/invites` (bearer-token, CLI/scripts) — has no user, passes `None`.

### 3.3 `InviteOut` exposes it

```python
class InviteOut(BaseModel):
    # ... existing fields ...
    issued_by_user_id: UUID | None
```

Both invite-listing routes (`GET /admin/invites` and `GET /admin/invites/cookie`) return the new
field. The frontend may ignore it.

## 4. Auto-connect mechanics

When a valid invite is consumed during signup and `invite.issued_by_user_id` is not null:

```
inviter = invite.issued_by_user_id
invitee = new_user.id
```

Call `connection_service.send_request(db, requester_id=inviter, addressee_id=invitee)` then
`connection_service.accept(db, id=<returned-id>, accepting_user_id=inviter)` — all in the same
transaction as user creation and invite consumption. The inviter both sends and accepts their own
request, which is semantically "the inviter vouched for this person by inviting them" and uses
existing codepaths end to end.

If `issued_by_user_id` is `NULL` (invite predates the column), skip auto-connect — the invitee
gets pre-verified but no connection. This is a migration-era edge case; every invite created after
this ships has an issuer.

## 5. Implementation

### 5.1 `SignupRequest`

```python
class SignupRequest(BaseModel):
    # ... existing fields unchanged ...
    invite_code: str | None = Field(default=None, max_length=128)
```

### 5.2 `account_service.signup()`

Signature change: `invite_code: str` → `invite_code: str | None`.

Logic:

```
if invite_code is not None:
    validate, consume, pre-verify, auto-connect (if issuer known)
else:
    if settings.invite_required:
        raise InvalidInvite()
    # open signup: unverified, no auto-connect
```

Pre-verification: `user.email_verified_at = datetime.now(UTC)` set on the user object before
`db.commit()`, in the same transaction.

Auto-connect: after invite consumption, call `send_request` then `accept` with the inviter as
accepting party. If either raises, the transaction rolls back and the signup fails.

### 5.3 `Settings`

```python
invite_required: bool = Field(default=False, alias="INVITE_REQUIRED")
```

### 5.4 `invite_repo.create()`

```python
async def create(
    db: AsyncSession, *, code: str, email_hint: str | None,
    issued_by_user_id: UUID | None = None,
) -> Invite:
```

### 5.5 `invite_service.create_invite()`

```python
async def create_invite(
    db: AsyncSession, *, email_hint: str | None = None,
    issued_by_user_id: UUID | None = None,
) -> Invite:
```

### 5.6 Route changes

`POST /admin/invites/email` (cookie-session): pass `_admin.id` as `issued_by_user_id`.
`POST /admin/invites` (bearer-token): no change — already has no user, passes nothing.
`GET /admin/invites` and `GET /admin/invites/cookie`: add `issued_by_user_id` to `InviteOut`.

### 5.7 `Invite` model

```python
class Invite(Base):
    # ... existing columns unchanged ...
    issued_by_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.user.id", ondelete="SET NULL"),
        nullable=True,
    )
```

### 5.8 Tests

At minimum:

- Signup with no code succeeds (open registration).
- Signup with a valid code succeeds, sets `email_verified_at`, auto-connects inviter and invitee.
- Signup with an invalid code → 403 (unknown, consumed, email_hint mismatch).
- Signup with a consumed code → 403 (not silent fallthrough).
- `INVITE_REQUIRED=true` blocks signup without a code → 403.
- `INVITE_REQUIRED=true` still allows signup with a valid code.

## 6. Acceptance criteria

- [ ] `SignupRequest.invite_code` is `str | None = None`.
- [ ] Signup without a code creates an unverified, unconnected account.
- [ ] Signup with a valid code creates a pre-verified account, auto-connected to the inviter.
- [ ] Signup with an invalid/consumed/mismatched code → `403 invalid_invite`, never falls through.
- [ ] `INVITE_REQUIRED` env var: `true` blocks open signup, `false` (default) allows it.
- [ ] `INVITE_REQUIRED=true` still allows signup with a valid code.
- [ ] `app.invite` gains `issued_by_user_id UUID` (nullable, FK → `app.user`).
- [ ] `InviteOut` includes `issued_by_user_id`; both listing routes return it.
- [ ] `task test`, `task lint`, `task typecheck` green.

## 7. Not in scope

- Any change to the invite *listing* page in the SPA.
- Removing the invite mechanism — it stays as a growth lever.
- Changing the verification email flow for open-signup users.
- The frontend half (NEU-1171).

## 8. Notes for the frontend ticket

The `POST /signup` response shape is unchanged. An invited signup's `email_verified_at` will be
non-null immediately; an open signup's will be `null`. NEU-1171 should not need to handle this
differently — the auth response already carries the field and the SPA already renders verification
state from it.

The auto-connect means an invited signup's first `/me/connections` call returns at least one row.
NEU-1171's AC 4 ("an invited signup should say who invited them and that they are now connected")
can derive the inviter from the username on that connection row.

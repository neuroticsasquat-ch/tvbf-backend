# NEU-1194 — Stop a display name from being an email address

**Ticket:** [NEU-1194](https://linear.app/neuroticsasquatch/issue/NEU-1194/backend-stop-a-display-name-from-being-an-email-address)
**Project:** TVBF: Open Registration · Milestone 2, Identity
**Repo:** `tvbf-backend` only
**Written:** 2026-08-19

`app.user.display_name` is free text with no content rule beyond `min_length=1`,
so a user can set it to their own email address — and one production user has.
That address renders as an `h1` on their profile and in every connection list,
visible to anyone who connects with them.

This spec supersedes the ticket in one place, recorded in §7: its recommended
answer for the existing row does not work, because the ticket it defers to never
touches `display_name`.

---

## 1. What is wrong

Two write sites, and they disagree with each other about more than this rule:

| | `SignupRequest` (`app/schemas.py:47`) | `MeUpdateRequest` (`app/schemas.py:77`) |
|---|---|---|
| `max_length` | 100 | 80 |
| strips whitespace | no | yes (`mode="before"`) |
| rejects an address | no | no |

Confirmed user-entered rather than a fallback: `account_service.signup` passes
the value straight through, and no migration ever backfilled `display_name`
from `email`. `user_repo.create` is the only writer.

**The affected row count is 1 of 5 users**, observed 2026-08-19 against real
production data (the prod `app` schema landed locally during NEU-1195's work,
before anonymisation ran). This settles the ticket's DoD item 4 and its "if it
is more than a handful the answer changes" clause: it is one row.

Note for anyone re-taking that count: **NEU-1195 now rewrites `display_name`
during `task db:refresh`**, so it can no longer be measured from a local copy.
It needs a direct query against prod.

## 2. The rule

One regex, applied to the stripped value:

```python
_EMAIL_SHAPED = re.compile(r"[^\s@]@[^\s@]*\.[^\s@]")   # .search()
```

The `@` and the dot must sit inside one whitespace-free run, with a non-`@`
character on each side. The ticket asked for "contains `@` with a dot after
it"; taken literally that is `.+@.+\..+`, which rejects any display name
carrying an `@` and a later sentence dot:

| Value | `.+@.+\..+` | This rule |
|---|---|---|
| `jeanne_briggs@yahoo.com` | reject | **reject** |
| `@home with Tom` | accept | accept |
| `@home with Tom. Really` | **reject** | accept |
| `Tom O'Brien @ 3.5 stars` | **reject** | accept |
| `a@b.c` | reject | **reject** |

**The load-bearing character is the `[^\s@]` before the `@`**, which makes a
leading `@` never match. That matters more than it looks: NEU-1163 is about to
make `@handle` a first-class concept in this product, so users typing
`@tomboone` or `@home with Tom` into a display name goes from hypothetical to
likely, and a rule rejecting any string that starts with `@` would fight the
feature being built one ticket over in the same milestone.

Two values are **accepted** on purpose, and both are faithful to the ticket's
"the goal is to stop a user publishing an address, not to parse one":

- `tom@localhost` — no dot, so the rule lets it through. Not a routable address.
- `Tom @home.com` — the `@` has no local part in front of it. Also not an address.

## 3. Where the rule lives

One `Annotated` alias in `app/schemas.py`, used by both write sites — the house
pattern CLAUDE.md names for one rule over several fields (`OptionalDate` in
`tmdb/api_payloads.py` is the precedent):

```python
DisplayName = Annotated[str, BeforeValidator(_strip), AfterValidator(_reject_email_shaped)]
```

Each class keeps its own `Field(...)`, so `SignupRequest` stays at
`max_length=100` and `MeUpdateRequest` at `80`.

**The alias carries the strip as well as the rule.** Without it the two sites
disagree about the rule itself rather than merely about length: `" a@b.c "`
would be rejected at `PATCH /me` and accepted at signup, because `min_length`
and the email check both run against the raw string. Stripping inside the alias
means one input gets one verdict at both doors — and it fixes, for free, that
`"   "` is currently a valid signup display name.

**The differing lengths are deliberately left alone.** 100 vs 80 is a real
inconsistency, but it is a different defect, it has a live-data question behind
it (whether any prod row exceeds 80, no longer answerable locally for §1's
reason), and folding it in would mean this PR silently tightens signup by
twenty characters under a ticket about email addresses.

## 4. The existing row

**Rewrite it, in a data migration.**

The ticket recommends leaving it and letting NEU-1163's handle backfill "be the
moment they get a non-email identifier". That does not work. NEU-1163 adds
`handle` **beside** `display_name` and backfills it **from** `display_name`; it
never rewrites `display_name`. After it ships, that user has a handle derived
from their address *and still renders* `<h1>jeanne_briggs@yahoo.com</h1>`. The
address stays exactly as public as it is today. NEU-1194's own Problem section
records NEU-1190 making this same mistake — assuming NEU-1154 covered it — and
the recommendation repeats the shape one ticket further on.

Leaving it would also ship a validator whose stated purpose is false for the
only user currently tripping it.

**The derived value is the address's local part, verbatim** —
`jeanne_briggs@yahoo.com` becomes `jeanne_briggs`. It is the half of the value
that is not an address and a string the person literally typed. Title-casing or
turning underscores into spaces would be the migration guessing at a name they
did not choose.

**When the local part is empty or whitespace-only**, fall back to
`'User ' || substring(id::text, 1, 8)` — NEU-1195's shape. A value like
`@foo.bar` is storable today, since `display_name` was never validated as an
email, so this branch is reachable. The fallback is safe to reuse here even
though `display_name` has no unique constraint: a prefix collision costs
nothing at this grain.

**The migration's output must itself pass §2's rule**, or the change contradicts
itself on the one row it was written for. `jeanne_briggs` does — the rule keys
on `@`, not on dots, so a local part like `a.b` is fine.

## 5. How the refusal reaches the user

A Pydantic `ValueError` yields the usual 422. **The message will not be legible
to the user until NEU-1196 ships**, and that is accepted here rather than worked
around.

`api/client.ts:50` reads the error body's `detail` only when it is a *string*;
FastAPI returns a *list* for schema validation, so the SPA falls back to
`Request failed with status 422`. The raw body is on `ApiError.body` and nothing
in the SPA reads it (`grep -rn "err.body\|error.body" src/` returns nothing).

Two alternatives were rejected. Moving the rule to the routers to get a string
`detail` would put it in two places and contradict the ticket's own reasoning
that every other display-name rule lives in the schema. Flattening
`RequestValidationError` globally would change every endpoint's error contract
and discard the field location the SPA will need.

The gap is not this ticket's alone — **NEU-1163 AC 4 already requires "a
distinct error the SPA can render inline"** for a taken handle — so it is filed
once, as **NEU-1196**, rather than worked around in each backend ticket. The
interim cost is that a user who types their address into the field is told only
that something failed. Acceptable because the rule fires rarely and the value is
stopping a live address being published.

## 6. Acceptance criteria

- [ ] `POST /signup` with `display_name` set to an email address returns 422 and
      the account is not created.
- [ ] `PATCH /me` with the same value returns 422 and leaves the stored name
      unchanged.
- [ ] `@home with Tom` is accepted — and so are `@home with Tom. Really`,
      `Tom O'Brien @ 3.5 stars`, `tom@localhost` and `Tom @home.com`. Each has a
      test; the last two are documented accepts, not oversights.
- [ ] `a@b.c` and `jeanne_briggs@yahoo.com` are both rejected.
- [ ] `" a@b.c "` is rejected at **both** write sites, and `"   "` is rejected at
      both — the strip runs before the rule and before `min_length` everywhere.
- [ ] `SignupRequest` still accepts 100 characters and `MeUpdateRequest` still
      caps at 80; neither bound moves.
- [ ] A migration rewrites every existing email-shaped `display_name` to the
      address's local part, falling back to `'User ' || substring(id::text, 1, 8)`
      when that is empty, and every row it writes passes §2's rule.
- [ ] The migration is idempotent and does not fail on a `display_name` with no
      local part.
- [ ] The count of affected production rows and the decision to rewrite are
      recorded in a comment on NEU-1194 (the count is 1 of 5, measured
      2026-08-19 — see §1).
- [ ] `task test`, `task lint`, `task typecheck` green.

## 7. Where this supersedes the ticket

**The ticket's recommendation for existing rows is withdrawn** — §4. It defers
to a ticket that never touches the column, so the address would survive
indefinitely.

**The ticket's rule is tightened** — §2. "Contains `@` with a dot after it"
would reject `@home with Tom. Really`, and the ticket's own DoD requires the
`@home with Tom` class to pass.

**The ticket does not mention** that the two write sites disagree on
`max_length` and on stripping — §3 resolves the stripping half, because the rule
cannot be consistent without it, and deliberately leaves the length half alone.

**NEU-1196 is new** — §5. The ticket assumes a 422 is a legible refusal; it is
not, on this client.

## 8. Out of scope

- The handle itself — **NEU-1154** / **NEU-1163**.
- Display-name uniqueness, profanity, and impersonation rules.
- The local anonymiser — **NEU-1195**, shipped 2026-08-19.
- Reconciling `max_length` 100 vs 80 — §3.
- Rendering field-level validation errors in the SPA — **NEU-1196**.
- Applying this rule to `handle`; NEU-1163 defines its own character set, which
  excludes `@` and `.` by construction.

---

**References:** NEU-1190 spec, `docs/specs/NEU-1190-listing-surface-cleanup.md`
§7 (item 8 splits finding 12 item 5 into NEU-1194 and NEU-1195).

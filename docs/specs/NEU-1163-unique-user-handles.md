# NEU-1163 — `app.user.handle`: uniqueness, backfill, and payload exposure

**Ticket:** [NEU-1163](https://linear.app/neuroticsasquatch/issue/NEU-1163/backend-add-appuserhandle-with-uniqueness-backfill-and-payload)
**Parent:** [NEU-1154](https://linear.app/neuroticsasquatch/issue/NEU-1154/unique-user-handles) · **Project:** TVBF: Open Registration · Milestone 2, Identity
**Repo:** `tvbf-backend` only. The frontend half is [NEU-1169](https://linear.app/neuroticsasquatch/issue/NEU-1169/frontend-surface-handles-in-signup-settings-search-and-profiles), which this unblocks.
**Written:** 2026-08-20

`app.user.display_name` has no uniqueness constraint and `UserSearchResult`
returns `{id, display_name}` and nothing else, so two users named "Tom" are
indistinguishable at the moment someone decides whether to accept a connection
request. Impersonating a real friend costs nothing: pick their name, wait to be
added.

This adds `handle` as the stable public identifier beside the free-text
`display_name`, backfills the accounts that already exist, and puts it on every
payload that names a user.

This spec supersedes the ticket in five places, collected in §12.

---

## 1. What a handle is

A handle is **the identifier**; a display name is **the label**. The handle is
unique, stable, and lowercase; the display name is free text and stays exactly
as permissive as NEU-1194 left it. Neither replaces the other, and neither is a
credential — a handle is printed on every card, which is what makes §9's search
rule safe and what makes §6.3's refusal deliberately uninformative.

**Shape:** `^[a-z][a-z0-9_]{2,29}$` — 3 to 30 characters, lowercase ASCII
letters, digits and underscores, starting with a letter.

The 3-character floor is not arbitrary. Registration is about to open, and the
one- and two-character space is the first thing a stranger takes; 30 is the
ceiling at which a handle still fits beside a display name in a card caption at
a 375px viewport, which is the width NEU-1187 and NEU-1183 measured every other
control against. Starting with a letter keeps `@99` and `@_x` out and gives the
derivation in §5 a rule it can enforce by trimming rather than by failing.

### 1.1 Case is normalised on input, and `CITEXT` is not load-bearing

The ticket contradicts itself here: AC 1 justifies `CITEXT` on the grounds that
"`@TomBoone` and `@tomboone` cannot both exist", which is only meaningful if
mixed case is *storable*, while AC 2 restricts the charset to `[a-z0-9_]`, which
means it never is.

**Resolved in favour of AC 2, with normalisation rather than refusal.** A
`BeforeValidator` strips surrounding whitespace, strips a leading `@`, and
lowercases. `TomBoone`, `@TomBoone` and `  @tomboone ` all become `tomboone`;
none of them is refused. A user who types their own name the way they capitalise
it, or pastes a handle with the sigil they saw it printed with, gets the account
they meant instead of a form error about a rule they had no way to know.

The column is `CITEXT NOT NULL UNIQUE` anyway, for parity with `email` and as a
guard against a future writer that reaches the table without passing the
validator. It is **belt-and-braces and nothing rests on it**: every value this
application stores is already lowercase, so removing the case-insensitivity
would change no behaviour. Say so in the model comment, or the next reader will
assume the normalisation is redundant and delete the wrong one of the two.

The strip is inside the alias for the reason NEU-1194 §3 gives about
`DisplayName`: two write sites (`POST /signup` and `PATCH /me/handle`) must
reach one verdict on one input, and a rule applied to the raw string at one door
and the stripped string at the other is two rules.

### 1.2 The `user_<8 hex>` shape is not hand-claimable

`^user_[0-9a-f]{8}$` is refused by the validator, by pattern rather than by
appearing in the blocklist.

It is the shape §5 gives an account whose display name yields nothing usable,
**and** the shape §10 gives every non-admin account during anonymisation. Left
claimable, a stranger could take `@user_3f4a2b1c` and wear an identifier a real
account either holds or recently held — the same identity inheritance §4 exists
to prevent, arriving through the derivation rather than through a release.

By pattern and not by list, because the list is a fixed set of strings and this
is a shape with 4.3 billion members.

---

## 2. Where the rule lives

`src/tvbf/app/schemas.py` gains a `Handle` alias, on `DisplayName`'s precedent
one screen up and `OptionalDate`'s one layer down — one `Annotated` alias over
one normaliser and one validator, shared by both write sites:

```python
Handle = Annotated[str, BeforeValidator(_normalise_handle), AfterValidator(_validate_handle)]
```

`_validate_handle` enforces §1's regex, §1.2's pattern refusal, and membership
of the blocklist. All three are **schema rules**, so all three produce a 422
carrying `loc: ["body", "handle"]`, which NEU-1196 (shipped 2026-08-19) now
renders against the field rather than as `Request failed with status 422`. The
gap NEU-1194 §5 accepted is closed; this ticket inherits a working client.

**Uniqueness is not one of them.** It needs a session, a Pydantic validator has
none, and the answer changes between validation and commit anyway. It lives in
the service layer and answers differently — §6.3.

---

## 3. The reserved list

### 3.1 What it holds

A long defensive list, in three groups:

* **Impersonation** — `admin`, `administrator`, `support`, `help`, `staff`,
  `moderator`, `official`, `root`, `system`, `security`, `abuse`, plus the
  product's own names: `tvbf`, `tvbingefriend`, `bingefriend`.
* **Operational** — `api`, `www`, `static`, `assets`, `docs`, `redoc`,
  `healthz`, `readyz`, `mail`, `smtp`, and the rest of the conventional set.
* **Routes** — every top-level path segment the SPA serves today, recorded as a
  snapshot: `login`, `signup`, `verify_email`, `email_change`,
  `forgot_password`, `reset_password`, `upcoming`, `discover`, `my_shows`,
  `friends`, `connections`, `settings`, `admin`, `users`, `search`, `shows`,
  `episodes`, `people`, `watch_next`, `watched`, `all`, plus the pages
  NEU-1155 is going to add: `about`, `terms`, `privacy`, `contact`, and `me`.

**Why route names at all**, when profiles are `/users/:uuid` and nothing in the
SPA routes on a handle: because NEU-1154's stated reason for choosing handles
over the smaller alternative is that they give future public-profile work a URL
to hang on, and that URL may or may not carry a sigil. Reserving costs nothing
now and keeps `/{handle}` open as an option. The asymmetry is the whole
argument — a word can be un-reserved later by deleting a line, and a handle can
never be taken back from someone already holding it.

The route group is a **snapshot of the SPA as of this revision**. The backend
cannot import `tvbf-frontend/src/router.tsx`, so nothing tracks it and nothing
should pretend to; a route added later is not automatically reserved, and that
is a known and accepted hole rather than a bug.

### 3.2 Where it lives, and why it exists twice

`src/tvbf/app/handles.py` owns `RESERVED_HANDLES: frozenset[str]`, vendored from
a public reserved-username list, filtered to entries matching
`^[a-z][a-z0-9_]{2,29}$` (anything outside the charset can never be claimed
anyway, so carrying it would be noise). **The module docstring records the
source URL, its licence, the retrieval date and the resulting count** — the
count is whatever the filter yields and is not predicted here.

The migration in §5 **restates the same strings as a SQL array literal**, under
a comment saying it is a snapshot as of that revision and not a copy that tracks
the module. This is `b7d3e02c9a41`'s precedent, followed deliberately:

* No migration in this repo imports application code — 0 of 52, checked — and
  the reason is that a migration must keep meaning what it meant on the day it
  ran, while `handles.py` is free to change.
* The pass has already run everywhere it will ever run. If the list is later
  extended, the migration is not re-run and must not be edited to chase it.

The cost is honest: several hundred strings written twice, the second copy
frozen forever. A `app.reserved_handle` table would dissolve the duplication,
and was rejected because it forces the check out of the Pydantic alias — which
is sync and has no session — and so out of the 422-with-a-field-`loc` shape that
NEU-1194 deliberately chose and NEU-1196 just built the client half for.

---

## 4. Two tables

### 4.1 `app.user.handle`

```
handle  CITEXT  NOT NULL  UNIQUE      -- uq_user_handle
```

Named explicitly in `app/models.py`, because `app` tables are built by
`create_all` in the suite and by Alembic in prod and the two must agree.

### 4.2 `app.handle_release`

```sql
CREATE TABLE app.handle_release (
  handle       CITEXT      PRIMARY KEY,
  user_id      UUID        NULL REFERENCES app."user"(id) ON DELETE SET NULL,
  released_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_handle_release_user_id_released_at
  ON app.handle_release (user_id, released_at DESC);
```

**A released handle is never claimable by anyone else, and the original owner
may always reclaim it.** The ticket asks for this decision to be made and
written down; it is made in favour of never reusing, with one exemption.

Releasing a handle otherwise lets the next person inherit whoever held it.
Today that is a label-only risk — profiles are `/users/:uuid` — but the moment
NEU-1154's future profile URL exists, the inherited thing is a link people have
already shared with each other. The counter-argument, that this is
over-engineering at five accounts, is answered by what it costs: one small
table, one predicate, and one insert on a route already limited to three calls
per user per month. It is empty on day one.

**The same-owner exemption** is what keeps the rule from being a trap. Changing
your mind and taking your old handle back is free; only strangers are refused.
That is what `user_id` is for.

**`user_id` is nullable with `ON DELETE SET NULL`, and `DELETE /me` inserts the
current handle before the account goes.** Without this, deleting an account
would drop its release rows *and* its live handle, and account deletion — which
is self-service behind a password — would become a supported route to handing
your identity to a stranger. A null owner means the handle is blocked and
reclaimable by nobody, which is the correct end state for an identity its owner
disclaimed.

The index serves §6.2's throttle count, which is the only query with a `user_id`
predicate. `handle` is the primary key because the claim path asks about a
handle, not about a user.

---

## 5. The backfill

One Alembic revision, three steps in order: add the column nullable, backfill
it, then add `NOT NULL` and `UNIQUE`. A fourth step asserts the result.

### 5.1 Derivation

```sql
-- 1. word boundaries become underscores, then unaccent and lowercase
-- 2. anything outside the charset is dropped
-- 3. leading non-letters are trimmed, then the value is capped and tidied
stem := trim(both '_' from
          left(
            regexp_replace(
              regexp_replace(
                immutable_unaccent(lower(
                  regexp_replace(display_name, '[[:punct:][:space:]]+', '_', 'g')
                )),
                '[^a-z0-9_]', '', 'g'),
              '^[^a-z]+', ''),
            30))
```

| `display_name` | `stem` |
|---|---|
| `Tom Boone` | `tom_boone` |
| `jeanne_briggs` | `jeanne_briggs` |
| `Renée O'Hara` | `renee_o_hara` |
| `Łukasz` | `lukasz` |
| `99 Problems` | `problems` |
| `Jo` | `jo` → too short, §5.2 |
| `Тимофей` | `""` → §5.2 |
| `🎬` | `""` → §5.2 |

**This is not `sql_fold.folded()`, and that is deliberate.** `folded()` strips
every punctuation character including `_`, so it cannot preserve word
boundaries — `Tom Boone` would become `@tomboone` and NEU-1194's freshly
rewritten production row `jeanne_briggs` would become `@jeannebriggs`. The
expression above collapses those runs to `_` instead.

CLAUDE.md's rule is that there is exactly one fold and it runs in Postgres. That
rule is about **comparison**: `folded()` exists so that two strings compared
across a seam agree, and a Python-side `unicodedata` copy would disagree on
precisely the titles the fold exists for. **Nothing here is compared.** This is
a one-way derivation whose output is a new value, and there is no second string
on the other side of an equals sign. What the ticket's note actually demands —
that this run in Postgres rather than through `unicodedata`, because
`unicodedata` does not decompose ł, ø, đ or ħ — is honoured: the expression uses
`immutable_unaccent`, which is the part Python cannot reproduce, and `Łukasz`
becomes `lukasz` rather than `ukasz`.

Say this in the migration docstring. A reader who knows the one-fold rule and
not this paragraph will "fix" it into `folded()` and silently change what every
existing account is called.

The `[^a-z0-9_]` strip after unaccenting is what removes the scripts `folded()`
deliberately passes through — Cyrillic, CJK, emoji — which is exactly why the
fold alone was never sufficient.

### 5.2 The tail

Applied in this order, so that no input can fail:

```
rn := row_number() OVER (PARTITION BY stem ORDER BY created_at, id)

CASE
  WHEN length(stem) < 3       THEN 'user_' || substring(id::text, 1, 8)
  WHEN stem = ANY(:reserved)  THEN 'user_' || substring(id::text, 1, 8)
  WHEN rn = 1                 THEN stem
  ELSE left(stem, 30 - length(rn::text)) || rn
END
```

**Reserved words fall back rather than taking a suffix.** A numeric suffix would
turn `Admin` into `@admin2`, which is precisely the impersonation the blocklist
exists to stop — a suffix does not weaken a name, it decorates it.

**A genuine collision leaves the oldest account holding the bare stem**, ordered
by `(created_at, id)` — the same ordering `user_repo.list_ids` already states,
for the same reason: an ordering that is written down is one a test can assert
and a re-run can reproduce. `left(stem, 30 - length(rn::text))` is what keeps
the suffixed value inside the ceiling.

**`user_<8 hex>` is the one fallback for three different failures** (empty, too
short, reserved) and is NEU-1195's shape, so a `User 3f4a2b1c` /
`user-3f4a2b1c@anon.local` / `@user_3f4a2b1c` triple reads as one account. It
takes the same eight characters those two take, so a prefix collision surfaces
in one place. Unlike NEU-1195's use of it, **there is a unique constraint here**,
so a collision aborts the migration rather than passing quietly — which is the
right failure and the reason §5.3 exists.

### 5.3 The migration asserts its own result

Before returning, the migration verifies that **every** row's handle matches
`^[a-z][a-z0-9_]{2,29}$`, is not `^user_[0-9a-f]{8}$`-shaped for an account
whose stem was usable, and is distinct — raising if not.

NEU-1195 established why: `ON_ERROR_STOP` catches a statement that raised, not a
`CASE` a later edit breaks into matching everyone. Here the stakes are higher
than a bad local copy. A derivation that silently produces an invalid handle
leaves an account whose own validator would refuse its identifier, discoverable
only when that user next opens settings — and the `UNIQUE` constraint would fail
the *deploy* rather than the migration, which is the worse place to find out.

### 5.4 Hand-verify against production before it ships

The ticket's own note, kept as an acceptance criterion. Five accounts exist and
this is a one-way door for all of them: `downgrade` drops the column, and the
derived value is not recoverable once someone has started using it.

Run the derivation as a `SELECT` against prod, read the five values, and record
them on the ticket before the migration is applied.

---

## 6. Claiming and changing

### 6.1 Signup

`SignupRequest` gains `handle: Handle`, **required**. AC 4. Unaffected by
NEU-1165 making `invite_code` optional later — these are different fields with
different reasons.

### 6.2 `PATCH /me/handle`

A dedicated route rather than a field on `PATCH /me`. `get_current_user` +
`require_csrf`, returning `AuthedUserOut`.

Its own route because it has its own error vocabulary, its own throttle, and
because `PATCH /me` is the display-name route whose contract NEU-1194 settled
three weeks ago; widening `MeUpdateRequest` to a partial update in order to
carry a throttled field beside an unthrottled one is how a display-name save
ends up refused by a 429 about a handle the user did not touch.

**The throttle's ledger is `app.handle_release` itself** — NEU-1162's shape,
adopted for its stated reason: the row is written anyway, so there is no
`auth_attempt`-style side table and no "record the attempt" step to forget.

```
count(*) FROM app.handle_release
 WHERE user_id = :me AND released_at > now() - :window
```

**3 changes per 30 days**, as `HANDLE_CHANGE_THROTTLE_MAX` (3) and
`HANDLE_CHANGE_THROTTLE_WINDOW_MINUTES` (43200) on the existing frozen
`Throttle` dataclass in `config.py`. Over the cap is `429 rate_limited` with
`Retry-After`, matching every other throttle in this codebase. Three rather than
one because a new user fixing a typo, then fixing their mind, should not be
locked out for a month; thirty days rather than a day because the ticket's own
sentence — "a handle that changes hourly defeats the purpose" — is about
identity stability, not about load.

The un-locked count-then-insert race is inherited from `auth_throttle.enforce`
and accepted for the reason stated there.

**Changing writes the release row for the old handle inside the same
transaction as the update.** A commit that moved the handle without recording
the release would free it silently, which is the one way §4.2's rule can be
broken from inside.

### 6.3 Refusals

| Cause | Response |
|---|---|
| Charset, length, leading character, reserved word, `user_<8hex>` shape | `422` with `loc: ["body", "handle"]` |
| Held by a live account | `409 handle_unavailable` |
| Released by a different account | `409 handle_unavailable` |
| Over the change throttle (`PATCH /me/handle` only) | `429 rate_limited` + `Retry-After` |

**`409` matches `email_in_use`**, which is the same class of thing on the same
form, so NEU-1169 maps both conflicts to their field in one place rather than
learning two shapes. Synthesising a Pydantic-shaped 422 from the service layer
was rejected: it would have the router fake a validation error for something
Pydantic never checked, and diverge from the conflict already sitting beside it.

**Taken and previously-released are one code, deliberately.** Distinguishing
them is more helpful — the second is permanent and no amount of waiting fixes
it — and it turns an unauthenticated-adjacent surface into a *has this handle
ever existed* oracle, including for accounts since deleted. The refusal is
uninformative on purpose; NEU-1169 renders one message.

### 6.4 `IntegrityError` is no longer unambiguous

`account_service.signup` currently wraps `user_repo.create` in
`except IntegrityError: raise EmailInUse()`. That was correct while `app.user`
carried exactly one unique constraint. With `uq_user_handle` beside
`uq_user_email`, **a duplicate handle would report `email_in_use`** — a refusal
naming the wrong field, on the one form where both fields are being submitted
together.

The handler dispatches on the constraint name and raises `EmailInUse` or
`HandleUnavailable` accordingly.

A pre-check (`user_repo.get_by_handle` plus the release lookup) runs first,
because it is the only thing that can distinguish "held" from "released by
someone else" and because it produces the refusal without a rollback. The
`IntegrityError` branch stays as the race fallback — the pre-check is not
locked, and two signups claiming one handle in the same instant must both get a
correct answer.

---

## 7. Exposure

Every payload that names a user carries the handle. One sentence, kept true:

| Payload | Reached by |
|---|---|
| `UserBrief` | connection requests (both parties), connections list, blocks list, friend show/episode activity, feed actor |
| `UserSearchResult` | `GET /users/search` |
| `UserOut` / `AuthedUserOut` | `GET /me`, `POST /signup`, `POST /login`, `PATCH /me`, `PATCH /me/handle` |
| `FriendRatingItem` | `GET /shows/{id}/friends/ratings`, `GET /episodes/{id}/friends/ratings` |
| `AdminUserOut` | `GET /admin/users` |
| `AdminReportUserRef` | `GET /admin/reports` |

**This costs nothing.** Every construction site already holds a full `User` row
— `get_many_by_ids`, `users_by_id`, `requester`/`addressee`, `actor_user` — so
no site gains a query. Checked, not assumed.

The last three are not named by AC 6 and are included anyway. `AdminReportUserRef`
is the strongest case: NEU-1197 deliberately withheld `email` there, leaving
`display_name` as a moderator's only label, and a report queue is exactly where
"two users named Tom" is most expensive to get wrong. `AdminUserOut` and
`FriendRatingItem` follow because a payload that names a user and omits the
identifier is the ambiguity this ticket exists to close, wherever it appears.

`GET /me/export` carries it too — it is account data, and the export is this
app's portability answer.

`UserSearchResult` **keeps `display_name`**. Returning the handle alone would be
the strongest anti-impersonation stance available and it throws away the name
people actually recognise; both fields, side by side, is what makes the
disambiguation legible.

---

## 8. Search

`user_repo.search` gains a handle clause beside the display-name one:

```sql
WHERE (display_name ILIKE '%'||:q||'%'
       OR handle     ILIKE '%'||:q||'%'
       OR email = :q)
  AND email_verified_at IS NOT NULL      -- NEU-1161
  AND disabled_at IS NULL                -- NEU-1162
ORDER BY (handle = :q) DESC, display_name
LIMIT 20
```

**A leading `@` is stripped from the query** before it is used. Someone handed
`@tom_b` will paste it exactly as they were given it.

**Substring, not exact.** Email is exact-only because an address is
enumeration-sensitive; a handle is the opposite — it is printed on every card
that names its owner. Matching part of one reveals nothing the display-name
substring clause does not already reveal, and refusing to would protect a value
that is already public.

**An exact handle match sorts first**, which is the point at the moment of
decision: you were given `@tom_b` precisely because three people are called Tom,
and a result list that buries the exact match alphabetically has answered the
wrong question.

No index. At five accounts a sequential scan is the plan Postgres would pick
anyway; if it ever matters, the answer is a trigram index on `handle` matching
the `ix_*_folded_trgm` pattern already used in `catalog`, and it is a
measurement rather than a guess.

Both new predicates live in the repo layer beside the two already there, for
NEU-1161 §3.2's reason: `limit` must still return a full page.

---

## 9. Anonymisation

`scripts/refresh_db.sh` rewrites `handle` alongside `email`, `display_name` and
`password_hash`, **unconditionally** for every non-`ADMIN_EMAIL` row:

```sql
UPDATE app."user"
   SET handle = 'user_' || substring(id::text, 1, 8)
 WHERE email <> :admin_email;
TRUNCATE app.handle_release;
```

and then asserts its own result — every non-admin handle matches
`^user_[0-9a-f]{8}$` — for NEU-1195's reason, that `ON_ERROR_STOP` catches a
statement that raised and not a `CASE` a later edit breaks.

`@jeanne_briggs` is as identifying as the name it was derived from, and a
conditional rule here would be a second copy of NEU-1194's email-shaped test
with the same failure mode. It takes the same eight id characters the other two
rewrites take, so the triple stays visibly one account and a prefix collision
surfaces once — here at `uq_user_handle`, which unlike the other two will
actually raise.

`app.handle_release` is truncated rather than rewritten: it holds handles
derived from real names, its rows are worthless in a local copy, and mapping
them would just be a third derivation to keep consistent.

**Shipped in this PR rather than filed as a follow-up.** Any `db:refresh` run
between the column landing and the anonymisation landing copies real handles
onto a laptop, and that window is avoidable by not opening it.

---

## 10. Acceptance criteria

- [ ] `app.user.handle` is `CITEXT NOT NULL UNIQUE`, named `uq_user_handle` in
      both the model and the migration.
- [ ] `app.handle_release` exists with a nullable `user_id`,
      `ON DELETE SET NULL`, and the `(user_id, released_at DESC)` index.
- [ ] `TomBoone`, `@TomBoone` and `  @tomboone ` all yield the stored value
      `tomboone`; none is refused.
- [ ] `ab`, `a_very_long_handle_of_thirty_one`, `9lives`, `_tom`, `tom-boone`
      and `admin` are each refused with a 422 carrying `loc: ["body","handle"]`,
      at **both** write sites.
- [ ] `user_3f4a2b1c` is refused by pattern, and `user_notahex` is accepted —
      the refusal is the shape, not the prefix.
- [ ] `RESERVED_HANDLES` records its source URL, licence, retrieval date and
      entry count in the module docstring, and every entry matches
      `^[a-z][a-z0-9_]{2,29}$`.
- [ ] A test asserts the migration's inlined array and `RESERVED_HANDLES` agreed
      **at the revision** — a snapshot test that pins the list as it shipped, not
      one that fails when the module is later extended.
- [ ] The migration backfills every existing row, and `Tom Boone`,
      `jeanne_briggs`, `Renée O'Hara`, `Łukasz`, `99 Problems`, `Jo`, `Тимофей`
      and `🎬` each produce the value in §5.1/§5.2's tables. Each has a test.
- [ ] Two accounts whose display names derive to one stem yield the bare stem
      for the older (`created_at, id`) and a suffixed value for the newer, with
      the suffixed value inside 30 characters.
- [ ] A display name deriving to a reserved word yields `user_<8 hex>`, not
      `<word>2`.
- [ ] The migration asserts its own result and raises rather than completing
      when a derived value is invalid or non-distinct.
- [ ] The migration is idempotent and does not fail on any display name storable
      today, including one that derives to the empty string.
- [ ] `POST /signup` with a handle a live account holds returns
      `409 handle_unavailable`, and no account is created.
- [ ] `POST /signup` with a handle a *different* account released returns the
      same `409 handle_unavailable` — byte-identical, so the two causes are
      indistinguishable.
- [ ] A duplicate **email** at signup still returns `409 email_in_use` while a
      duplicate **handle** returns `409 handle_unavailable`; the
      `IntegrityError` branch dispatches on the constraint name and is covered by
      a test that reaches it (pre-check bypassed or raced).
- [ ] `PATCH /me/handle` changes the handle, writes the old one to
      `app.handle_release` in the same transaction, and returns `AuthedUserOut`
      carrying the new value.
- [ ] The original owner can reclaim a handle they released; another account
      cannot, permanently.
- [ ] `DELETE /me` inserts the account's current handle into
      `app.handle_release`, and the row survives the user row's deletion with a
      null `user_id`. The handle is then claimable by nobody.
- [ ] A fourth change inside 30 days returns `429 rate_limited` with
      `Retry-After`; the third succeeds.
- [ ] `UserBrief`, `UserSearchResult`, `UserOut`, `AuthedUserOut`,
      `FriendRatingItem`, `AdminUserOut` and `AdminReportUserRef` all carry
      `handle`, and `GET /me/export` includes it.
- [ ] `GET /shows` and `GET /users/search` issue the same number of queries as
      before — the exposure adds none.
- [ ] `GET /users/search?q=@tom_b` finds `@tom_b`, an exact handle match sorts
      first, and `q=boone` still finds display name `Tom Boone`. Email remains
      exact-match-only.
- [ ] `scripts/refresh_db.sh` rewrites every non-admin `handle` to
      `user_<8 hex>`, truncates `app.handle_release`, and asserts the result.
- [ ] The derived handles for the five production accounts are read from a
      `SELECT` against prod and recorded on NEU-1163 **before** the migration is
      applied there.
- [ ] `.claude/docs/architecture-database.md`, `.claude/docs/patterns-auth-and-abuse.md`,
      `.claude/docs/architecture-endpoints.md` and CLAUDE.md's endpoint index are
      updated.
- [ ] `task format`, `task test`, `task lint`, `task typecheck` green.

---

## 11. Where this supersedes the ticket

**AC 1 and AC 2 contradict each other and AC 2 wins** — §1.1. `CITEXT` stays,
but as parity with `email` rather than as the thing preventing `@TomBoone`;
normalisation prevents it, and the column could be `TEXT` without any behaviour
changing.

**"`PATCH /me` (or a dedicated route)" is resolved to the dedicated route** —
§6.2, and the throttle is the release ledger rather than a new counter.

**The handle-history question is answered: never reusable by anyone else, with
a same-owner exemption and an ownerless row on deletion** — §4.2. The ticket
asks for a decision and flags over-engineering; the cost is one table and one
predicate, and the deletion path is a hole the simple version leaves open.

**AC 3's "numeric suffix on collision" does not apply to reserved words** —
§5.2. `@admin2` is the thing being prevented, decorated.

**AC 6's list is widened by three payloads** — §7. `AdminReportUserRef` in
particular, because NEU-1197 left `display_name` as a moderator's only label.

**AC 4's "distinct error the SPA can render inline" is no longer blocked** —
NEU-1196 shipped on 2026-08-19, so the 422 shape NEU-1194 §5 accepted as
illegible now renders against its field. This ticket inherits a working client
and needs no interim workaround.

**Two things the ticket does not mention.** `account_service.signup`'s blanket
`except IntegrityError → EmailInUse` becomes wrong the moment a second unique
constraint exists (§6.4), and `scripts/refresh_db.sh` will copy real handles onto
developer machines unless it is taught about the column in the same PR (§9).

---

## 12. Out of scope

* **Everything in the SPA.** NEU-1169 owns signup collection, the settings form,
  live availability checking, and rendering the handle beside display names.
* **A public profile URL.** No `/@handle` or `/{handle}` route is added here; §3
  reserves the words that keep the option open and nothing more.
* **A handle-availability endpoint.** NEU-1169 calls live checking optional and
  notes it would enumerate handles; if it is wanted, it is its own ticket with
  its own throttle.
* **The `max_length` 100-vs-80 split** between `SignupRequest.display_name` and
  `MeUpdateRequest.display_name`. Still a real inconsistency, still a different
  defect, deliberately untouched — NEU-1194 §3.
* **A one-time prompt inviting backfilled users to change their handle.**
  NEU-1169's note; a frontend concern, and the backend route it needs is §6.2.
* **Trigram or any other index on `handle`.** §8 — a measurement, not a guess.

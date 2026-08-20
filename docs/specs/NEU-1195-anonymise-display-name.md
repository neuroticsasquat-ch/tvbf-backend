# NEU-1195 — Anonymise `display_name` in `refresh_db.sh`

**Ticket:** [NEU-1195](https://linear.app/neuroticsasquatch/issue/NEU-1195/anonymise-display-name-in-refresh-dbsh)
**Project:** TVBF: Open Registration
**Repo:** `tvbf-backend` only
**Written:** 2026-08-19

`scripts/refresh_db.sh` anonymises `app."user".email` and `password_hash` and
nothing else, so real, live email addresses reach every local database `task
db:refresh` produces — in `display_name`, where at least one production user
put their own address, and in `auth_token.payload`, where the email-change flow
stores a pending one.

This spec supersedes the ticket in two places, both recorded in §2 and §6: the
ticket's claim that the base table is the *only* remaining hole is false, and
its acceptance criteria 1 and 2 contradict each other.

---

## 1. What is wrong

`scripts/refresh_db.sh:156–160` is the whole anonymiser:

```sql
UPDATE app."user" SET
  email = CASE
    WHEN nullif(:'admin_email', '') IS NOT NULL AND email = :'admin_email' THEN email
    ELSE 'user-' || substring(id::text, 1, 8) || '@anon.local'
  END,
  password_hash = :'anon_hash';
```

`display_name` is not mentioned; `grep -n display_name scripts/refresh_db.sh`
returns nothing. `app.user.display_name` is free text with no content rule
beyond `min_length=1` (NEU-1194 is the fix going forward), and it renders as an
`h1` on the profile page and in every connection list. Observed on a local
refresh as `<h1>jeanne_briggs@yahoo.com</h1>` — NEU-1190's Milestone 0 audit,
finding 12 item 5.

## 2. The base table is not the only hole

The ticket asserts that the two `TRUNCATE`d tables cover everything else, "so
the base table is the remaining hole rather than a general oversight". That is
wrong. `app.auth_token.payload` is `JSONB`, and `email_change_service.py:76`
writes:

```python
payload={"new_email": new_email},
```

`auth_token` is not in the `TRUNCATE` list, so a pending email change carries a
second real address through every refresh — the same defect, in the same
statement's blast radius, two lines below the `UPDATE` this ticket edits.
**NEU-1195 absorbs it** rather than filing a sibling: it is one table name added
to a list that already exists, it needs no design of its own, and a ticket that
documented the hole should not ship without closing it.

`app` was swept for the rest of the class and this is the complete set:

| Table | Holds | Status |
|---|---|---|
| `user.email`, `password_hash` | address, credential | already rewritten |
| `user.display_name` | address, or a real name | **this ticket** |
| `auth_token.payload` | `{"new_email": …}` | **this ticket** |
| `session` | `ip`, `user_agent` | already truncated |
| `login_attempt` | `email`, `ip` | already truncated |
| `invite` | `email_hint` | already truncated |
| `watch_archive` | denormalised email + name | already truncated |
| `user_recommendation_set` | `compiled_payload`, `raw_response` | already truncated |
| `activity_event.payload` | `{"stars": …}` only | no PII |
| `connection` | user ids only | no PII |

## 3. What to build

### 3.1 Rewrite `display_name` in the same statement

Extend the existing `UPDATE` with a second `CASE` on the same shape as `email`
and the same `ADMIN_EMAIL` carve-out:

```sql
  display_name = CASE
    WHEN nullif(:'admin_email', '') IS NOT NULL AND email = :'admin_email' THEN display_name
    ELSE 'User ' || substring(id::text, 1, 8)
  END,
```

**Unconditionally**, never gated on the value looking like an email address. A
conditional rule would be a second copy of NEU-1194's email-shaped test living
in shell SQL, and it would still leave real people's real names in local
databases — the same class of data, and the reason the anonymiser exists.

**Rejected:** nulling or emptying the column. It is `NOT NULL`, and it is
rendered as an `h1`, so a local refresh would produce blank headings across the
friends surfaces and every list that names a user.

### 3.2 The derived form is coupled to the email rewrite, on purpose

`'User ' || substring(id::text, 1, 8)` takes **the same eight characters** the
email rewrite takes. Two things follow, and the second is the non-obvious one.

The two columns *agree*: `User 3f4a2b1c` is visibly the same account as
`user-3f4a2b1c@anon.local`, which is what an operator wants when they are
staring at a local friends list working out who is who.

And the uniqueness has exactly one home. Eight hex characters of a v4 UUID are
not unique by construction — they degrade as a birthday problem — but
`app."user".email` carries a unique index and `display_name` does not, so a
prefix collision is not two events. It is one event, and it surfaces at
`uq_user_email`. `display_name`'s collision-freedom is therefore precisely as
guaranteed as `email`'s already is, and is enforced by a constraint rather than
asserted by hope. **Changing the email derivation alone silently removes
`display_name`'s only collision check** — that is what the coupling costs, and
it belongs in the comment block.

**Rejected:** a form unique by construction, `'User ' || replace(id::text,'-','')`.
It guarantees the property directly, and it puts
`User 3f4a2b1c9e0d4f2ab7c6d5e4f3a2b1c0` in an `h1`.

### 3.3 `auth_token` joins the `TRUNCATE`

```sql
TRUNCATE app.session, app.login_attempt, app.invite, app.auth_token,
         app.watch_archive, app.user_recommendation_set CASCADE;
```

Truncated rather than rewritten, for the reason the two tables above it are:
the tokens are a production artifact, worthless outside their own expiry
window, and every session is truncated in the same statement anyway, so nothing
local is holding a token it could still redeem. Nothing references
`auth_token`, so the `CASCADE` already present does not widen.

### 3.4 A failed anonymisation must not report success

The anonymising `psql` at line 153 is the **only** call in the script without
`-v ON_ERROR_STOP=1` — compare lines 126 and 137. psql exits 0 on a statement
error unless that flag is set, so `set -euo pipefail` never fires, and a run
whose `UPDATE` raised still prints:

```
  ✓ Admin user preserved: log in as … / 'localdev'.
→ Applying any newer migrations from dev branch...
✓ Refresh complete (mode=app).
```

…over a database where every production email address is still present. The
success line is printed by the shell unconditionally, not by psql. The
realistic trigger is the collision of §3.2 violating `uq_user_email`.

**Add `-v ON_ERROR_STOP=1` to that invocation.** One flag, consistent with the
two calls above it, and it is what turns §3.2's uniqueness argument from a
statement about probability into a checked property.

### 3.5 The script asserts what it just did

`ON_ERROR_STOP` catches a statement that *raised*. It cannot catch a `CASE` that
a later edit breaks into matching everyone, because wrong data is not an error.
After the anonymising statement, assert the result:

```bash
ANON_LEFT=$(docker exec -i "$LOCAL_PG_CONTAINER" \
  psql -v ON_ERROR_STOP=1 -U "$LOCAL_DB_USER" -d "$LOCAL_DB" -tA \
  -v "admin_email=$ADMIN_EMAIL_VAL" <<'SQL'
SELECT count(*) FROM app."user"
 WHERE display_name !~ '^User [0-9a-f]{8}$'
   AND (nullif(:'admin_email', '') IS NULL OR email <> :'admin_email');
SQL
)
if [[ "$ANON_LEFT" != "0" ]]; then
  echo "ERROR: anonymisation left $ANON_LEFT display_name value(s) unrewritten." >&2
  exit 1
fi
```

Three things about its shape are deliberate.

**It asserts the positive, not the absence of an email.** Counting survivors
matching `'%@%.%'` only tests for the one shape we happened to fear, says
nothing about real names, and — see §6 — collides with the admin carve-out.
Matching the derived form is a direct statement of what the `UPDATE` was for.

**It runs as its own invocation, capturing into a shell variable.** A `DO $$ …
$$` block raising an exception would be tighter, but psql does not interpolate
`:'admin_email'` inside dollar-quoted strings, so the admin carve-out could not
be expressed there. Capture-then-branch is also the shape the script already
uses for `FK_RESTORE_SQL`.

**`[0-9a-f]` is lower-case only**, which is safe because `uuid::text` is
lower-case in Postgres and `display_name` is `Text`, not `CITEXT`.

### 3.6 Nothing between the restore and the anonymiser may exit on its own

Found by running §5's gate on 2026-08-19, and it is a worse instance of the same
defect than the one this ticket was filed for.

`pg_restore` exits non-zero for errors it has *already ignored* — most often a
cross-schema foreign key it cannot re-add because the local `catalog` is behind
prod's. It restores every row regardless. Under `set -euo pipefail` that
non-zero exit tore the script down **after** the data landed and **before** the
anonymiser ran, so the local database was left holding production PII while the
run reported a failure that read like nothing had happened. Measured on the
first real run: five real users, one display name that is an email address,
five `auth_token` rows and 9,359 `watch_archive` rows, all sitting locally.

That is strictly worse than the `display_name` gap: this ticket's whole premise
is that anonymisation is guaranteed, and a restore hiccup was skipping it
entirely.

Both steps between the restore and the anonymiser therefore **record a flag
instead of exiting** — `pg_restore` itself, and the cross-schema FK re-add below
it, which had the identical `exit 1` one step over. They land on a single
consolidated gate placed **after** the anonymiser and **before** `task migrate`:
a schema that did not restore cleanly is not one to migrate on top of, and the
exit code is the only thing between a half-restored database and a developer who
believes it is whole. The run still ends non-zero — it just makes the data safe
first, because **a partial restore holds exactly the same PII a whole one does.**

The gate's message branches on whether anonymisation actually ran, so it cannot
claim the database is safe under `ANONYMIZE=0` or in `catalog` mode.

**Rejected:** passing `--exit-on-error` to `pg_restore`, or treating its
non-zero exit as fatal earlier. The FK failures here are expected on any machine
whose catalog is stale, which is the normal state of a developer laptop — making
them fatal would mean the anonymiser never runs on exactly the machines that
most need it.

### 3.7 The comment block

The existing block explains why `watch_archive` and `user_recommendation_set`
are truncated rather than rewritten. Extend it in the same voice to say why
`display_name` is rewritten **unconditionally** (§3.1), why the eight-character
prefix is shared with the email rewrite and what that coupling protects (§3.2),
and why `auth_token` is truncated (§3.3).

## 4. Acceptance criteria

- [ ] After `task db:refresh app` (or `both`) with anonymisation on, every
      `app."user"` row except the `ADMIN_EMAIL` account has a `display_name`
      matching `^User [0-9a-f]{8}$`, and its first eight characters are the same
      eight in that row's rewritten `email`.
- [ ] The account matching `ADMIN_EMAIL` keeps both its email and its display
      name. If no `ADMIN_EMAIL` is set, every row is rewritten.
- [ ] `SELECT count(*) FROM app.auth_token` returns 0 after the same run.
- [ ] The anonymising `psql` carries `-v ON_ERROR_STOP=1`, and a deliberately
      broken statement in that heredoc aborts the script instead of reaching
      `→ Applying any newer migrations`.
- [ ] The assertion of §3.5 exits 1 with a message naming the count when a row
      is left unrewritten.
- [ ] The comment block covers all three of §3.7's points.
- [ ] A `pg_restore` that reports errors still reaches the anonymiser, and the
      run then exits non-zero at the consolidated gate without running
      `task migrate`. Verified against the real failure, not simulated.
- [ ] The gate's message does not claim the database is anonymised when
      `ANONYMIZE=0` or `MODE=catalog`.
- [ ] **The merge is gated on a real refresh having been run** — see §5.

## 5. Verification is a real run, not CI

Nothing here can be checked by CI. There is no test harness for `scripts/`; the
pre-commit gates are ruff, pyright and pytest over `src` and `tests`, and this
script needs `PROD_SSH` and a live prod dump to execute at all.

So the ticket closes only after an actual `task db:refresh app` (or `both`)
against prod completes with the §3.5 assertion passing. The assertion is what
makes that cheap: the run either completes or aborts, so "I ran it" *is* the
test. Code review cannot substitute, because the interesting failures — a
`CASE` that matches every row, an unexpected `auth_token` FK — only appear
against real rows. This is the standing merged-≠-run rule; a one-file shell
change whose entire value is what happens on a real refresh is exactly the
shape that rule exists for.

## 6. Where this supersedes the ticket

**The ticket's DoD items 1 and 2 cannot both hold.** Item 1 requires
`SELECT count(*) FROM app."user" WHERE display_name LIKE '%@%.%'` to return 0;
item 2 preserves the admin row's display name. If the operator's own display
name is email-shaped, the count returns 1 and the criteria contradict. §4
replaces both with the positive form, which is silent about what the old value
looked like and carves out the admin row explicitly.

**The ticket's Problem section is wrong that the base table is the only
remaining hole** — §2 — and NEU-1195 absorbs `auth_token` rather than leaving
it. The ticket title stops being literally complete as a result.

`-v ON_ERROR_STOP=1`, the §3.5 assertion and §3.6's deferred-failure gate are
all additions the ticket does not ask for. The third was found by running the
merge gate and is a worse instance of the ticket's own defect — anonymisation
being skipped outright rather than covering one column too few. Both were taken deliberately: the ticket asserts a post-condition the
script has no way of guaranteeing, and these two are the difference between a
guarantee and a hope.

## 7. Out of scope

- Validating `display_name` at the write sites — **NEU-1194**.
- Production `watch_archive` retention — **NEU-1158**.
- Anything about `ANONYMIZE=0`, which is opt-out by design.
- The `psql` at line 95 that captures `FK_RESTORE_SQL` also lacks
  `ON_ERROR_STOP`, so a failure there yields an empty variable and the restore
  proceeds without re-adding cross-schema foreign keys. Noted because it was
  found while auditing the same class of silent failure; it is a different bug
  with a different consequence and is not fixed here.
- Rewriting rather than truncating `auth_token` — the tokens have no local
  value once `session` is truncated in the same statement.

---

**References:** NEU-1190 spec, `docs/specs/NEU-1190-listing-surface-cleanup.md` §7
(item 8 splits finding 12 item 5 into NEU-1194 and NEU-1195).

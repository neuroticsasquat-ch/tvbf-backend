"""Rewrite every email-shaped app.user.display_name to its local part

NEU-1194. `display_name` was free text with no rule beyond `min_length=1`, so a
user could set it to their own address — and one production row of five does,
rendering as an `h1` on their profile and in every connection list. The
validator shipping alongside this migration closes the door; this closes the
row already behind it.

**Rewritten rather than left for NEU-1163's handle backfill**, which is what the
ticket originally recommended. That backfill adds `handle` *beside*
`display_name` and derives it *from* `display_name`; it never rewrites the
column, so the address would have stayed exactly as public as it is today, under
a validator whose stated purpose was false for the only user tripping it.

**The derived value is the address's local part, verbatim** —
`jeanne_briggs@yahoo.com` becomes `jeanne_briggs`. It is the half of the value
that is not an address and a string the person literally typed; title-casing it
or turning underscores into spaces would be this migration guessing at a name
they did not choose. When the local part is empty or whitespace-only — reachable,
because a value like `@ x@y.com` was storable — it falls back to
`'User ' || substring(id::text, 1, 8)`, NEU-1195's shape. A prefix collision
costs nothing at this grain, since `display_name` carries no unique constraint.

The predicate is the validator's regex verbatim (`app/schemas.py:_EMAIL_SHAPED`).
Two properties follow and both are load-bearing: **every value written passes
that same rule**, because `split_part(…, '@', 1)` can contain no `@` at all and
neither can the fallback — a migration whose output its own validator would
reject would contradict itself on the one row it was written for; and the pass is
**idempotent**, because a rewritten row no longer matches the predicate.

Irreversible: the address it overwrote is not recoverable from the column, and
`downgrade` deliberately does not invent one.

Revision ID: b7d3e02c9a41
Revises: c9f4a1b73e26
Create Date: 2026-08-19 17:30:00.000000+00:00

"""

from alembic import op

revision = "b7d3e02c9a41"
down_revision = "c9f4a1b73e26"
branch_labels = None
depends_on = None

# A snapshot of `app/schemas.py:_EMAIL_SHAPED` **as of this revision**, not a
# copy that tracks it: an `@` and a later dot inside one whitespace-free run,
# with a non-`@` character on each side. If the validator later loosens, this
# stays as it is — the pass has already run everywhere it will ever run, and
# editing a shipped migration to chase it would rewrite history for nothing.
_EMAIL_SHAPED = r"[^\s@]@[^\s@]*\.[^\s@]"


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE app."user"
           SET display_name = CASE
                 WHEN btrim(split_part(display_name, '@', 1)) = ''
                   THEN 'User ' || substring(id::text, 1, 8)
                 ELSE btrim(split_part(display_name, '@', 1))
               END
         WHERE display_name ~ '{_EMAIL_SHAPED}'
        """
    )


def downgrade() -> None:
    # One-way. The overwritten address is gone from the column, and inventing a
    # value here would be worse than leaving the rewritten name standing.
    pass

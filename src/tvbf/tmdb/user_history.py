"""The `app` write sites a catalog-grain retirement has to move, in one place (NEU-1146).

Two migration passes move user history off a catalog row and onto the row that
supersedes it. `episode_repoint` (NEU-1126) did it for copies that paired on the
exact `(show, season, episode)` key; `orphan_retire` (NEU-1146) does it for
everything that key could not reach. NEU-1146's spec is explicit that the second
one must **extract** this machinery rather than author a second copy of it —
these statements encode which uniqueness constraint each write site carries, and
a divergent copy of that knowledge is how one of them starts raising in
production at three in the morning.

## What is shared, and what deliberately is not

Shared: the three episode-grain UPDATEs, the three show-grain UPDATEs, and the
`EXISTS` predicate that decides whether a catalog row is still referenced. Each
UPDATE carries a `NOT EXISTS` guard mirroring its table's uniqueness constraint
exactly — `user_episode_watch`'s primary key `(user_id, episode_id)`,
`uq_user_episode_rating` on the same pair, `uq_activity_event` on
`(actor_id, verb, target_type, target_id, season_number)`, and the show-grain
equivalents. Those guards are what keep a collision from taking a batch down.

**Not shared: what happens to a row that cannot move.** That is policy, and the
two passes answer it oppositely on purpose. `episode_repoint` *keeps* the copy and the
user row on it (`blocked_by_collision`), refusing at `(episode, user)` grain so
one person's history never splits across two rows. `orphan_retire` *deletes* the
redundant row, because the surviving twin already records that viewing and
NEU-1146 §4.2 reverses the earlier call. So the withholding predicate is
injected by the caller through `extra`, not baked in here.

`extra` is a callable rather than a string because the owner column differs per
statement — `w.user_id`, `r.user_id`, `a.actor_id` — and a single formatted
string cannot name all three.

## Why `IS NOT DISTINCT FROM` on the season number

`uq_activity_event` is declared `NULLS NOT DISTINCT`, so two episode events with
a NULL season number *do* conflict. A plain `=` would decide they do not, the
`NOT EXISTS` would pass, and the UPDATE would raise on the constraint it was
meant to dodge.
"""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import TextClause, text

# The no-op injection: a caller with no withholding policy of its own.
_ALWAYS: Callable[[str], str] = lambda _owner: "TRUE"  # noqa: E731

# Whether any user row still points at a `catalog.episode`. Both the guard on the
# statement that deletes such a row and the honest half of either pass's report,
# which is why it is one string rather than two.
EPISODE_STILL_REFERENCED = """
    EXISTS (SELECT 1 FROM app.user_episode_watch w WHERE w.episode_id = e.id)
 OR EXISTS (SELECT 1 FROM app.user_episode_rating r WHERE r.episode_id = e.id)
 OR EXISTS (SELECT 1 FROM app.activity_event a
             WHERE a.target_type = 'episode' AND a.target_id = e.id)
"""

# The show-grain twin. `user_show_watch` and `user_show_rating` cascade from
# `catalog.show`; `activity_event` does not, and is the site that orphans
# silently rather than failing loudly.
SHOW_STILL_REFERENCED = """
    EXISTS (SELECT 1 FROM app.user_show_watch w WHERE w.show_id = s.id)
 OR EXISTS (SELECT 1 FROM app.user_show_rating r WHERE r.show_id = s.id)
 OR EXISTS (SELECT 1 FROM app.activity_event a
             WHERE a.target_type = 'show' AND a.target_id = s.id)
"""

# Both arrays are positionally paired, so one `unnest` yields the whole batch as
# rows — two bind parameters whatever the batch size, which is what keeps
# Postgres's 32,767-parameter cap out of the picture.
_UNNEST = """
    unnest(cast(:doomed AS bigint[]), cast(:survivors AS bigint[]))
      AS m(doomed_id, survivor_id)
"""

_EPISODE_WATCH = f"""
    UPDATE app.user_episode_watch w
       SET episode_id = m.survivor_id
      FROM {_UNNEST}
     WHERE w.episode_id = m.doomed_id
       AND NOT EXISTS (
             SELECT 1 FROM app.user_episode_watch x
              WHERE x.user_id = w.user_id AND x.episode_id = m.survivor_id
           )
       AND {{extra}}
 RETURNING w.user_id, w.episode_id
"""

_EPISODE_RATING = f"""
    UPDATE app.user_episode_rating r
       SET episode_id = m.survivor_id
      FROM {_UNNEST}
     WHERE r.episode_id = m.doomed_id
       AND NOT EXISTS (
             SELECT 1 FROM app.user_episode_rating x
              WHERE x.user_id = r.user_id AND x.episode_id = m.survivor_id
           )
       AND {{extra}}
 RETURNING r.user_id, r.episode_id
"""

_EPISODE_ACTIVITY = f"""
    UPDATE app.activity_event a
       SET target_id = m.survivor_id
      FROM {_UNNEST}
     WHERE a.target_type = 'episode'
       AND a.target_id = m.doomed_id
       AND NOT EXISTS (
             SELECT 1 FROM app.activity_event x
              WHERE x.actor_id = a.actor_id
                AND x.verb = a.verb
                AND x.target_type = 'episode'
                AND x.target_id = m.survivor_id
                AND x.season_number IS NOT DISTINCT FROM a.season_number
           )
       AND {{extra}}
"""

_SHOW_WATCH = f"""
    UPDATE app.user_show_watch w
       SET show_id = m.survivor_id
      FROM {_UNNEST}
     WHERE w.show_id = m.doomed_id
       AND NOT EXISTS (
             SELECT 1 FROM app.user_show_watch x
              WHERE x.user_id = w.user_id AND x.show_id = m.survivor_id
           )
       AND {{extra}}
"""

_SHOW_RATING = f"""
    UPDATE app.user_show_rating r
       SET show_id = m.survivor_id
      FROM {_UNNEST}
     WHERE r.show_id = m.doomed_id
       AND NOT EXISTS (
             SELECT 1 FROM app.user_show_rating x
              WHERE x.user_id = r.user_id AND x.show_id = m.survivor_id
           )
       AND {{extra}}
"""

_SHOW_ACTIVITY = f"""
    UPDATE app.activity_event a
       SET target_id = m.survivor_id
      FROM {_UNNEST}
     WHERE a.target_type = 'show'
       AND a.target_id = m.doomed_id
       AND NOT EXISTS (
             SELECT 1 FROM app.activity_event x
              WHERE x.actor_id = a.actor_id
                AND x.verb = a.verb
                AND x.target_type = 'show'
                AND x.target_id = m.survivor_id
                AND x.season_number IS NOT DISTINCT FROM a.season_number
           )
       AND {{extra}}
"""


@dataclass(frozen=True)
class RepointStatements:
    """One grain's three write sites, built against a caller's withholding policy."""

    watch: TextClause
    rating: TextClause
    activity: TextClause


def episode_statements(extra: Callable[[str], str] = _ALWAYS) -> RepointStatements:
    """The three episode-grain UPDATEs, each taking `:doomed` / `:survivors` arrays.

    `extra` receives the owning user column for each statement and returns an
    additional SQL predicate — `episode_repoint` uses it to withhold a whole
    person's rows when any one of them would collide. The default withholds
    nothing.

    The watch and rating statements `RETURNING` the pairs they moved, which is
    how `orphan_retire` learns exactly whose history landed in a show they do
    not track (NEU-1146 §4.3) rather than inferring it.
    """
    return RepointStatements(
        watch=text(_EPISODE_WATCH.format(extra=extra("w.user_id"))),
        rating=text(_EPISODE_RATING.format(extra=extra("r.user_id"))),
        activity=text(_EPISODE_ACTIVITY.format(extra=extra("a.actor_id"))),
    )


def show_statements(extra: Callable[[str], str] = _ALWAYS) -> RepointStatements:
    """The three show-grain UPDATEs, each taking `:doomed` / `:survivors` arrays."""
    return RepointStatements(
        watch=text(_SHOW_WATCH.format(extra=extra("w.user_id"))),
        rating=text(_SHOW_RATING.format(extra=extra("r.user_id"))),
        activity=text(_SHOW_ACTIVITY.format(extra=extra("a.actor_id"))),
    )

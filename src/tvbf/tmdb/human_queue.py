"""The human matching queue: every user-touched show without a verified mapping (NEU-1044).

This is the mechanism that makes "no user data loss, even if it requires human
matching" real. NEU-1043 maps what it can prove; whatever it could not prove and
a user has actually touched ends up here, with enough context on each row for a
person to decide, and three ways to record what they decided.

**The query is the durable part; the interface is thin.** The residue is six rows
in production, so an admin UI would be a week's work for a page nobody opens
twice. `tvbf.jobs.human_queue` is an argparse CLI over the functions below, and
everything that would still be true behind a UI lives here.

## What "unverified" means

A row is in the queue when a user has touched it **and** its mapping is not
something anyone has vouched for:

* `tmdb_id IS NULL` — nothing matched it, or it lost a collision; or
* `match_method = 'title_year'` — tier 3 guessed it, under a three-way guard
  that is still a guess.

`tvdb_id` / `imdb_id` matches are exact and never surface. Neither does a row the
TMDB ingest inserted directly (`tmdb_id` set, `match_method IS NULL`), which knew
its own id and had nothing to match. Neither does `human`, which is the point of
the value: a verdict is what removes a row from the queue, and the queue is
empty exactly when every user-touched show has one.

## Three resolutions, two of them terminal in the database

1. **Confirm** a TMDB series → `tmdb_id` set, `match_method = 'human'`.
2. **Reject** every candidate → `tmdb_id IS NULL`, `match_method = 'human'`. The
   row stays locally-authored (ADR-0008) and every watch record on it keeps
   working — nothing in `app` references `tmdb_id`.
3. Add the show to TMDB upstream and then confirm it. Optional, and it never
   blocks a user's history: (2) is always available and always sufficient.

Rejecting *writes* rather than leaving the row untouched. An untouched row is
indistinguishable from an unreviewed one, so the queue would never empty and the
cutover gate would have nothing to read.

## The report names users by email, and is never committed

"Which users track it" is the context that decides how hard to look at a row, and
an opaque uuid does not supply it. That makes the report the opposite of
`reconciliation-baseline.json`, which holds ids precisely because it lives in
git: this one is produced live, read once, and thrown away. Do not add it to
`docs/migration/`.

## Run this before the full TMDB ingest

The ingest inserts a row per series, conflict-targeting `tmdb_id`. Once it has
run, the series a queue row should map onto already has a catalog row holding
that id, and `confirm` can only report the collision — merging two rows and
repointing `app`'s foreign keys is not something a six-row queue should be doing.
Same ordering constraint, and for the same reason, as `tmdb/enrichment.py`.
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tmdb.client import TMDBClient
from tvbf.tmdb.enrichment import MATCH_HUMAN, MATCH_TITLE_YEAR

log = logging.getLogger(__name__)

# How many `/search/tv` results to carry per row. The reviewer needs enough to
# recognise the show or conclude TMDB does not have it; a full page of 20 is a
# wall of text on a report meant to be read in a terminal.
CANDIDATE_LIMIT = 5

# The methods a queue verdict may overwrite. The two exact tiers are missing on
# purpose: `/find` is an upstream assertion rather than an inference, so the
# queue never surfaces such a row, and a command that never surfaces a row should
# not be able to silently retract one either.
RESOLVABLE_METHODS = (None, MATCH_TITLE_YEAR, MATCH_HUMAN)

# Episode-grain user rows reach a show through `tvmaze.episode`, which is where
# `app.user_episode_watch.episode_id` points for the whole window this queue
# exists in: it gates NEU-1046, which is the ticket that repoints those foreign
# keys. The join stays correct past that point anyway, because the copy preserved
# episode ids — but this is scaffolding, not something to generalise a spine
# parameter for.
_EPISODE = "tvmaze.episode"

# Every way a user can have touched a show, unioned. `activity_event` is
# polymorphic with no foreign key, so its show is resolved per target type
# exactly as the reconciliation harness does it — and it is included for the
# same reason: this query's job is that nothing slips through, not that the
# common cases are covered.
_TOUCHED = f"""
    SELECT show_id FROM app.user_show_watch
    UNION
    SELECT show_id FROM app.user_show_rating
    UNION
    SELECT e.show_id FROM app.user_episode_watch w JOIN {_EPISODE} e ON e.id = w.episode_id
    UNION
    SELECT e.show_id FROM app.user_episode_rating r JOIN {_EPISODE} e ON e.id = r.episode_id
    UNION
    SELECT CASE
               WHEN a.target_type = 'show' THEN a.target_id
               WHEN a.target_type = 'episode' THEN e.show_id
           END
      FROM app.activity_event a
      LEFT JOIN {_EPISODE} e ON a.target_type = 'episode' AND e.id = a.target_id
"""

# A user-touched show with no `catalog.show` row at all is invisible to the queue
# below, which reads *from* `catalog.show` — and it reads as an empty queue,
# which is the one wrong answer this thing must not give. It happens for real:
# the TV Maze daily keeps adding shows right up to cutover, and every one added
# after `task copy:catalog` ran is trackable by a user while having nothing to
# map. The fix is operational (re-run the copy), so this is reported rather than
# repaired here — but it is reported loudly, because the alternative is a queue
# that says "empty" while a user's show has no mapping at all.
_UNMIRRORED = text(f"""
    WITH touched AS ({_TOUCHED})
    SELECT DISTINCT t.show_id
      FROM touched t
     WHERE t.show_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM catalog.show s WHERE s.id = t.show_id)
     ORDER BY t.show_id
""")

_QUEUE = text(f"""
    WITH touched AS ({_TOUCHED})
    SELECT s.id,
           s.name,
           s.first_air_date,
           s.original_language,
           s.status,
           s.tmdb_id,
           s.match_method,
           s.tvdb_id,
           s.imdb_id,
           (SELECT count(*)
              FROM app.user_episode_watch w
              JOIN {_EPISODE} e ON e.id = w.episode_id
             WHERE e.show_id = s.id) AS episode_watches,
           (SELECT count(*)
              FROM app.user_show_rating r
             WHERE r.show_id = s.id) AS show_ratings,
           (SELECT count(*)
              FROM app.user_episode_rating r
              JOIN {_EPISODE} e ON e.id = r.episode_id
             WHERE e.show_id = s.id) AS episode_ratings,
           (SELECT coalesce(array_agg(u.email ORDER BY u.email), ARRAY[]::text[])
              FROM app."user" u
             WHERE EXISTS (SELECT 1 FROM app.user_show_watch t
                            WHERE t.user_id = u.id AND t.show_id = s.id)
                OR EXISTS (SELECT 1 FROM app.user_show_rating r
                            WHERE r.user_id = u.id AND r.show_id = s.id)
                OR EXISTS (SELECT 1 FROM app.user_episode_watch w
                            JOIN {_EPISODE} e ON e.id = w.episode_id
                           WHERE w.user_id = u.id AND e.show_id = s.id)
                OR EXISTS (SELECT 1 FROM app.user_episode_rating r
                            JOIN {_EPISODE} e ON e.id = r.episode_id
                           WHERE r.user_id = u.id AND e.show_id = s.id)) AS users
      FROM catalog.show s
     -- Unmapped, or mapped by a guess. An exact tier and a row the TMDB ingest
     -- inserted directly (`tmdb_id` set, method NULL — it knew its own id) both
     -- fall out here. `human` is excluded last and separately, because it is the
     -- only value that can carry either shape and still be settled.
     WHERE s.id IN (SELECT show_id FROM touched WHERE show_id IS NOT NULL)
       AND (s.tmdb_id IS NULL OR s.match_method = :title_year)
       AND s.match_method IS DISTINCT FROM :human
     ORDER BY episode_watches DESC, s.id
""")

_LOCK_SHOW = text("""
    SELECT id, name, tmdb_id, match_method
      FROM catalog.show
     WHERE id = :id
       FOR UPDATE
""")

_HOLDER = text("SELECT id, name FROM catalog.show WHERE tmdb_id = :tmdb_id AND id <> :id")

_RESOLVE = text("""
    UPDATE catalog.show
       SET tmdb_id = :tmdb_id, match_method = :match_method
     WHERE id = :id
""")


class QueueError(Exception):
    """A refused resolution. The message is what the operator needs to read."""


@dataclass(frozen=True)
class QueueRow:
    """One unresolved show, with everything needed to decide about it.

    Deliberately flat and JSON-shaped: the report is read in a terminal and
    piped over `ssh docker exec`, so the row is its own serialisation.
    """

    show_id: int
    name: str
    first_air_date: date | None
    original_language: str | None
    status: str | None
    tmdb_id: int | None
    match_method: str | None
    tvdb_id: int | None
    imdb_id: str | None
    episode_watches: int
    show_ratings: int
    episode_ratings: int
    users: tuple[str, ...]

    @property
    def carries_user_data(self) -> bool:
        """Whether being wrong about this row would cost anything.

        Tracking a show is re-addable in a click; watch and rating history is
        not. This is what sorts the queue and what decides how hard to look.
        """
        return bool(self.episode_watches or self.show_ratings or self.episode_ratings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "show_id": self.show_id,
            "name": self.name,
            "first_air_date": self.first_air_date.isoformat() if self.first_air_date else None,
            "original_language": self.original_language,
            "status": self.status,
            "tmdb_id": self.tmdb_id,
            "match_method": self.match_method,
            "tvdb_id": self.tvdb_id,
            "imdb_id": self.imdb_id,
            "episode_watches": self.episode_watches,
            "show_ratings": self.show_ratings,
            "episode_ratings": self.episode_ratings,
            "users": list(self.users),
        }


async def build_queue(db: AsyncSession) -> list[QueueRow]:
    """Every user-touched show without a verified mapping, worst risk first.

    Ordered by episode watches descending, because that is the only column that
    says how much a wrong answer costs. Needs no TMDB credential — the database
    half of the report always works.
    """
    rows = (await db.execute(_QUEUE, {"title_year": MATCH_TITLE_YEAR, "human": MATCH_HUMAN})).all()
    return [
        QueueRow(
            show_id=row.id,
            name=row.name,
            first_air_date=row.first_air_date,
            original_language=row.original_language,
            status=row.status,
            tmdb_id=row.tmdb_id,
            match_method=row.match_method,
            tvdb_id=row.tvdb_id,
            imdb_id=row.imdb_id,
            episode_watches=row.episode_watches,
            show_ratings=row.show_ratings,
            episode_ratings=row.episode_ratings,
            users=tuple(row.users or ()),
        )
        for row in rows
    ]


async def unmirrored_user_touched_shows(db: AsyncSession) -> list[int]:
    """Shows a user has touched that the copy never put into `catalog` — see `_UNMIRRORED`.

    Separate from `build_queue` because it is a different problem with a
    different fix: these rows need `task copy:catalog` re-run, not a person's
    judgement. Public so the cutover gate can assert it is empty alongside the
    queue itself.
    """
    return [row.show_id for row in (await db.execute(_UNMIRRORED)).all()]


def _candidate(result: dict[str, Any]) -> dict[str, Any]:
    """A `/search/tv` hit, trimmed to what identifies a show to a person."""
    return {
        "tmdb_id": result.get("id"),
        "name": result.get("name"),
        "original_name": result.get("original_name"),
        "first_air_date": result.get("first_air_date") or None,
        "overview": result.get("overview") or None,
    }


async def search_candidates(
    client: TMDBClient, row: QueueRow, *, limit: int = CANDIDATE_LIMIT
) -> list[dict[str, Any]]:
    """What TMDB offers for this row's title, unfiltered by the tier-3 rules.

    The mapping pass rejects anything ambiguous; a person is exactly the thing
    that can resolve ambiguity, so the candidates arrive here as they came —
    several results, a near-miss year and a differently-punctuated title are all
    signal to a reviewer where they were disqualifying to the pass.
    """
    payload = await client.search_tv(row.name)
    return [_candidate(result) for result in (payload.get("results") or [])[:limit]]


async def annotate(client: TMDBClient | None, rows: list[QueueRow]) -> list[dict[str, Any]]:
    """The report: each row as JSON, plus TMDB context when a client is supplied.

    A row that already holds a `tmdb_id` is a tier-3 guess awaiting review, so it
    also carries `current_match` — what that id actually points at, which is the
    whole question being asked about it.

    **An upstream failure degrades the row rather than the report.** The DB half
    is the part that cannot be reconstructed by hand; losing a six-row report to
    one 503 while a migration window is open would be the wrong trade.
    """
    report = []
    for row in rows:
        entry = row.to_dict()
        if client is not None:
            try:
                entry["candidates"] = await search_candidates(client, row)
                if row.tmdb_id is not None:
                    series = await client.get_tv_series(row.tmdb_id, append=[])
                    entry["current_match"] = _candidate(series)
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                log.warning("TMDB lookup failed for catalog show %d: %s", row.show_id, exc)
                entry["tmdb_error"] = f"{type(exc).__name__}: {exc}"
        report.append(entry)
    return report


async def _load_for_resolution(
    db: AsyncSession, show_id: int
) -> Row[tuple[int, str, int | None, str | None]]:
    """Lock the row and refuse the two cases a queue command must not touch."""
    row = (await db.execute(_LOCK_SHOW, {"id": show_id})).one_or_none()
    if row is None:
        raise QueueError(f"no catalog.show with id {show_id}")
    if row.match_method not in RESOLVABLE_METHODS:
        raise QueueError(
            f"catalog show {show_id} ({row.name}) matched by {row.match_method}, which is exact "
            f"— the queue never surfaced it, so retract it deliberately before resolving it here"
        )
    return row


async def confirm(db: AsyncSession, show_id: int, tmdb_id: int) -> str:
    """Record that a person verified this show is TMDB series `tmdb_id`.

    Covers three shapes with one command, because to a reviewer they are one
    decision: attaching an id to an unmatched row, re-pointing a tier-3 guess
    that was wrong, and re-stamping a guess that was right. Only the last is a
    no-op on `tmdb_id`, and it is the one that matters most — it is what moves a
    confirmed guess out of a ticket comment and into the database.
    """
    row = await _load_for_resolution(db, show_id)
    if row.match_method == MATCH_HUMAN and row.tmdb_id not in (None, tmdb_id):
        raise QueueError(
            f"catalog show {show_id} ({row.name}) was already resolved by hand onto TMDB "
            f"{row.tmdb_id}; reject it first if that verdict was wrong"
        )

    holder = (await db.execute(_HOLDER, {"tmdb_id": tmdb_id, "id": show_id})).one_or_none()
    if holder is not None:
        raise QueueError(
            f"TMDB {tmdb_id} is already held by catalog show {holder.id} ({holder.name}) — "
            f"resolving this needs the two rows reconciled, not a second claim on the id"
        )

    await db.execute(_RESOLVE, {"id": show_id, "tmdb_id": tmdb_id, "match_method": MATCH_HUMAN})
    await db.commit()

    if row.tmdb_id == tmdb_id:
        return f"re-stamped catalog show {show_id} ({row.name}): TMDB {tmdb_id} confirmed by hand"
    if row.tmdb_id is not None:
        return (
            f"re-pointed catalog show {show_id} ({row.name}): TMDB {row.tmdb_id} "
            f"({row.match_method}) retracted, TMDB {tmdb_id} confirmed by hand"
        )
    return f"confirmed catalog show {show_id} ({row.name}) as TMDB {tmdb_id}"


async def reject(db: AsyncSession, show_id: int) -> str:
    """Record that a person looked and TMDB has no counterpart for this show.

    The row stays locally-authored — `tmdb_id IS NULL`, which under ADR-0008 no
    upsert can ever conflict onto — and every watch and rating on it keeps
    working, because nothing in `app` references `tmdb_id`. What changes is that
    the decision is now recorded, which is the only reason the queue can empty.
    """
    row = await _load_for_resolution(db, show_id)
    await db.execute(_RESOLVE, {"id": show_id, "tmdb_id": None, "match_method": MATCH_HUMAN})
    await db.commit()

    retracted = (
        f", TMDB {row.tmdb_id} ({row.match_method}) retracted" if row.tmdb_id is not None else ""
    )
    return f"catalog show {show_id} ({row.name}) left locally-authored by hand{retracted}"

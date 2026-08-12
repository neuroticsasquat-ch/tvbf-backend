"""The genre vocabulary at cutover, and the four queries that read it.

**TMDB's genre names are adopted verbatim; ours are not mapped onto them**
(NEU-1064, ADR-0011). `GET /genres` therefore returns a different list the day
`catalog` becomes the read spine, and 21 of the 28 names the browse filter
accepts today stop existing.

Measured 2026-08-09 — our mirror's 28 against `GET /genre/tv/list`, which
returns 16:

* **In both (7)** — Animation, Comedy, Crime, Drama, Family, Mystery, Western.
* **Ours only (21)** — Action, Adult, Adventure, Anime, Children, DIY,
  Espionage, Fantasy, Food, History, Horror, Legal, Medical, Music, Nature,
  Romance, Science-Fiction, Sports, Supernatural, Thriller, Travel.
* **Theirs only (9)** — Action & Adventure, Documentary, Kids, News, Reality,
  Sci-Fi & Fantasy, Soap, Talk, War & Politics.

So `Science-Fiction` and `Fantasy` both land as `Sci-Fi & Fantasy`, `Anime`
disappears into `Animation`, and `?genre=Anime` matches nothing. That is a real
loss of resolution, accepted for the reason the audit's D1 accepted it for
language: a translation layer is maintained forever, against a vocabulary the
ingest keeps writing and we do not control, and every show it touches then
carries a name TMDB never gave it. ADR-0011 has the argument.

**Genres come only from TMDB, so a show can carry none.** `catalog.genre` is
keyed on `tmdb_id`, so NEU-1042 deliberately copied no TV Maze genre rows — a
copy with `tmdb_id IS NULL` could never match the row the ingest creates, and
every genre would end up stored twice. The rows here are written solely by
`tmdb/upsert.py` from `series.genres`, which means a show TMDB never matched has
an empty genre list rather than the one TV Maze gave it. Every function below
treats that as ordinary: it is roughly 26k shows, not an edge case.

The queries are the same four shapes `tvmaze/browse_queries.py` reads today,
carried across unchanged except where the target schema forced a difference —
see `shows_with_all_genres`. **Nothing calls this yet, and that is the ticket
boundary**: every read still goes to `tvmaze`, and NEU-1047 is the pass that
repoints browse to `catalog`.
"""

from collections.abc import Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.catalog import models as m


async def list_genres(session: AsyncSession) -> list[m.Genre]:
    """Every genre in the mirror, alphabetically — the `GET /genres` body.

    Whatever TMDB has named on a mirrored show, which after a full pass is its
    whole published vocabulary and before one is a subset of it.

    Rows are **not** collapsed by name, where `shows_with_all_genres` counts by
    it. The asymmetry is deliberate: every row here carries its own surrogate
    id, so collapsing two rows sharing a name would mean publishing one of the
    two ids arbitrarily. Two identical options in the picker is a visible,
    harmless duplicate; a name counted twice in the filter silently *drops* the
    shows carrying both, which is why only that one defends itself.
    """
    result = await session.execute(select(m.Genre).order_by(m.Genre.name))
    return list(result.scalars().all())


def shows_with_all_genres(names: Sequence[str]) -> Select[tuple[int]]:
    """Show ids carrying *every* named genre — the AND semantics of `?genre=`.

    Two details differ from the `tvmaze` original, both because the filter's
    unit is the **name** while the join's unit is the row:

    * The count is over distinct **names**, not distinct genre ids.
      `tvmaze.genre` carries `UNIQUE (name)`; `catalog.genre` carries only
      `UNIQUE (tmdb_id)`, because the name is TMDB's to change and the id is
      what an upsert conflicts on. Under TMDB's published list the two counts
      are identical — a name has one id — but if that ever stopped being true,
      counting ids would silently *exclude* the shows carrying both rows, which
      is the opposite of what the filter asked for.
    * Repeated values are collapsed first. `?genre=Comedy&genre=Comedy` names
      one genre, so the bar is one; comparing against the raw parameter count
      would make the query unsatisfiable and return nothing at all.

    **Naming no genres is not a wildcard.** An empty `names` selects no shows
    at all, exactly as `WHERE name IN ()` reads — so a caller filters only when
    the parameter was supplied, the way `list_shows` guards with
    `if filters.genres:` today. Pinned by a test rather than left to be
    rediscovered at the call site.
    """
    wanted = list(dict.fromkeys(names))
    return (
        select(m.ShowGenre.show_id)
        .join(m.Genre, m.Genre.id == m.ShowGenre.genre_id)
        .where(m.Genre.name.in_(wanted))
        .group_by(m.ShowGenre.show_id)
        .having(func.count(func.distinct(m.Genre.name)) == len(wanted))
    )


async def genres_by_show(session: AsyncSession, show_ids: Sequence[int]) -> dict[int, list[str]]:
    """Genre names for a page of shows, in one query.

    Every requested id gets a key, so a show with no genres — the copied rows
    TMDB never matched — reads as `[]` rather than missing, and the caller needs
    no `.get(id, [])` to serialise it.
    """
    if not show_ids:
        return {}
    rows = (
        await session.execute(
            select(m.ShowGenre.show_id, m.Genre.name)
            .join(m.Genre, m.Genre.id == m.ShowGenre.genre_id)
            .where(m.ShowGenre.show_id.in_(show_ids))
        )
    ).all()
    by_show: dict[int, list[str]] = {sid: [] for sid in show_ids}
    for show_id, name in rows:
        by_show[show_id].append(name)
    return by_show


async def genres_for_show(session: AsyncSession, show_id: int) -> list[m.Genre]:
    """One show's genres, alphabetically — the `GET /shows/{id}` detail body."""
    result = await session.execute(
        select(m.Genre)
        .join(m.ShowGenre, m.ShowGenre.genre_id == m.Genre.id)
        .where(m.ShowGenre.show_id == show_id)
        .order_by(m.Genre.name)
    )
    return list(result.scalars().all())

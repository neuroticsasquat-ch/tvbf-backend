"""The browse, search and credits query layer, reading `catalog`.

Ported from `tvmaze/browse_queries.py` at the repoint (NEU-1047). The shapes are
the originals — the AKA-aware semi-join, the folded-search machinery, the batch
hydration that keeps `GET /shows` at a fixed query count, the tombstone filter
scoped to discovery — and each is carried across rather than rewritten. Where a
query had to change, it is because the target schema forced it:

* **`network` and `web_channel` are one concept now.** `tvmaze.show` carried two
  scalar FKs; TMDB returns `networks[]` and `catalog` models it as the
  `show_network` join table (audit §6). So the `?network=` filter becomes a
  semi-join instead of an `IN` on a column, hydration reads one query instead of
  two, and a show with several networks resolves to **the alphabetically first**
  — TMDB sends the array in an order we do not store, so alphabetical is the only
  choice that is stable across re-ingests rather than across payloads.
* **Genre queries live in `catalog/genres.py`** (NEU-1064), which owns the
  vocabulary change and the name-vs-id counting rule that goes with it.
* **Seasons are deduplicated on read** by `catalog/seasons.py` (NEU-1047), the
  one payload this repoint allows to differ.
* **Credits sort by `episode_count`, not by a billing order.** TMDB sends no
  `order` on a crew entry at all and `aggregate_credits` gives the measure
  `order` only ever proxied for, so both credit tables lead their index on it.
* **"Regular" is read off `season_cast`, never inferred** (NEU-1512). A regular
  credit is a (person, character) upstream lists on one of the show's seasons;
  every other `show_cast` row is a guest. Crew has no season-grain source worth
  fetching, so a series crew credit is the one derivation left: a `show_crew` job
  whose aggregate count exceeds the person's `episode_crew` rows in it.

`GET /shows` still issues four queries for a page of any size: count, page,
genres-by-show, networks-by-show. It was five before — dropping `web_channel`
dropped one.
"""

import unicodedata
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    distinct,
    false,
    func,
    literal,
    select,
    union,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from tvbf.app.repos import episode_rating_repo, show_rating_repo
from tvbf.catalog import episodes as episode_rules
from tvbf.catalog import genres as genre_queries
from tvbf.catalog import models as m
from tvbf.catalog import seasons as season_rules
from tvbf.catalog.schemas import ALLOWED_SORT_KEYS, ShowFilters
from tvbf.sorting import SQL_LEADING_ARTICLE_PATTERN
from tvbf.sql_fold import folded

# Strip leading articles for natural alphabetical sort: "The Office" → "office".
_NORMALIZED_NAME = func.regexp_replace(func.lower(m.Show.name), SQL_LEADING_ARTICLE_PATTERN, "")

# `?sort=tvmaze_updated` keeps its name and now means *when we last mirrored this
# show* — see `schemas._updated_epoch`, which reads the same two columns in the
# same order so the sort and the serialized field cannot disagree.
_MIRRORED_AT = func.coalesce(m.Show.tmdb_synced_at, m.Show.ingested_at)

_SORT_EXPRS = {
    "name": _NORMALIZED_NAME.asc(),
    "-name": _NORMALIZED_NAME.desc(),
    "premiered": m.Show.first_air_date.asc().nulls_last(),
    "-premiered": m.Show.first_air_date.desc().nulls_last(),
    "tvmaze_updated": _MIRRORED_AT.asc(),
    "-tvmaze_updated": _MIRRORED_AT.desc(),
    # The stored column (NEU-1502), not a per-row aggregate: `-last_aired` over
    # the whole catalog is then a walk of `ix_show_last_aired_live`.
    "last_aired": m.Show.last_aired.asc().nulls_last(),
    "-last_aired": m.Show.last_aired.desc().nulls_last(),
}


def _shows_on_networks(network_ids: list[int]) -> Select[tuple[int]]:
    """Show ids carrying *any* of the named networks — the OR semantics `?network=`
    has always had, expressed as a semi-join now that the FK lives on a join table."""
    return select(m.ShowNetwork.show_id).where(m.ShowNetwork.network_id.in_(network_ids))


async def list_genres(session: AsyncSession) -> list[m.Genre]:
    return await genre_queries.list_genres(session)


async def list_networks(session: AsyncSession) -> list[m.Network]:
    result = await session.execute(select(m.Network).order_by(m.Network.name))
    return list(result.scalars().all())


async def get_show_with_seasons(
    session: AsyncSession, show_id: int
) -> tuple[m.Show, list[m.Season], list[m.Genre], m.Network | None] | None:
    show = (await session.execute(select(m.Show).where(m.Show.id == show_id))).scalar_one_or_none()
    if show is None:
        return None

    seasons = await get_show_seasons(session, show_id)
    genres = await genre_queries.genres_for_show(session, show_id)
    network = (await primary_networks(session, [show_id])).get(show_id)
    return show, seasons, genres, network


async def primary_networks(session: AsyncSession, show_ids: Sequence[int]) -> dict[int, m.Network]:
    """The one network the API exposes per show, of however many each carries.

    Alphabetically first, id breaking ties. TMDB's `networks[]` has an order we
    do not store, so picking "the first one upstream sent" is not available; a
    name ordering at least does not change when the array does.

    **One query however many shows are asked for**, which is what keeps
    `GET /shows` at a fixed count, and one implementation of the rule — a
    `DISTINCT ON` for the page plus an `ORDER BY ... LIMIT 1` for the detail
    route would be the same decision written twice, in two languages, free to
    drift. Shows with no `show_network` row are simply absent from the result.
    """
    if not show_ids:
        return {}
    rows = (
        await session.execute(
            select(m.ShowNetwork.show_id, m.Network)
            .join(m.Network, m.Network.id == m.ShowNetwork.network_id)
            .where(m.ShowNetwork.show_id.in_(show_ids))
        )
    ).all()
    best: dict[int, m.Network] = {}
    for show_id, network in rows:
        incumbent = best.get(show_id)
        if incumbent is None or (network.name, network.id) < (incumbent.name, incumbent.id):
            best[show_id] = network
    return best


async def get_show_seasons(session: AsyncSession, show_id: int) -> list[m.Season]:
    """A show's seasons, one per season number — see `catalog/seasons.py`."""
    result = await session.execute(
        select(m.Season).where(m.Season.show_id == show_id).order_by(*season_rules.SEASON_ORDER)
    )
    return season_rules.deduped(result.scalars().all())


async def get_show_season(session: AsyncSession, show_id: int, number: int) -> m.Season | None:
    """The season row a show serves at one season number, or None.

    Resolved through `get_show_seasons` rather than a `WHERE season_number =`,
    so a duplicated number picks the same row the seasons route lists.
    """
    seasons = await get_show_seasons(session, show_id)
    return next((s for s in seasons if s.season_number == number), None)


async def show_exists(session: AsyncSession, show_id: int) -> bool:
    result = await session.execute(select(m.Show.id).where(m.Show.id == show_id))
    return result.scalar_one_or_none() is not None


async def get_episode(session: AsyncSession, episode_id: int) -> m.Episode | None:
    result = await session.execute(select(m.Episode).where(m.Episode.id == episode_id))
    return result.scalar_one_or_none()


async def episode_exists(session: AsyncSession, episode_id: int) -> bool:
    result = await session.execute(select(m.Episode.id).where(m.Episode.id == episode_id))
    return result.scalar_one_or_none() is not None


async def get_show_episodes(
    session: AsyncSession, show_id: int, season: int | None
) -> list[m.Episode]:
    stmt = select(m.Episode).where(m.Episode.show_id == show_id)
    if season is not None:
        stmt = stmt.where(m.Episode.season_number == season)
    stmt = stmt.order_by(*episode_rules.EPISODE_ORDER)
    result = await session.execute(stmt)
    return list(result.scalars().all())


SIMILAR_LIMIT = 12
"""Project spec §2: twenty rows are mirrored per show and twelve are served."""


async def list_similar_shows(
    session: AsyncSession, show_id: int, *, limit: int = SIMILAR_LIMIT
) -> list[m.Show]:
    """TMDB's "More like this" for one show, in TMDB's own rank order.

    One join over `catalog.show_recommendation`, which the ingest and the nightly
    delta already keep current (NEU-1052) — a request never reaches upstream
    (ADR-0002).

    **`adult` and `deleted_upstream_at` are filtered here, at read time**, on
    NEU-1108's precedent and for its reason: a list mirrored in March can name a
    show tombstoned in June, and a write-time copy of this filter would make a
    resurrected show permanently invisible. The filters run *before* the cap, so
    twelve means twelve survivors — which is what storing twenty leaves headroom
    for.

    Ranks may have gaps, because a target that did not resolve to a
    `catalog.show` was dropped rather than renumbered at write time. The order is
    all the read path takes from them, so a gap costs nothing here.
    """
    result = await session.execute(
        select(m.Show)
        .join(m.ShowRecommendation, m.ShowRecommendation.target_show_id == m.Show.id)
        .where(
            m.ShowRecommendation.source_show_id == show_id,
            m.Show.adult.is_(False),
            m.Show.deleted_upstream_at.is_(None),
        )
        .order_by(m.ShowRecommendation.rank)
        .limit(limit)
    )
    return list(result.scalars().all())


TRENDING_MAX_AGE = timedelta(days=7)
"""Project spec §3: past this, the snapshot is not served at all.

The cutoff lives here, on the read, and nowhere else — not in the SPA and not in
the job. A rule enforced in two places drifts, and what drifts into is week-old
rows under a label reading "trending right now". Silent staleness under a
present-tense label is worse than an absent section, which is why the answer to
an old snapshot is an empty list rather than a smaller one or a warning flag.
"""


async def get_trending_snapshot(session: AsyncSession) -> tuple[datetime | None, list[m.Show]]:
    """The current trending snapshot: `(captured_at, shows)`, in TMDB's rank order.

    One join over `catalog.trending_show`, which the daily job replaces whole
    (NEU-1055) — a request never reaches upstream (ADR-0002).

    **The staleness cutoff is applied in the query**, so there is no path through
    this module that returns a row past it. It is measured against `captured_at`,
    which the job stamps *before* the request goes out, so it describes the list
    rather than the bookkeeping that stored it. The window is not a parameter:
    the constant is the whole rule, and an injectable override would be the
    second place to enforce it that the constant's own docstring rules out.

    The cutoff is computed from Python's clock rather than Postgres's `now()`
    because Python's is the clock that wrote the value; comparing the two would
    make the answer depend on the skew between them.

    **`captured_at` is taken from the rows returned, so it is null exactly when
    the list is empty.** It describes the list in hand: reporting the timestamp
    of a snapshot withheld for being stale would hand a client everything it
    needs to re-derive the cutoff this route exists to own.

    `adult` and `deleted_upstream_at` are filtered here, at read time, on
    NEU-1053's and NEU-1108's precedent — the job deliberately does not apply
    them on the way in, so a resurrected show returns to the list rather than
    being invisible until the next snapshot.

    Ranks may have gaps: an entry the job could not resolve to a `catalog.show`
    was dropped rather than renumbered. The order is all this reads from them.

    **This leans on `catalog.trending_show` holding exactly one snapshot** — the
    job replaces the lot inside one transaction, so every surviving row carries
    the same `captured_at` and the first one's is the list's. Should that table
    ever become a history, this function has to scope itself to the newest
    snapshot rather than to the window, or it will interleave two vintages and
    report the lowest-ranked row's timestamp as the list's.
    """
    cutoff = datetime.now(tz=UTC) - TRENDING_MAX_AGE
    result = await session.execute(
        select(m.TrendingShow.captured_at, m.Show)
        .join(m.Show, m.Show.id == m.TrendingShow.show_id)
        .where(
            m.TrendingShow.captured_at >= cutoff,
            m.Show.adult.is_(False),
            m.Show.deleted_upstream_at.is_(None),
        )
        .order_by(m.TrendingShow.rank)
    )
    rows = result.all()
    if not rows:
        return None, []
    return rows[0][0], [show for _captured_at, show in rows]


ANTICIPATED_WINDOW_DAYS = 365
"""How far ahead the most-anticipated list looks (project spec §4).

Barely binds, and is meant not to: 385 of the 408 future-dated shows in
production fall inside a year, so its real job is excluding placeholder entries
dated far out — TMDB carries a *Ben-Hur* in 2027 — rather than sizing the list,
which `ANTICIPATED_LIMIT` does. A year also happens to be the horizon past which
an announced date is a guess.
"""

ANTICIPATED_LIMIT = 24
"""How many are served (project spec §4).

A page-layout decision rather than a quality one: measured on the production
mirror, ranks 21-45 still read *Blade Runner 2099*, *Crystal Lake*, *Ben-Hur*,
so quality holds well past twenty and the number is free to be whatever the grid
wants. Unlike `SIMILAR_LIMIT` there is no headroom to reserve, because the
filters are in the query rather than applied to stored rows.
"""


async def list_anticipated_shows(
    session: AsyncSession,
    *,
    window_days: int = ANTICIPATED_WINDOW_DAYS,
    limit: int = ANTICIPATED_LIMIT,
) -> list[m.Show]:
    """Shows premiering between today and `window_days` out, most popular first.

    A live query over `catalog.show` — no upstream call (ADR-0002), and no
    snapshot table either. Measured on 2026-08-16, this and
    `/discover/tv?first_air_date.gte=…&sort_by=popularity.desc` agree on every
    show in the top fifteen, differing only in an ordering that our popularity
    being six days stale entirely explains — the staleness NEU-1172 fixes. Since
    ADR-0007, TMDB's catalog *is* our catalog, and `/discover/tv` is a query
    against it.

    **The date comparison being in the query is what makes the surface correct
    rather than fresh.** A snapshot would need a rule for dropping shows that
    premiered since it was taken, a rule for what a failed run leaves behind, and
    a staleness cutoff of the kind `get_trending_snapshot` carries. `current_date`
    is evaluated on the read, so all three problems are absent rather than
    solved.

    **An undated show never appears**, which the `>=` comparison enforces on its
    own: 2,501 shows carry `Planned` / `In Production` / `Pilot` with no
    `first_air_date`, and there is no defensible position to sort a show with no
    date into. **`status` is deliberately not in the predicate** — *Lanterns* is
    `Returning Series` with a future first air date and belongs on the list, so
    filtering on status would drop exactly the returning-favourite entries the
    surface is most wanted for.

    **There is no `vote_count` floor**, and nothing replaces it. Of the 408
    future-dated shows in production four have any votes and one has ten or
    more; unpremiered shows do not get voted on, which is what "unpremiered"
    means, so a floor there is a category error rather than a threshold needing
    tuning. Popularity is already doing that filtering, because a show nobody
    has heard of does not accumulate a popularity score either.

    `adult` and `deleted_upstream_at` are filtered here, on the read, as
    everywhere else in this module.

    **An unscored show is served last, not withheld.** `popularity` is NULL for a
    show the export has never carried a score for, and `NULLS LAST` is the whole
    of what that means here: absent evidence of interest is not evidence of
    absent interest, and the window and the limit already bound the list.

    The id breaks ties, because `ORDER BY popularity` alone is a partial order —
    two shows carrying the same score, or both carrying none, may come back in
    either order from one request to the next, which a cached browse response
    then freezes at random.
    """
    result = await session.execute(
        select(m.Show)
        .where(
            m.Show.deleted_upstream_at.is_(None),
            m.Show.adult.is_(False),
            m.Show.first_air_date >= func.current_date(),
            m.Show.first_air_date < func.current_date() + literal(window_days),
        )
        .order_by(m.Show.popularity.desc().nullslast(), m.Show.id)
        .limit(limit)
    )
    return list(result.scalars().all())


# Credit ordering. `episode_count` is the measure TV Maze's `sort_order` only ever
# proxied for, and it is the one ordering show cast and show crew can share — both
# indexes lead on it. Descending, so the most-present person is billed first;
# Postgres scans the index backwards for that. The credit id breaks ties, or the
# order within a show is nondeterministic across requests.
_CREDIT_COUNT_DESC = func.coalesce(m.ShowCast.episode_count, 0).desc()
_CREW_COUNT_DESC = func.coalesce(m.ShowCrew.episode_count, 0).desc()


# A null `billing_order` or `credit_order` sorts after every real one.
_LAST = 2**31 - 1


def _series_crew(episode_rows: ColumnElement) -> ColumnElement[bool]:
    """A `show_crew` job is series crew when its aggregate count exceeds the
    person's episode credits in it on that show (NEU-1512 §2.2).

    A director of forty episodes holds forty episode credits and an aggregate of
    forty — a sum, which the episode rows already list. An Executive Producer
    holds no episode rows at all. `episode_rows` is the count of the former,
    null when there are none.
    """
    return func.coalesce(m.ShowCrew.episode_count, 0) > func.coalesce(episode_rows, 0)


async def list_show_cast(
    session: AsyncSession, show_id: int
) -> list[tuple[m.Person, m.Character, int | None]]:
    """Regular credits for one show, with the aggregate episode count (NEU-1512).

    A regular is a (person, character) with a `season_cast` row on any of the
    show's seasons — once, however many seasons. The count and billing order come
    from `show_cast`; a regular upstream's aggregate omits has neither and sorts
    last. `show_cast` is grouped first because nothing makes (show, person,
    character) unique there, and a duplicate must not list a regular twice.

    **The join to `character` is inner, which drops a credit whose character TMDB
    left blank** — measured at 1 of 7,629 sampled roles. `catalog.show_cast`
    made `character_id` nullable so one blank cannot abort a multi-hour ingest;
    `CastMemberOut.character` is required, and widening it is a contract change
    for a row that would render as "person as (nothing)" anyway.
    """
    regulars = (
        select(m.SeasonCast.person_id, m.SeasonCast.character_id)
        .join(m.Season, m.Season.id == m.SeasonCast.season_id)
        .where(m.Season.show_id == show_id)
        .distinct()
        .subquery()
    )
    aggregate = (
        select(
            m.ShowCast.person_id,
            m.ShowCast.character_id,
            func.max(m.ShowCast.episode_count).label("episode_count"),
            func.min(m.ShowCast.billing_order).label("billing_order"),
        )
        .where(m.ShowCast.show_id == show_id)
        .group_by(m.ShowCast.person_id, m.ShowCast.character_id)
        .subquery()
    )
    stmt = (
        select(m.Person, m.Character, aggregate.c.episode_count)
        .select_from(regulars)
        .join(m.Person, m.Person.id == regulars.c.person_id)
        .join(m.Character, m.Character.id == regulars.c.character_id)
        .outerjoin(
            aggregate,
            and_(
                aggregate.c.person_id == regulars.c.person_id,
                aggregate.c.character_id == regulars.c.character_id,
            ),
        )
        .order_by(
            aggregate.c.episode_count.desc().nulls_last(),
            aggregate.c.billing_order.asc().nulls_last(),
            m.Person.id.asc(),
            m.Character.id.asc(),
        )
    )
    return list((await session.execute(stmt)).tuples().all())


async def list_show_guest_cast(
    session: AsyncSession, show_id: int
) -> list[tuple[m.Person, m.Character, int | None]]:
    """The `show_cast` rows that are not regular credits, most-present first.

    Together with `list_show_cast` this partitions `show_cast`: a regular who
    guested in another season as the same character is one aggregate row, and it
    is a regular's. The anti-join matches `character_id` with `=` rather than
    `IS NOT DISTINCT FROM` because the inner join to `character` has already
    dropped the null ones, and `=` is what lets Postgres hash it — Law & Order
    carries 11,550 rows here. Covered by ix_show_cast_show_id_episode_count.
    """
    is_regular = (
        select(m.SeasonCast.id)
        .join(m.Season, m.Season.id == m.SeasonCast.season_id)
        .where(
            m.Season.show_id == m.ShowCast.show_id,
            m.SeasonCast.person_id == m.ShowCast.person_id,
            m.SeasonCast.character_id == m.ShowCast.character_id,
        )
        .exists()
    )
    stmt = (
        select(m.Person, m.Character, m.ShowCast.episode_count)
        .join(m.ShowCast, m.ShowCast.person_id == m.Person.id)
        .join(m.Character, m.Character.id == m.ShowCast.character_id)
        .where(m.ShowCast.show_id == show_id, ~is_regular)
        .order_by(
            _CREDIT_COUNT_DESC,
            func.coalesce(m.ShowCast.billing_order, _LAST).asc(),
            m.ShowCast.id.asc(),
        )
    )
    return list((await session.execute(stmt)).tuples().all())


async def _list_show_crew(
    session: AsyncSession, show_id: int, *, series: bool
) -> list[tuple[m.Person, m.CrewRole, int | None]]:
    """One half of a show's crew, split by `_series_crew`, most-present first.

    Covered by ix_show_crew_show_id_episode_count, plus the show's episodes'
    crew rows counted per (person, role) — ix_episode_show_id_season_number, then
    uq_episode_crew_episode_person_role.
    """
    episode_rows = (
        select(m.EpisodeCrew.person_id, m.EpisodeCrew.role_id, func.count().label("n"))
        .join(m.Episode, m.Episode.id == m.EpisodeCrew.episode_id)
        .where(m.Episode.show_id == show_id)
        .group_by(m.EpisodeCrew.person_id, m.EpisodeCrew.role_id)
        .subquery()
    )
    is_series = _series_crew(episode_rows.c.n)
    stmt = (
        select(m.Person, m.CrewRole, m.ShowCrew.episode_count)
        .join(m.ShowCrew, m.ShowCrew.person_id == m.Person.id)
        .join(m.CrewRole, m.CrewRole.id == m.ShowCrew.role_id)
        .outerjoin(
            episode_rows,
            and_(
                episode_rows.c.person_id == m.ShowCrew.person_id,
                episode_rows.c.role_id == m.ShowCrew.role_id,
            ),
        )
        .where(m.ShowCrew.show_id == show_id, is_series if series else ~is_series)
        .order_by(_CREW_COUNT_DESC, m.CrewRole.job.asc(), m.ShowCrew.id.asc())
    )
    return list((await session.execute(stmt)).tuples().all())


async def list_show_crew(
    session: AsyncSession, show_id: int
) -> list[tuple[m.Person, m.CrewRole, int | None]]:
    """Series crew for one show (NEU-1512 §2.2) — Executive Producer, Creator,
    Composer: jobs held across the series rather than episode by episode."""
    return await _list_show_crew(session, show_id, series=True)


async def list_show_episode_crew(
    session: AsyncSession, show_id: int
) -> list[tuple[m.Person, m.CrewRole, int | None]]:
    """The rest of a show's crew: jobs whose aggregate is the sum of the episode
    credits `episode_crew` already holds — directors, writers, editors."""
    return await _list_show_crew(session, show_id, series=False)


async def list_season_regulars(
    session: AsyncSession, season: m.Season
) -> list[tuple[m.Person, m.Character]]:
    """A season's regular cast in billing order (NEU-1512). Covered by the
    leading column of uq_season_cast_season_person_character."""
    stmt = (
        select(m.Person, m.Character)
        .join(m.SeasonCast, m.SeasonCast.person_id == m.Person.id)
        .join(m.Character, m.Character.id == m.SeasonCast.character_id)
        .where(m.SeasonCast.season_id == season.id)
        .order_by(func.coalesce(m.SeasonCast.billing_order, _LAST).asc(), m.SeasonCast.id.asc())
    )
    return list((await session.execute(stmt)).tuples().all())


async def list_season_guests(
    session: AsyncSession, season: m.Season
) -> list[tuple[m.Person, m.Character, int]]:
    """A season's guest stars, one per (person, character) with its appearances.

    The season's episodes are the show's at that season number — the scope
    `GET /shows/{id}/episodes?season=N` serves. A pair the season's regular list
    also holds is dropped: an entry in both is upstream inconsistency, and the
    season's own list wins at season grain. Ordered by appearances, then the
    earliest credit position, then person.
    """
    is_regular = (
        select(m.SeasonCast.id)
        .where(
            m.SeasonCast.season_id == season.id,
            m.SeasonCast.person_id == m.EpisodeGuestCast.person_id,
            m.SeasonCast.character_id == m.EpisodeGuestCast.character_id,
        )
        .exists()
    )
    guests = (
        select(
            m.EpisodeGuestCast.person_id,
            m.EpisodeGuestCast.character_id,
            func.count().label("appearances"),
            func.min(m.EpisodeGuestCast.credit_order).label("first_credit"),
        )
        .join(m.Episode, m.Episode.id == m.EpisodeGuestCast.episode_id)
        .where(
            m.Episode.show_id == season.show_id,
            m.Episode.season_number == season.season_number,
            ~is_regular,
        )
        .group_by(m.EpisodeGuestCast.person_id, m.EpisodeGuestCast.character_id)
        .subquery()
    )
    stmt = (
        select(m.Person, m.Character, guests.c.appearances)
        .select_from(guests)
        .join(m.Person, m.Person.id == guests.c.person_id)
        .join(m.Character, m.Character.id == guests.c.character_id)
        .order_by(
            guests.c.appearances.desc(),
            guests.c.first_credit.asc().nulls_last(),
            m.Person.id.asc(),
            m.Character.id.asc(),
        )
    )
    return list((await session.execute(stmt)).tuples().all())


async def list_season_crew(
    session: AsyncSession, season: m.Season
) -> list[tuple[m.Person, m.CrewRole, int]]:
    """A season's crew, one per (person, role) with the episodes they hold it on.

    Season-grain crew is not ingested (NEU-1512 §2.2); this is the season's
    episode crew grouped. Same episode scope as `list_season_guests`.
    """
    crew = (
        select(m.EpisodeCrew.person_id, m.EpisodeCrew.role_id, func.count().label("episodes"))
        .join(m.Episode, m.Episode.id == m.EpisodeCrew.episode_id)
        .where(
            m.Episode.show_id == season.show_id,
            m.Episode.season_number == season.season_number,
        )
        .group_by(m.EpisodeCrew.person_id, m.EpisodeCrew.role_id)
        .subquery()
    )
    stmt = (
        select(m.Person, m.CrewRole, crew.c.episodes)
        .select_from(crew)
        .join(m.Person, m.Person.id == crew.c.person_id)
        .join(m.CrewRole, m.CrewRole.id == crew.c.role_id)
        .order_by(crew.c.episodes.desc(), m.CrewRole.job.asc(), m.Person.id.asc())
    )
    return list((await session.execute(stmt)).tuples().all())


async def list_episode_guest_cast(
    session: AsyncSession, episode_id: int
) -> list[tuple[m.Person, m.Character]]:
    """Guest-cast credits for one episode in upstream credit order — the
    episode's own credit sequence, not billing order.

    Covered by the leading column of uq_egc_episode_person_character. The credit
    id breaks ties: nothing upstream guarantees `credit_order` is distinct within
    an episode, so the order would otherwise be nondeterministic across requests.
    Same inner join to `character` as `list_show_cast`, for the same reason.
    """
    stmt = (
        select(m.Person, m.Character)
        .join(m.EpisodeGuestCast, m.EpisodeGuestCast.person_id == m.Person.id)
        .join(m.Character, m.Character.id == m.EpisodeGuestCast.character_id)
        .where(m.EpisodeGuestCast.episode_id == episode_id)
        .order_by(
            func.coalesce(m.EpisodeGuestCast.credit_order, _LAST).asc(),
            m.EpisodeGuestCast.id.asc(),
        )
    )
    return list((await session.execute(stmt)).tuples().all())


async def list_episode_crew(
    session: AsyncSession, episode_id: int
) -> list[tuple[m.Person, m.CrewRole]]:
    """Crew credits for one episode.

    **Ordered by job name, where the TV Maze version used upstream's credit
    sequence.** TMDB sends no `order` on a crew entry — 0 of 7,456 sampled — and
    `catalog.episode_crew` has no `episode_count` either, so there is no upstream
    signal left to sort on and the alternative is an arbitrary id order. The tie
    on id still matters: one person holds more than one crew role on 36 of 1,043
    sampled episodes (ADR-0003).
    """
    stmt = (
        select(m.Person, m.CrewRole)
        .join(m.EpisodeCrew, m.EpisodeCrew.person_id == m.Person.id)
        .join(m.CrewRole, m.CrewRole.id == m.EpisodeCrew.role_id)
        .where(m.EpisodeCrew.episode_id == episode_id)
        .order_by(m.CrewRole.job.asc(), m.EpisodeCrew.id.asc())
    )
    return list((await session.execute(stmt)).tuples().all())


async def get_person(session: AsyncSession, person_id: int) -> m.Person | None:
    result = await session.execute(select(m.Person).where(m.Person.id == person_id))
    return result.scalar_one_or_none()


async def person_exists(session: AsyncSession, person_id: int) -> bool:
    result = await session.execute(select(m.Person.id).where(m.Person.id == person_id))
    return result.scalar_one_or_none() is not None


# A filmography reads most-recent-first, so credited shows are ordered by
# premiere date descending. Shows with no premiere date (unaired, upcoming) sort
# last; show id breaks ties so the order is stable across requests.
_CREDIT_SHOW_ORDER = (m.Show.first_air_date.desc().nulls_last(), m.Show.id.asc())

# Episode-level credits read the same way, by air date. Episodes with no air date
# (unaired, or never dated upstream) sort last; episode id breaks ties.
_CREDIT_EPISODE_ORDER = (m.Episode.air_date.desc().nulls_last(), m.Episode.id.asc())


async def list_person_cast_credits(
    session: AsyncSession, person_id: int
) -> list[tuple[m.Show, m.Character, int | None, list[int], date | None]]:
    """Regular credits for one person: one per (show, character) they are a
    season regular as (NEU-1512), with the aggregate episode count, the season
    numbers and the latest air date across those seasons' episodes.

    Most recently credited first, nulls last. Guest-only shows are absent — they
    are `list_person_guest_credits`' rows, and the SPA merges the two per show.
    Covered by ix_season_cast_person_id, ix_episode_show_id_season_number and
    ix_show_cast_person_id.
    """
    held = (
        select(
            m.Season.show_id.label("show_id"),
            m.SeasonCast.character_id.label("character_id"),
            func.array_agg(distinct(m.Season.season_number)).label("seasons"),
            func.max(m.Episode.air_date).label("last_credited"),
        )
        .select_from(m.SeasonCast)
        .join(m.Season, m.Season.id == m.SeasonCast.season_id)
        # The season routes' episode scope, `(show_id, season_number)`, so the
        # date agrees with the season the person page links to.
        .outerjoin(
            m.Episode,
            and_(
                m.Episode.show_id == m.Season.show_id,
                m.Episode.season_number == m.Season.season_number,
            ),
        )
        .where(m.SeasonCast.person_id == person_id)
        .group_by(m.Season.show_id, m.SeasonCast.character_id)
        .subquery()
    )
    aggregate = (
        select(
            m.ShowCast.show_id,
            m.ShowCast.character_id,
            func.max(m.ShowCast.episode_count).label("episode_count"),
        )
        .where(m.ShowCast.person_id == person_id)
        .group_by(m.ShowCast.show_id, m.ShowCast.character_id)
        .subquery()
    )
    stmt = (
        select(m.Show, m.Character, aggregate.c.episode_count, held.c.seasons, held.c.last_credited)
        .select_from(held)
        .join(m.Show, m.Show.id == held.c.show_id)
        .join(m.Character, m.Character.id == held.c.character_id)
        .outerjoin(
            aggregate,
            and_(
                aggregate.c.show_id == held.c.show_id,
                aggregate.c.character_id == held.c.character_id,
            ),
        )
        .order_by(held.c.last_credited.desc().nulls_last(), m.Show.id.asc(), m.Character.id.asc())
    )
    rows = (await session.execute(stmt)).tuples().all()
    # `array_agg(DISTINCT …)` promises no order; the contract is ascending.
    return [(show, char, count, sorted(seasons), last) for show, char, count, seasons, last in rows]


async def list_person_crew_credits(
    session: AsyncSession, person_id: int
) -> list[tuple[m.Show, m.CrewRole, int | None]]:
    """Series crew credits for one person (NEU-1512 §2.2), with the aggregate
    episode count. A job that only sums their episode credits is left to
    `list_person_episode_crew_credits`. Covered by ix_show_crew_person_id and
    ix_episode_crew_person_id."""
    episode_rows = (
        select(m.Episode.show_id, m.EpisodeCrew.role_id, func.count().label("n"))
        .join(m.Episode, m.Episode.id == m.EpisodeCrew.episode_id)
        .where(m.EpisodeCrew.person_id == person_id)
        .group_by(m.Episode.show_id, m.EpisodeCrew.role_id)
        .subquery()
    )
    stmt = (
        select(m.Show, m.CrewRole, m.ShowCrew.episode_count)
        .join(m.ShowCrew, m.ShowCrew.show_id == m.Show.id)
        .join(m.CrewRole, m.CrewRole.id == m.ShowCrew.role_id)
        .outerjoin(
            episode_rows,
            and_(
                episode_rows.c.show_id == m.ShowCrew.show_id,
                episode_rows.c.role_id == m.ShowCrew.role_id,
            ),
        )
        .where(m.ShowCrew.person_id == person_id, _series_crew(episode_rows.c.n))
        # Crew is the common multi-credit case — one person is routinely writer
        # and director on the same show — so job name orders within a show, and
        # the credit id keeps even identical jobs stable.
        .order_by(*_CREDIT_SHOW_ORDER, m.CrewRole.job.asc(), m.ShowCrew.id.asc())
    )
    return list((await session.execute(stmt)).tuples().all())


async def list_person_guest_credits(
    session: AsyncSession, person_id: int
) -> list[tuple[m.Episode, m.Show, m.Character]]:
    """Guest-cast credits for one person, joined through episode → show so each
    entry can render "Show — S2E11" without a second round trip.

    Ordered by air date descending (`_CREDIT_EPISODE_ORDER`): `credit_order` on a
    guest credit is credit order within its own episode and says nothing useful
    across episodes. Within one episode the credit id keeps the order stable.
    """
    stmt = (
        select(m.Episode, m.Show, m.Character)
        .join(m.EpisodeGuestCast, m.EpisodeGuestCast.episode_id == m.Episode.id)
        .join(m.Show, m.Show.id == m.Episode.show_id)
        .join(m.Character, m.Character.id == m.EpisodeGuestCast.character_id)
        .where(m.EpisodeGuestCast.person_id == person_id)
        .order_by(*_CREDIT_EPISODE_ORDER, m.EpisodeGuestCast.id.asc())
    )
    return list((await session.execute(stmt)).tuples().all())


async def list_person_episode_crew_credits(
    session: AsyncSession, person_id: int
) -> list[tuple[m.Episode, m.Show, m.CrewRole]]:
    """Episode-crew credits for one person, joined through episode → show so each
    entry can render "Show — S1E3" without a second round trip.

    Ordered by air date descending like `list_person_guest_credits`, nulls last.
    A person routinely holds both Writer and Director on one episode; within that
    episode job name orders them, the same order `list_episode_crew` serves, so
    the two views of one episode agree.
    """
    stmt = (
        select(m.Episode, m.Show, m.CrewRole)
        .join(m.EpisodeCrew, m.EpisodeCrew.episode_id == m.Episode.id)
        .join(m.Show, m.Show.id == m.Episode.show_id)
        .join(m.CrewRole, m.CrewRole.id == m.EpisodeCrew.role_id)
        .where(m.EpisodeCrew.person_id == person_id)
        .order_by(*_CREDIT_EPISODE_ORDER, m.CrewRole.job.asc(), m.EpisodeCrew.id.asc())
    )
    return list((await session.execute(stmt)).tuples().all())


def _strip_punct_space(token: str) -> str:
    """Token with punctuation and whitespace removed. Used only to detect tokens
    that fold to nothing (e.g. "--"): ``unaccent`` never maps a non-empty letter
    to empty, so emptiness depends solely on the punctuation/space strip."""
    return "".join(
        c for c in token if not (unicodedata.category(c)[0] in ("P", "Z") or c.isspace())
    )


def _search_tokens(search: str | None) -> list[str]:
    """Whitespace tokens that fold to something — the one tokenizer both show
    search and its badge use. Empty when the query was all punctuation."""
    return [t for t in (search or "").split() if _strip_punct_space(t)]


# pg_trgm can drive an index from a substring pattern only when it holds one
# whole trigram; a *prefix* is indexable from one character, because the index
# pads the start of every string.
_TRIGRAM = 3


def _is_short_query(tokens: Sequence[str]) -> bool:
    """A **Short query** (CONTEXT.md): no token folds to three or more characters.

    Counted in Python, which the fold's own rule forbids for *comparing* titles
    but which only has to approximate a length here. Punctuation, symbols,
    separators and combining marks are not counted — the fold strips the first
    three and `unaccent` drops the last, so a decomposed `ér` is two characters
    either way. What remains is `unaccent`'s expansions (ß → ss, æ → ae), where
    this count comes out *lower* than the fold's: such a token can be treated
    as a prefix search when the fold would make it a substring one. That errs
    toward the indexable plan, never toward a 1.5 s sequential scan.
    """
    return all(
        sum(1 for c in t if unicodedata.category(c)[0] not in ("P", "S", "Z", "M")) < _TRIGRAM
        for t in tokens
    )


def _title_predicate(column, tokens: Sequence[str]) -> ColumnElement[bool]:
    """Whether one title column matches the whole search — the one rule
    `list_shows` and `hydrate_matched_aka` both build, so the list and its badge
    cannot drift apart (they did once, NEU-433).

    Every token must be in *this* column: a match lives entirely in the name or
    entirely in one AKA (NEU-1502 §2.1). The `AND` of plain `LIKE`s is what lets
    Postgres bitmap-AND the trigram index across tokens; `LIKE ALL (ARRAY[…])`
    reads the same and falls back to a sequential scan.

    A short query matches the **start** of the title instead — its tokens run
    together, as the fold runs a title's words together — because a one- or
    two-character substring is a full scan and a prefix is not (§2.2).

    No wildcard escaping: `%` and `_` are punctuation, which the fold strips
    from the token before it becomes a pattern (§2.3).
    """
    title = folded(column)
    if _is_short_query(tokens):
        prefix = folded(literal("".join(tokens), literal_execute=True))
        return title.like(func.concat(prefix, "%"))
    return and_(
        *(
            title.like(func.concat("%", folded(literal(t, literal_execute=True)), "%"))
            for t in tokens
        )
    )


async def list_shows(
    session: AsyncSession,
    filters: ShowFilters,
    sort: str,
    page: int,
    per_page: int,
) -> tuple[list[m.Show], int]:
    if sort not in ALLOWED_SORT_KEYS:
        raise ValueError(f"invalid sort key: {sort}")

    # Tombstoned shows are gone upstream and must not be discoverable — nobody
    # should be able to newly find or add one (ADR-0005). Deliberately scoped to
    # discovery: `get_show_with_seasons` and every /me surface still serve them,
    # so a user already tracking one keeps their list, ratings and history.
    base = select(m.Show).where(m.Show.deleted_upstream_at.is_(None))
    if filters.search:
        # Token-AND against the accent- and punctuation-folded name, or against
        # one folded AKA. Folding both the column and the token lets "shogun"
        # match "Shōgun" and "spiderman" match "Spider-Man", while whitespace
        # tokenization keeps "alien earth" matching "Alien: Earth" and
        # non-Latin titles ("進撃") still match natively.
        usable = _search_tokens(filters.search)
        if not usable:
            # Search was all punctuation/whitespace — match nothing, not everything.
            base = base.where(false())
        else:
            # One semi-join over a UNION of the two sources, not a per-token
            # `name LIKE … OR id IN (aka …)`: Postgres cannot drive the name's
            # trigram index through that OR and folded all 231k names per token
            # (NEU-1502 §2.1). Aliased so the inner `show` is not correlated
            # away against the outer one.
            named = aliased(m.Show)
            matches = union(
                select(named.id).where(_title_predicate(named.name, usable)),
                select(m.ShowAka.show_id).where(_title_predicate(m.ShowAka.title, usable)),
            )
            base = base.where(m.Show.id.in_(matches))
    if filters.status is not None:
        base = base.where(m.Show.status == filters.status)
    if filters.language is not None:
        base = base.where(m.Show.original_language == filters.language)
    if filters.type is not None:
        base = base.where(m.Show.type == filters.type)
    if filters.genres:
        base = base.where(m.Show.id.in_(genre_queries.shows_with_all_genres(filters.genres)))
    if filters.network_ids:
        base = base.where(m.Show.id.in_(_shows_on_networks(filters.network_ids)))

    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()

    stmt = (
        base.order_by(_SORT_EXPRS[sort], m.Show.id.asc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total


async def search_people(
    session: AsyncSession,
    search: str | None,
    page: int,
    per_page: int,
) -> tuple[list[m.Person], int]:
    """Paginated person search — the same query shape as show search, pointed at
    a different table.

    Deliberately a separate entity search rather than a third OR branch in
    `list_shows`: a cast member's name is not a name of the show, and folding
    crew names into the title predicate would make "smith" return most of the
    catalog. `list_shows` is untouched by this.

    Reuses `folded` so the column and each query token normalize under identical
    rules, which matters more for names than for titles — "visnjic" has to reach
    "Goran Višnjić" because nobody types the diacritics. Backed by
    `ix_person_name_folded_trgm` on `catalog.person`.

    Search-only by design: with no usable token there is nothing to match, so
    this returns an empty page rather than the whole table. There is no
    browse-all-people surface, and an unfiltered listing would sort the entire
    table on every request off the back of an index that only covers the folded
    name.
    """
    # Token-AND, same as show search: "zachary levi" matches, "zachary garcia"
    # doesn't. A query that folds to nothing ("--", "") matches nothing — never
    # everything.
    usable = [t for t in (search or "").split() if _strip_punct_space(t)]
    if not usable:
        return [], 0

    base = select(m.Person)
    for token in usable:
        needle = func.concat("%", folded(literal(token, literal_execute=True)), "%")
        base = base.where(folded(m.Person.name).like(needle))

    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()

    stmt = (
        base.order_by(func.lower(m.Person.name).asc(), m.Person.id.asc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total


async def hydrate_matched_aka(
    session: AsyncSession, shows: list[m.Show], search: str | None
) -> dict[int, str | None]:
    """Per-show: which AKA (if any) matched the search?

    Returns a dict mapping show_id → matched_aka (or None when the show's own
    name carries the match, or when there's no search term). Empty dict when
    `shows` is empty or `search` is falsy. Used by the browse list route to
    surface match context to the frontend so users see why a foreign-titled
    show came back for an English query.

    Picks the shortest matching AKA per show — heuristic for "most canonical".
    """
    if not search or not shows:
        return {}

    tokens = _search_tokens(search)
    if not tokens:
        return {}

    show_ids = [s.id for s in shows]

    # Best (shortest) AKA per show that matches the whole search.
    aka_query = select(m.ShowAka.show_id, m.ShowAka.title).where(
        m.ShowAka.show_id.in_(show_ids), _title_predicate(m.ShowAka.title, tokens)
    )
    aka_rows = (await session.execute(aka_query)).all()
    best_by_show: dict[int, str] = {}
    for sid, aname in aka_rows:
        if sid not in best_by_show or len(aname) < len(best_by_show[sid]):
            best_by_show[sid] = aname

    # Which shows matched on their own (folded) name? Determined in SQL so the
    # rule is identical to list_shows — a Python unaccent would diverge on
    # characters like ł/ø that NFKD does not decompose.
    name_query = select(m.Show.id).where(
        m.Show.id.in_(show_ids), _title_predicate(m.Show.name, tokens)
    )
    name_matched_ids = set((await session.execute(name_query)).scalars().all())

    result: dict[int, str | None] = {}
    for show in shows:
        if show.id in name_matched_ids:
            result[show.id] = None
        else:
            result[show.id] = best_by_show.get(show.id)
    return result


async def hydrate_show_refs(
    session: AsyncSession, shows: list[m.Show]
) -> tuple[dict[int, list[str]], dict[int, m.Network]]:
    """Genre names and the primary network for a page of shows, in two queries.

    One query fewer than the `tvmaze` original, because `web_channel` merged into
    `network`. Both halves are the same functions the single-show detail route
    calls, handed a list instead of an id, so neither rule exists twice.
    """
    if not shows:
        return {}, {}

    show_ids = [s.id for s in shows]
    return (
        await genre_queries.genres_by_show(session, show_ids),
        await primary_networks(session, show_ids),
    )


async def hydrate_my_ratings(
    session: AsyncSession, *, viewer_id: UUID, show_ids: list[int]
) -> dict[int, float]:
    """Per-show: the viewer's own rating (stars) if any. Empty dict when no
    inputs or no viewer ratings. Stars come back as float for JSON-friendliness."""
    return await show_rating_repo.get_many_for_user(session, user_id=viewer_id, show_ids=show_ids)


async def hydrate_my_episode_ratings(
    session: AsyncSession, *, viewer_id: UUID, episode_ids: list[int]
) -> dict[int, float]:
    """Per-episode: the viewer's own rating (stars) if any."""
    return await episode_rating_repo.get_many_for_user(
        session, user_id=viewer_id, episode_ids=episode_ids
    )

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

`GET /shows` still issues four queries for a page of any size: count, page,
genres-by-show, networks-by-show. It was five before — dropping `web_channel`
dropped one.
"""

import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, false, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

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

# Most recent already-aired episode airdate per show. Correlated subquery so it can
# participate in ORDER BY without a join that would multiply rows.
_LAST_AIRED = (
    select(func.max(m.Episode.air_date))
    .where(m.Episode.show_id == m.Show.id)
    .where(m.Episode.air_date <= func.current_date())
    .correlate(m.Show)
    .scalar_subquery()
)

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
    "last_aired": _LAST_AIRED.asc().nulls_last(),
    "-last_aired": _LAST_AIRED.desc().nulls_last(),
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


async def list_show_cast(session: AsyncSession, show_id: int) -> list[tuple[m.Person, m.Character]]:
    """Cast credits for one show, most-present first.

    Covered by ix_show_cast_show_id_episode_count. Single-show route, so unlike
    `hydrate_show_refs` there is no N+1 to batch away.

    **The join to `character` is inner, which drops a credit whose character TMDB
    left blank** — measured at 1 of 7,629 sampled roles. `catalog.show_cast`
    made `character_id` nullable so one blank cannot abort a multi-hour ingest;
    `CastMemberOut.character` is required, and widening it is a contract change
    for a row that would render as "person as (nothing)" anyway.
    """
    stmt = (
        select(m.Person, m.Character)
        .join(m.ShowCast, m.ShowCast.person_id == m.Person.id)
        .join(m.Character, m.Character.id == m.ShowCast.character_id)
        .where(m.ShowCast.show_id == show_id)
        .order_by(
            _CREDIT_COUNT_DESC,
            func.coalesce(m.ShowCast.billing_order, 2**31 - 1).asc(),
            m.ShowCast.id.asc(),
        )
    )
    result = await session.execute(stmt)
    return list(result.tuples().all())


async def list_show_crew(session: AsyncSession, show_id: int) -> list[tuple[m.Person, m.CrewRole]]:
    """Crew credits for one show, most-present first. Covered by
    ix_show_crew_show_id_episode_count."""
    stmt = (
        select(m.Person, m.CrewRole)
        .join(m.ShowCrew, m.ShowCrew.person_id == m.Person.id)
        .join(m.CrewRole, m.CrewRole.id == m.ShowCrew.role_id)
        .where(m.ShowCrew.show_id == show_id)
        .order_by(_CREW_COUNT_DESC, m.CrewRole.job.asc(), m.ShowCrew.id.asc())
    )
    result = await session.execute(stmt)
    return list(result.tuples().all())


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
            func.coalesce(m.EpisodeGuestCast.credit_order, 2**31 - 1).asc(),
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
) -> list[tuple[m.Show, m.Character]]:
    """Regular cast credits for one person. Covered by ix_show_cast_person_id."""
    stmt = (
        select(m.Show, m.Character)
        .join(m.ShowCast, m.ShowCast.show_id == m.Show.id)
        .join(m.Character, m.Character.id == m.ShowCast.character_id)
        .where(m.ShowCast.person_id == person_id)
        # `aggregate_credits` nests one row per role, so one person legitimately
        # holds several credits on the same show. Break the tie on the credit
        # itself, or the order within a show is nondeterministic across requests.
        .order_by(*_CREDIT_SHOW_ORDER, _CREDIT_COUNT_DESC, m.ShowCast.id.asc())
    )
    return list((await session.execute(stmt)).tuples().all())


async def list_person_crew_credits(
    session: AsyncSession, person_id: int
) -> list[tuple[m.Show, m.CrewRole]]:
    """Crew credits for one person. Covered by ix_show_crew_person_id."""
    stmt = (
        select(m.Show, m.CrewRole)
        .join(m.ShowCrew, m.ShowCrew.show_id == m.Show.id)
        .join(m.CrewRole, m.CrewRole.id == m.ShowCrew.role_id)
        .where(m.ShowCrew.person_id == person_id)
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
        # Token-based AND match against an accent- and punctuation-folded form of
        # the show name OR any of its AKAs. Folding both the column and the token
        # lets "shogun" match "Shōgun" and "spiderman" match "Spider-Man", while
        # whitespace tokenization keeps "alien earth" matching "Alien: Earth" and
        # non-Latin titles ("進撃") still match natively.
        usable = [t for t in filters.search.split() if _strip_punct_space(t)]
        if not usable:
            # Search was all punctuation/whitespace — match nothing, not everything.
            base = base.where(false())
        for token in usable:
            needle = func.concat("%", folded(literal(token, literal_execute=True)), "%")
            aka_subq = select(m.ShowAka.show_id).where(folded(m.ShowAka.title).like(needle))
            base = base.where(or_(folded(m.Show.name).like(needle), m.Show.id.in_(aka_subq)))
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

    tokens = [t for t in search.split() if _strip_punct_space(t)]
    if not tokens:
        return {}

    show_ids = [s.id for s in shows]

    # Best (shortest) AKA per show that matches every folded token.
    aka_query = select(m.ShowAka.show_id, m.ShowAka.title).where(m.ShowAka.show_id.in_(show_ids))
    for token in tokens:
        needle = func.concat("%", folded(literal(token, literal_execute=True)), "%")
        aka_query = aka_query.where(folded(m.ShowAka.title).like(needle))
    aka_rows = (await session.execute(aka_query)).all()
    best_by_show: dict[int, str] = {}
    for sid, aname in aka_rows:
        if sid not in best_by_show or len(aname) < len(best_by_show[sid]):
            best_by_show[sid] = aname

    # Which shows matched on their own (folded) name? Determined in SQL so the
    # rule is identical to list_shows — a Python unaccent would diverge on
    # characters like ł/ø that NFKD does not decompose.
    name_query = select(m.Show.id).where(m.Show.id.in_(show_ids))
    for token in tokens:
        needle = func.concat("%", folded(literal(token, literal_execute=True)), "%")
        name_query = name_query.where(folded(m.Show.name).like(needle))
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

import logging
from datetime import UTC, datetime

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze import models as m
from tvbf.tvmaze.api_payloads import (
    TVMazeAka,
    TVMazeCastEntry,
    TVMazeCharacter,
    TVMazeCrewEntry,
    TVMazeEpisode,
    TVMazeNetwork,
    TVMazePerson,
    TVMazeSeason,
    TVMazeSeasonEpisode,
    TVMazeShow,
)

log = logging.getLogger(__name__)


async def upsert_network(session: AsyncSession, net: TVMazeNetwork | None) -> int | None:
    if net is None:
        return None
    stmt = (
        insert(m.Network)
        .values(
            id=net.id,
            name=net.name,
            country_code=net.country_code,
            country_name=net.country_name,
            timezone=net.timezone,
        )
        .on_conflict_do_update(
            index_elements=[m.Network.id],
            set_={
                "name": net.name,
                "country_code": net.country_code,
                "country_name": net.country_name,
                "timezone": net.timezone,
            },
        )
    )
    await session.execute(stmt)
    return net.id


async def upsert_web_channel(session: AsyncSession, wc: TVMazeNetwork | None) -> int | None:
    if wc is None:
        return None
    stmt = (
        insert(m.WebChannel)
        .values(
            id=wc.id,
            name=wc.name,
            country_code=wc.country_code,
            country_name=wc.country_name,
            timezone=wc.timezone,
        )
        .on_conflict_do_update(
            index_elements=[m.WebChannel.id],
            set_={
                "name": wc.name,
                "country_code": wc.country_code,
                "country_name": wc.country_name,
                "timezone": wc.timezone,
            },
        )
    )
    await session.execute(stmt)
    return wc.id


async def upsert_genre_by_name(session: AsyncSession, name: str) -> int:
    existing = (
        await session.execute(select(m.Genre.id).where(m.Genre.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    stmt = (
        insert(m.Genre)
        .values(name=name)
        .on_conflict_do_nothing(index_elements=[m.Genre.name])
        .returning(m.Genre.id)
    )
    result = (await session.execute(stmt)).scalar_one_or_none()
    if result is not None:
        return result
    return (await session.execute(select(m.Genre.id).where(m.Genre.name == name))).scalar_one()


async def upsert_season(session: AsyncSession, show_id: int, season: TVMazeSeason) -> int:
    network_id = await upsert_network(session, season.network)
    web_channel_id = await upsert_web_channel(session, season.webChannel)
    values = {
        "id": season.id,
        "show_id": show_id,
        "number": season.number,
        "name": season.name,
        "episode_order": season.episodeOrder,
        "premiere_date": season.premiereDate,
        "end_date": season.endDate,
        "network_id": network_id,
        "web_channel_id": web_channel_id,
        "image_medium": season.image.medium if season.image else None,
        "image_original": season.image.original if season.image else None,
        "summary": season.summary,
    }
    stmt = (
        insert(m.Season)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[m.Season.id],
            set_={k: v for k, v in values.items() if k not in ("id", "show_id")},
        )
    )
    await session.execute(stmt)
    return season.id


async def upsert_show(session: AsyncSession, show: TVMazeShow) -> int:
    network_id = await upsert_network(session, show.network)
    web_channel_id = await upsert_web_channel(session, show.webChannel)

    values = {
        "id": show.id,
        "name": show.name,
        "type": show.type,
        "language": show.language,
        "status": show.status,
        "runtime": show.runtime,
        "premiered": show.premiered,
        "ended": show.ended,
        "official_site": show.officialSite,
        "summary": show.summary,
        "image_medium": show.image.medium if show.image else None,
        "image_original": show.image.original if show.image else None,
        "externals_imdb": show.externals.imdb if show.externals else None,
        "externals_tvdb": show.externals.tvdb if show.externals else None,
        "externals_tvrage": show.externals.tvrage if show.externals else None,
        "network_id": network_id,
        "web_channel_id": web_channel_id,
        "tvmaze_updated": show.updated,
        "rating_average": show.rating_average,
    }
    stmt = (
        insert(m.Show)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[m.Show.id],
            set_={k: v for k, v in values.items() if k != "id"},
        )
    )
    await session.execute(stmt)

    await session.execute(delete(m.ShowGenre).where(m.ShowGenre.show_id == show.id))
    for name in show.genres:
        gid = await upsert_genre_by_name(session, name)
        await session.execute(
            insert(m.ShowGenre).values(show_id=show.id, genre_id=gid).on_conflict_do_nothing()
        )
    return show.id


# Postgres caps bind parameters per query at 32767. Episode has 12 bound columns,
# so we batch at 1000 rows (12000 params) to stay well under the limit. Shows
# with >2730 episodes (soaps, daily talk shows, news) would otherwise fail.
_EPISODE_BATCH_SIZE = 1000


async def upsert_episodes(
    session: AsyncSession, show_id: int, episodes: list[TVMazeEpisode]
) -> None:
    if not episodes:
        return

    season_rows = (
        await session.execute(
            select(m.Season.id, m.Season.number).where(m.Season.show_id == show_id)
        )
    ).all()
    season_by_number = {r.number: r.id for r in season_rows}

    values_list = [
        {
            "id": ep.id,
            "show_id": show_id,
            "season_id": season_by_number.get(ep.season),
            "season": ep.season,
            "number": ep.number,
            "name": ep.name,
            "airdate": ep.airdate,
            "airtime": ep.airtime,
            "runtime": ep.runtime,
            "summary": ep.summary,
            "image_medium": ep.image.medium if ep.image else None,
            "image_original": ep.image.original if ep.image else None,
            "rating_average": ep.rating_average,
        }
        for ep in episodes
    ]

    for start in range(0, len(values_list), _EPISODE_BATCH_SIZE):
        chunk = values_list[start : start + _EPISODE_BATCH_SIZE]
        stmt = insert(m.Episode).values(chunk)
        update_cols = {c: getattr(stmt.excluded, c) for c in chunk[0] if c != "id"}
        stmt = stmt.on_conflict_do_update(index_elements=[m.Episode.id], set_=update_cols)
        await session.execute(stmt)


async def prune_missing_seasons(
    session: AsyncSession, *, show_id: int, payload_seasons: list[TVMazeSeason]
) -> int:
    """Delete this show's seasons that the payload does not name. Returns the count.

    The show fetch is authoritative for the show's season set (ADR-0004) — the
    same argument ADR-0003 makes for a season response owning its credits, one
    level up. Without this the mirror never forgets a season TV Maze created and
    later deleted, and the episode-credits pass 404s on it forever.

    **Diff by id, never by `(show_id, number)`.** Upstream sometimes carries two
    seasons with the same number for one show — the quirk that keeps a UNIQUE
    constraint off `(show_id, number)` — and deduplicates them later. The
    correct outcome there is "delete the id that is gone, keep the id that
    remains", which only an id-set diff expresses.

    An empty `payload_seasons` under an opted-in caller is an authoritative
    zero and deletes every season of the show. That is why the opt-in lives in
    the caller and not in an `if not seasons: skip` guard here — see
    `upsert_show_payload`.
    """
    stmt = delete(m.Season).where(m.Season.show_id == show_id)
    if payload_ids := {s.id for s in payload_seasons}:
        stmt = stmt.where(m.Season.id.not_in(payload_ids))
    result = await session.execute(stmt)
    pruned: int = result.rowcount  # type: ignore[attr-defined]
    if pruned:
        # Rare by construction, so this is signal rather than noise: it is the
        # only record of the cleanup actually happening.
        log.info("show %d: pruned %d season(s) deleted upstream", show_id, pruned)
    return pruned


async def upsert_show_payload(
    session: AsyncSession,
    show: TVMazeShow,
    *,
    episodes: list[TVMazeEpisode] | None = None,
    prune_seasons: bool = False,
) -> int:
    """Upsert a complete show payload (show + its genres + seasons + episodes) in order.

    `episodes` supplements the embedded episode list. `embed[]=episodes`
    silently omits specials (episodes with a null `number`), so callers pass the
    `/shows/{id}/episodes?specials=1` result here instead. The two lists are
    merged on episode id with `episodes` winning, which means a caller that
    skips the embed entirely can pass the full list, and a caller whose specials
    fetch failed still writes whatever the embed carried.

    `prune_seasons` makes this payload authoritative for the show's season set,
    deleting mirrored seasons it does not name. It is **opt-in per caller and
    must stay that way**: `TVMazeEmbedded.seasons` defaults to `[]`, so this
    function cannot tell "no seasons upstream" from "the caller didn't request
    `embed[]=seasons`" — and `get_show` explicitly supports `embed=[]`. Pass
    True only when the fetch actually requested the seasons embed. An implicit
    `if not seasons: skip` is *not* an acceptable substitute: a show
    legitimately having zero seasons exists, and that guard conflates it with
    the missing-embed case, reintroducing the leak for exactly the shows where
    pruning matters.

    Order is load-bearing: the prune runs **between** the season upsert and the
    episode upsert. `upsert_episodes` resolves each episode's `season_id` from a
    live query over the show's seasons, so pruning first means that map holds
    only survivors — and in the duplicate-number case the survivor wins the
    lookup and the phantom's episodes are re-pointed to it. Prune afterwards and
    those episodes would instead be bound to a row about to disappear, and get
    their `season_id` nulled by the FK's ON DELETE SET NULL.

    Caller owns transaction boundaries (commit/rollback).
    """
    await upsert_show(session, show)
    for season in show.embedded.seasons:
        await upsert_season(session, show_id=show.id, season=season)
    if prune_seasons:
        await prune_missing_seasons(session, show_id=show.id, payload_seasons=show.embedded.seasons)
    merged: dict[int, TVMazeEpisode] = {ep.id: ep for ep in show.embedded.episodes}
    if episodes is not None:
        merged.update({ep.id: ep for ep in episodes})
    await upsert_episodes(session, show_id=show.id, episodes=list(merged.values()))
    return show.id


async def upsert_akas(session: AsyncSession, *, show_id: int, akas: list[TVMazeAka]) -> None:
    """Replace this show's AKA rows. Caller owns the transaction.

    AKA lists are short (typically <20 entries) and TVMaze can both add and
    remove entries between syncs, so a delete-then-insert is simpler and more
    correct than per-row upserts.
    """
    await session.execute(delete(m.ShowAka).where(m.ShowAka.show_id == show_id))
    if not akas:
        return
    rows = [
        {
            "show_id": show_id,
            "name": a.name,
            "country_code": a.country_code,
            "country_name": a.country_name,
            "language": a.language,
        }
        for a in akas
    ]
    await session.execute(insert(m.ShowAka).values(rows))


async def mark_akas_synced(session: AsyncSession, *, show_id: int) -> None:
    """Set the show's akas_synced_at to now()."""
    await session.execute(
        update(m.Show).where(m.Show.id == show_id).values(akas_synced_at=datetime.now(UTC))
    )


async def mark_ratings_synced(session: AsyncSession, *, show_id: int) -> None:
    """Set the show's ratings_synced_at to now()."""
    await session.execute(
        update(m.Show).where(m.Show.id == show_id).values(ratings_synced_at=datetime.now(UTC))
    )


# Postgres caps bind parameters per query at 32767 — the same ceiling that
# forced _EPISODE_BATCH_SIZE. show_cast binds 6 columns per row and show_crew
# 4, but persons and characters upsert in the same transaction, so the credit
# path keeps its own 1000-row batch rather than a tighter per-table bound.
# The Simpsons (1,420 cast rows, 533 crew) exceeds a single batch.
_CREDIT_BATCH_SIZE = 1000


async def upsert_persons(session: AsyncSession, people: list[TVMazePerson]) -> None:
    """Upsert person rows by upstream id.

    Writes the full attribute set from whatever payload it is handed, and the
    person objects embedded in show cast/crew and season guest cast/crew are
    byte-identical to `/people/{id}` — which is what lets every person arrive
    complete off the show axis alone (ADR-0003).
    """
    if not people:
        return
    # Last write wins within one payload: the same person can appear twice.
    seen: dict[int, TVMazePerson] = {p.id: p for p in people}
    rows = [
        {
            "id": p.id,
            "name": p.name,
            "country_code": p.country_code,
            "country_name": p.country_name,
            "timezone": p.timezone,
            "birthday": p.birthday,
            "deathday": p.deathday,
            "gender": p.gender,
            "image_medium": p.image.medium if p.image else None,
            "image_original": p.image.original if p.image else None,
            "tvmaze_updated": p.updated,
        }
        for p in seen.values()
    ]
    for start in range(0, len(rows), _CREDIT_BATCH_SIZE):
        chunk = rows[start : start + _CREDIT_BATCH_SIZE]
        stmt = insert(m.Person).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[m.Person.id],
            set_={c: getattr(stmt.excluded, c) for c in chunk[0] if c != "id"},
        )
        await session.execute(stmt)


async def upsert_characters(session: AsyncSession, characters: list[TVMazeCharacter]) -> None:
    if not characters:
        return
    seen: dict[int, TVMazeCharacter] = {c.id: c for c in characters}
    rows = [
        {
            "id": c.id,
            "name": c.name,
            "image_medium": c.image.medium if c.image else None,
            "image_original": c.image.original if c.image else None,
        }
        for c in seen.values()
    ]
    for start in range(0, len(rows), _CREDIT_BATCH_SIZE):
        chunk = rows[start : start + _CREDIT_BATCH_SIZE]
        stmt = insert(m.Character).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[m.Character.id],
            set_={c: getattr(stmt.excluded, c) for c in chunk[0] if c != "id"},
        )
        await session.execute(stmt)


async def _resolve_interned_name(
    session: AsyncSession, model: type[m.CrewRole] | type[m.EpisodeCrewRole], name: str
) -> int:
    """Resolve-or-insert a row in a name-interning lookup. Mirrors upsert_genre_by_name.

    Upstream sends both crew vocabularies as free text with no id, exactly like
    genre.

    Deliberately not cached across shows, despite the spec suggesting a
    run-lifetime cache: credits are written in per-show (or per-season)
    transactions and a failure there rolls back without aborting the run, so a
    cached id for a role inserted inside a transaction that later rolled back
    would produce an FK violation on `role_id` for every subsequent writer using
    it. Roles per payload are of the same order as genres, so the lookup cost
    matches the pattern this mirrors.
    """
    existing = (
        await session.execute(select(model.id).where(model.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    stmt = (
        insert(model)
        .values(name=name)
        .on_conflict_do_nothing(index_elements=[model.name])
        .returning(model.id)
    )
    result = (await session.execute(stmt)).scalar_one_or_none()
    if result is not None:
        return result
    return (await session.execute(select(model.id).where(model.name == name))).scalar_one()


async def resolve_crew_role(session: AsyncSession, name: str) -> int:
    """Resolve-or-insert a show-level crew role id ("Executive Producer")."""
    return await _resolve_interned_name(session, m.CrewRole, name)


async def resolve_episode_crew_role(session: AsyncSession, name: str) -> int:
    """Resolve-or-insert an episode-level crew role id ("Director", "Teleplay").

    A separate lookup from `crew_role`, not an oversight: the two vocabularies
    are disjoint. None of Writer / Director / Story / Teleplay appear among
    `crew_role`'s 233 production-function names (ADR-0003).
    """
    return await _resolve_interned_name(session, m.EpisodeCrewRole, name)


async def upsert_show_cast(
    session: AsyncSession, *, show_id: int, entries: list[TVMazeCastEntry]
) -> None:
    """Replace this show's cast rows. Caller owns the transaction.

    Delete-then-insert, same reasoning as upsert_akas: TV Maze both adds and
    removes entries, and there is no upstream row id to upsert against.
    Insertion follows the upstream array, which is billing order.
    """
    await session.execute(delete(m.ShowCast).where(m.ShowCast.show_id == show_id))
    if not entries:
        return

    await upsert_persons(session, [e.person for e in entries])
    await upsert_characters(session, [e.character for e in entries])

    rows: list[dict[str, object]] = []
    seen: set[tuple[int, int]] = set()
    for e in entries:
        key = (e.person.id, e.character.id)
        if key in seen:
            continue  # the same credit sent twice upstream is one credit
        seen.add(key)
        rows.append(
            {
                "show_id": show_id,
                "person_id": e.person.id,
                "character_id": e.character.id,
                "is_self": e.is_self,
                "is_voice": e.is_voice,
                "sort_order": len(rows),
            }
        )
    for start in range(0, len(rows), _CREDIT_BATCH_SIZE):
        await session.execute(insert(m.ShowCast).values(rows[start : start + _CREDIT_BATCH_SIZE]))


async def upsert_show_crew(
    session: AsyncSession, *, show_id: int, entries: list[TVMazeCrewEntry]
) -> None:
    """Replace this show's crew rows. Caller owns the transaction."""
    await session.execute(delete(m.ShowCrew).where(m.ShowCrew.show_id == show_id))
    if not entries:
        return

    await upsert_persons(session, [e.person for e in entries])
    role_ids = {t: await resolve_crew_role(session, t) for t in {e.type for e in entries}}

    rows: list[dict[str, object]] = []
    seen: set[tuple[int, int]] = set()
    for e in entries:
        key = (e.person.id, role_ids[e.type])
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "show_id": show_id,
                "person_id": e.person.id,
                "role_id": role_ids[e.type],
                "sort_order": len(rows),
            }
        )
    for start in range(0, len(rows), _CREDIT_BATCH_SIZE):
        await session.execute(insert(m.ShowCrew).values(rows[start : start + _CREDIT_BATCH_SIZE]))


async def mark_credits_synced(session: AsyncSession, *, show_id: int) -> None:
    """Set the show's credits_synced_at to now()."""
    await session.execute(
        update(m.Show).where(m.Show.id == show_id).values(credits_synced_at=datetime.now(UTC))
    )


async def upsert_season_credits(
    session: AsyncSession, *, season_id: int, episodes: list[TVMazeSeasonEpisode]
) -> None:
    """Replace every episode credit in one season. Caller owns the transaction.

    A season response is authoritative for every credit on every episode it
    contains (ADR-0003), so this is a delete-and-replace of the whole season:
    idempotent, and correct when a credit is removed upstream. That property is
    what the person axis could never have — one episode's guest cast belongs to
    many people, so the retired per-person writer had to delete by person.

    Writes credits only. Show, season and episode rows stay owned by the show
    fetch, which must therefore have committed first: credits FK to `episode.id`.

    A season response may name an episode we don't mirror, because the show
    gained one upstream since our last show fetch. Those credits are skipped and
    logged, and the watermark is still stamped. This deliberately reverses the
    person axis's stance of raising on the FK: there, a missing episode meant a
    broken prerequisite worth stopping for; at season grain it just means our
    mirror is older than upstream, and it self-heals, because a new episode
    marks its show updated and the daily then rewrites the show and its seasons.
    Failing the season instead would strand it on every retry until the show
    happened to be refetched — which, during the backfill with the daily cron
    disabled, is never.
    """
    response_ids = [ep.id for ep in episodes]
    # Delete by the season's own episodes AND by the ids this response names.
    # The second arm matters when an episode's `season_id` is NULL (its season
    # was never matched by number): the season arm alone would leave that
    # episode's stale rows behind, and the insert below would then collide with
    # the three-part unique key.
    scoped_episodes = select(m.Episode.id).where(
        or_(m.Episode.season_id == season_id, m.Episode.id.in_(response_ids))
    )
    await session.execute(
        delete(m.EpisodeGuestCast).where(m.EpisodeGuestCast.episode_id.in_(scoped_episodes))
    )
    await session.execute(
        delete(m.EpisodeCrew).where(m.EpisodeCrew.episode_id.in_(scoped_episodes))
    )

    known = set(
        (await session.execute(select(m.Episode.id).where(m.Episode.id.in_(response_ids))))
        .scalars()
        .all()
    )
    if missing := [eid for eid in response_ids if eid not in known]:
        log.info(
            "season %d: skipping credits for %d episode(s) we don't mirror yet: %s",
            season_id,
            len(missing),
            missing,
        )
    present = [ep for ep in episodes if ep.id in known]

    cast_entries = [e for ep in present for e in ep.embedded.guestcast]
    crew_entries = [e for ep in present for e in ep.embedded.guestcrew]

    await upsert_persons(
        session, [e.person for e in cast_entries] + [e.person for e in crew_entries]
    )
    await upsert_characters(session, [e.character for e in cast_entries])
    role_ids = {
        t: await resolve_episode_crew_role(session, t) for t in {e.type for e in crew_entries}
    }

    cast_rows: list[dict[str, object]] = []
    crew_rows: list[dict[str, object]] = []
    for ep in present:
        # sort_order counts within one episode — the episode's own credit
        # sequence, which is exactly what the person axis could not express (it
        # wrote the index within that person's credit list, so an episode's
        # guest cast came out ordered by how many other gigs each actor had).
        rank = 0
        seen_cast: set[tuple[int, int]] = set()
        for e in ep.embedded.guestcast:
            # Three-part dedup, NOT (episode_id, character_id): two people share
            # one character on 1.6% of episodes, and the narrower key would drop
            # one of them.
            key = (e.person.id, e.character.id)
            if key in seen_cast:
                continue  # the same credit sent twice upstream is one credit
            seen_cast.add(key)
            cast_rows.append(
                {
                    "episode_id": ep.id,
                    "person_id": e.person.id,
                    "character_id": e.character.id,
                    "is_self": e.is_self,
                    "is_voice": e.is_voice,
                    "sort_order": rank,
                }
            )
            rank += 1

        rank = 0
        seen_crew: set[tuple[int, int]] = set()
        for e in ep.embedded.guestcrew:
            key = (e.person.id, role_ids[e.type])
            if key in seen_crew:
                continue
            seen_crew.add(key)
            crew_rows.append(
                {
                    "episode_id": ep.id,
                    "person_id": e.person.id,
                    "role_id": role_ids[e.type],
                    "sort_order": rank,
                }
            )
            rank += 1

    for start in range(0, len(cast_rows), _CREDIT_BATCH_SIZE):
        await session.execute(
            insert(m.EpisodeGuestCast).values(cast_rows[start : start + _CREDIT_BATCH_SIZE])
        )
    for start in range(0, len(crew_rows), _CREDIT_BATCH_SIZE):
        await session.execute(
            insert(m.EpisodeCrew).values(crew_rows[start : start + _CREDIT_BATCH_SIZE])
        )

    await mark_season_credits_synced(session, season_id=season_id)


async def mark_season_credits_synced(session: AsyncSession, *, season_id: int) -> None:
    """Set the season's credits_synced_at to now() — the episode-credit watermark.

    Same column name as `show.credits_synced_at`, different grain: that one is
    show-level cast and crew, this one is episode-level credits. Don't conflate.
    """
    await session.execute(
        update(m.Season).where(m.Season.id == season_id).values(credits_synced_at=datetime.now(UTC))
    )


async def clear_season_credits_synced(session: AsyncSession, *, season_id: int) -> None:
    """Put a season back in the backfill's todo list.

    A season that fails to refresh must not keep the timestamp from its last
    successful one: the backfill selects on `credits_synced_at IS NULL`, and the
    daily's cursor has already advanced past the show, so a stale stamp means
    nothing ever retries it.
    """
    await session.execute(
        update(m.Season).where(m.Season.id == season_id).values(credits_synced_at=None)
    )

from datetime import UTC, datetime

from sqlalchemy import delete, select, update
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
    TVMazeShow,
)


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


async def upsert_show_payload(session: AsyncSession, show: TVMazeShow) -> int:
    """Upsert a complete show payload (show + its genres + seasons + episodes) in order.

    Caller owns transaction boundaries (commit/rollback).
    """
    await upsert_show(session, show)
    for season in show.embedded.seasons:
        await upsert_season(session, show_id=show.id, season=season)
    await upsert_episodes(session, show_id=show.id, episodes=show.embedded.episodes)
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

    Never touches credits_synced_at — a person created here still needs the
    person axis (pass C) to fetch their own credits.
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


async def resolve_crew_role(session: AsyncSession, name: str) -> int:
    """Resolve-or-insert a crew role id. Mirrors upsert_genre_by_name.

    Upstream sends crew type as free text with no id, exactly like genre.

    Deliberately not cached across shows, despite the spec suggesting a
    run-lifetime cache: credits are written in per-show transactions and a
    per-show failure rolls back without aborting the run, so a cached id for a
    role inserted inside a transaction that later rolled back would produce an
    FK violation on show_crew.role_id for every subsequent show using it.
    Crew types per show are of the same order as genres, so the lookup cost
    matches the pattern this mirrors.
    """
    existing = (
        await session.execute(select(m.CrewRole.id).where(m.CrewRole.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    stmt = (
        insert(m.CrewRole)
        .values(name=name)
        .on_conflict_do_nothing(index_elements=[m.CrewRole.name])
        .returning(m.CrewRole.id)
    )
    result = (await session.execute(stmt)).scalar_one_or_none()
    if result is not None:
        return result
    return (
        await session.execute(select(m.CrewRole.id).where(m.CrewRole.name == name))
    ).scalar_one()


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

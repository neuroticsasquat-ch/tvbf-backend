"""Season-grain episode credit upsert — the refresh grain ADR-0003 moved to.

A season response is authoritative for every credit on every episode it
contains, so the write is a delete-and-replace of the whole season. That is the
property the retired per-person writer never had, and everything below is a
consequence of it: idempotence, removal-tracking, and a three-part dedup key
that the per-person key would have silently narrowed.
"""

import pytest
from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.api_payloads import TVMazeSeasonEpisode
from tvbf.tvmaze.upsert import resolve_crew_role, upsert_season_credits

SEASON_ID = 100
OTHER_SEASON_ID = 101


def person(person_id: int) -> dict:
    return {"id": person_id, "name": f"Person {person_id}", "updated": 1700000000}


def character(character_id: int) -> dict:
    return {"id": character_id, "name": f"Character {character_id}"}


def episode(
    episode_id: int,
    *,
    cast: list[tuple[int, int]] | None = None,
    crew: list[tuple[int, str]] | None = None,
) -> TVMazeSeasonEpisode:
    """One season-response episode. `cast` is (person_id, character_id) pairs,
    `crew` is (person_id, role) pairs — both in upstream credit order."""
    return TVMazeSeasonEpisode.model_validate(
        {
            "id": episode_id,
            "season": 1,
            "number": 1,
            "_embedded": {
                "guestcast": [
                    {"person": person(pid), "character": character(cid), "self": False}
                    for pid, cid in (cast or [])
                ],
                "guestcrew": [
                    {"person": person(pid), "guestCrewType": role} for pid, role in (crew or [])
                ],
            },
        }
    )


@pytest.fixture
async def catalog(session):
    """One show, two seasons, three episodes — the FK targets credits need."""
    session.add(m.Show(id=1, name="S", tvmaze_updated=1))
    await session.flush()
    session.add_all(
        [
            m.Season(id=SEASON_ID, show_id=1, number=1),
            m.Season(id=OTHER_SEASON_ID, show_id=1, number=2),
        ]
    )
    await session.flush()
    session.add_all(
        [
            m.Episode(id=500, show_id=1, season_id=SEASON_ID, season=1, number=1, name="S1E1"),
            m.Episode(id=501, show_id=1, season_id=SEASON_ID, season=1, number=2, name="S1E2"),
            m.Episode(
                id=600, show_id=1, season_id=OTHER_SEASON_ID, season=2, number=1, name="S2E1"
            ),
        ]
    )
    await session.commit()


async def _cast_rows(session, episode_id: int) -> list[m.EpisodeGuestCast]:
    return list(
        (
            await session.execute(
                select(m.EpisodeGuestCast)
                .where(m.EpisodeGuestCast.episode_id == episode_id)
                .order_by(m.EpisodeGuestCast.sort_order)
            )
        )
        .scalars()
        .all()
    )


async def _crew_rows(session, episode_id: int) -> list[m.EpisodeCrew]:
    return list(
        (
            await session.execute(
                select(m.EpisodeCrew)
                .where(m.EpisodeCrew.episode_id == episode_id)
                .order_by(m.EpisodeCrew.sort_order)
            )
        )
        .scalars()
        .all()
    )


async def _season(session, season_id: int) -> m.Season:
    return (
        await session.execute(
            select(m.Season)
            .where(m.Season.id == season_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_writes_both_credit_sets_in_upstream_order(session, catalog):
    await upsert_season_credits(
        session,
        season_id=SEASON_ID,
        episodes=[
            episode(500, cast=[(10, 900), (11, 901)], crew=[(12, "Director"), (13, "Writer")])
        ],
    )
    await session.commit()

    cast = await _cast_rows(session, 500)
    assert [(r.person_id, r.character_id) for r in cast] == [(10, 900), (11, 901)]
    assert [r.sort_order for r in cast] == [0, 1]

    crew = await _crew_rows(session, 500)
    assert [r.sort_order for r in crew] == [0, 1]
    roles = {
        r.id: r.name for r in (await session.execute(select(m.EpisodeCrewRole))).scalars().all()
    }
    assert [roles[r.role_id] for r in crew] == ["Director", "Writer"]


async def test_sort_order_restarts_at_each_episode(session, catalog):
    """It is the credit's index within its own episode. The person axis wrote
    the index within that person's credit list, which ordered an episode's
    guest cast by how many other guest gigs each actor had."""
    await upsert_season_credits(
        session,
        season_id=SEASON_ID,
        episodes=[
            episode(500, cast=[(10, 900), (11, 901)]),
            episode(501, cast=[(12, 902), (13, 903)]),
        ],
    )
    await session.commit()

    assert [r.sort_order for r in await _cast_rows(session, 500)] == [0, 1]
    assert [r.sort_order for r in await _cast_rows(session, 501)] == [0, 1]


async def test_two_people_sharing_one_character_both_survive(session, catalog):
    """The dedup key has to be (episode_id, person_id, character_id). The
    per-person writer's (episode_id, character_id) is correct at its own grain
    but would drop one of these — 17 of 1,043 sampled episodes have it."""
    await upsert_season_credits(
        session, season_id=SEASON_ID, episodes=[episode(500, cast=[(10, 900), (11, 900)])]
    )
    await session.commit()

    assert [(r.person_id, r.character_id) for r in await _cast_rows(session, 500)] == [
        (10, 900),
        (11, 900),
    ]


async def test_one_person_in_two_crew_roles_survives_both(session, catalog):
    """36 of 1,043 sampled episodes credit one person in more than one role —
    a writer-director being the ordinary case."""
    await upsert_season_credits(
        session,
        season_id=SEASON_ID,
        episodes=[episode(500, crew=[(12, "Director"), (12, "Writer")])],
    )
    await session.commit()

    rows = await _crew_rows(session, 500)
    assert len(rows) == 2
    assert {r.person_id for r in rows} == {12}
    assert len({r.role_id for r in rows}) == 2


async def test_the_same_credit_sent_twice_collapses_to_one_row(session, catalog):
    await upsert_season_credits(
        session,
        season_id=SEASON_ID,
        episodes=[
            episode(500, cast=[(10, 900), (10, 900)], crew=[(12, "Director"), (12, "Director")])
        ],
    )
    await session.commit()

    assert len(await _cast_rows(session, 500)) == 1
    assert len(await _crew_rows(session, 500)) == 1


async def test_replaying_the_same_response_is_a_no_op(session, catalog):
    payload = [episode(500, cast=[(10, 900)], crew=[(12, "Director")])]
    await upsert_season_credits(session, season_id=SEASON_ID, episodes=payload)
    await session.commit()
    await upsert_season_credits(session, season_id=SEASON_ID, episodes=payload)
    await session.commit()

    assert [(r.person_id, r.character_id) for r in await _cast_rows(session, 500)] == [(10, 900)]
    assert len(await _crew_rows(session, 500)) == 1


async def test_a_credit_removed_upstream_disappears(session, catalog):
    await upsert_season_credits(
        session,
        season_id=SEASON_ID,
        episodes=[episode(500, cast=[(10, 900), (11, 901)], crew=[(12, "Director")])],
    )
    await session.commit()

    await upsert_season_credits(
        session, season_id=SEASON_ID, episodes=[episode(500, cast=[(11, 901)])]
    )
    await session.commit()

    assert [r.person_id for r in await _cast_rows(session, 500)] == [11]
    assert await _crew_rows(session, 500) == []


async def test_an_episode_dropped_from_the_response_loses_its_credits(session, catalog):
    """Delete covers the whole season, not only the ids the response names —
    otherwise an episode moved out of the response would keep stale credits."""
    await upsert_season_credits(
        session,
        season_id=SEASON_ID,
        episodes=[episode(500, cast=[(10, 900)]), episode(501, cast=[(11, 901)])],
    )
    await session.commit()

    await upsert_season_credits(
        session, season_id=SEASON_ID, episodes=[episode(500, cast=[(10, 900)])]
    )
    await session.commit()

    assert [r.person_id for r in await _cast_rows(session, 500)] == [10]
    assert await _cast_rows(session, 501) == []


async def test_another_seasons_credits_are_untouched(session, catalog):
    await upsert_season_credits(
        session, season_id=OTHER_SEASON_ID, episodes=[episode(600, cast=[(20, 950)])]
    )
    await upsert_season_credits(
        session, season_id=SEASON_ID, episodes=[episode(500, cast=[(10, 900)])]
    )
    await session.commit()

    assert [r.person_id for r in await _cast_rows(session, 600)] == [20]


async def test_an_episode_we_do_not_mirror_is_skipped_and_the_watermark_still_stamps(
    session, catalog
):
    """The show gained an episode upstream since our last show fetch. That is
    ordinary and self-correcting — the daily refetches the show, then its
    seasons — so it must not fail the season and strand it on every retry."""
    await upsert_season_credits(
        session,
        season_id=SEASON_ID,
        episodes=[episode(500, cast=[(10, 900)]), episode(999999, cast=[(11, 901)])],
    )
    await session.commit()

    assert [r.person_id for r in await _cast_rows(session, 500)] == [10]
    assert await _cast_rows(session, 999999) == []
    assert (await _season(session, SEASON_ID)).credits_synced_at is not None


async def test_the_watermark_is_stamped(session, catalog):
    """Absence of credit rows cannot stand in for "not yet fetched": a whole
    season legitimately may carry none."""
    assert (await _season(session, SEASON_ID)).credits_synced_at is None

    await upsert_season_credits(session, season_id=SEASON_ID, episodes=[episode(500)])
    await session.commit()

    assert (await _season(session, SEASON_ID)).credits_synced_at is not None
    assert (await _season(session, OTHER_SEASON_ID)).credits_synced_at is None


async def test_people_and_characters_arrive_complete_from_the_embeds(session, catalog):
    """The embedded person object is byte-identical to `/people/{id}`, which is
    what retires the person initial pass."""
    await upsert_season_credits(
        session,
        season_id=SEASON_ID,
        episodes=[episode(500, cast=[(10, 900)], crew=[(12, "Story")])],
    )
    await session.commit()

    people = (await session.execute(select(m.Person))).scalars().all()
    assert {p.id for p in people} == {10, 12}
    assert (await session.execute(select(m.Character))).scalars().one().id == 900


async def test_episode_crew_roles_intern_separately_from_show_crew_roles(session, catalog):
    """Disjoint vocabularies, disjoint lookups. "Director" existing as a
    show-level role must not be reused as an episode-level role id."""
    show_role_id = await resolve_crew_role(session, "Director")
    await upsert_season_credits(
        session, season_id=SEASON_ID, episodes=[episode(500, crew=[(12, "Director")])]
    )
    await session.commit()

    (row,) = await _crew_rows(session, 500)
    episode_role = (
        await session.execute(select(m.EpisodeCrewRole).where(m.EpisodeCrewRole.id == row.role_id))
    ).scalar_one()
    assert episode_role.name == "Director"
    assert (
        await session.execute(select(m.CrewRole).where(m.CrewRole.id == show_role_id))
    ).scalar_one().name == "Director"


async def test_an_episode_with_a_null_season_id_still_refreshes_cleanly(session, catalog):
    """A season fetch whose episode rows never got a `season_id` (the show
    fetch matched no season by number) must still replace rather than collide
    with the three-part unique key on a re-run."""
    session.add(m.Episode(id=700, show_id=1, season_id=None, season=1, number=3, name="orphan"))
    await session.commit()

    payload = [episode(700, cast=[(10, 900)])]
    await upsert_season_credits(session, season_id=SEASON_ID, episodes=payload)
    await session.commit()
    await upsert_season_credits(session, season_id=SEASON_ID, episodes=payload)
    await session.commit()

    assert [r.person_id for r in await _cast_rows(session, 700)] == [10]

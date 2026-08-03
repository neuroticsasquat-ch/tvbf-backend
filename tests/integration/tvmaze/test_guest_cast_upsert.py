"""Guest-cast upsert tests — the per-person refresh grain (NEU-942)."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tvbf.tvmaze import models as m
from tvbf.tvmaze.api_payloads import TVMazeGuestCastCredit
from tvbf.tvmaze.upsert import (
    mark_person_credits_synced,
    upsert_person_guest_cast,
)


def guest_credit(
    episode_id: int | None,
    character_id: int | None,
    *,
    name: str = "Guest",
    is_self: bool = False,
    is_voice: bool = False,
) -> TVMazeGuestCastCredit:
    links: dict = {}
    if episode_id is not None:
        links["episode"] = {"href": f"https://api.tvmaze.com/episodes/{episode_id}"}
    if character_id is not None:
        links["character"] = {
            "href": f"https://api.tvmaze.com/characters/{character_id}",
            "name": name,
        }
    return TVMazeGuestCastCredit.model_validate(
        {"self": is_self, "voice": is_voice, "_links": links}
    )


@pytest.fixture
async def catalog(session):
    """One show, two episodes, two people — the FK targets guest credits need."""
    session.add(m.Show(id=1, name="S", tvmaze_updated=1))
    await session.flush()
    session.add_all(
        [
            m.Episode(id=500, show_id=1, season=1, number=1, name="E1"),
            m.Episode(id=501, show_id=1, season=1, number=2, name="E2"),
        ]
    )
    session.add_all(
        [
            m.Person(id=10, name="Guest Star", tvmaze_updated=1),
            m.Person(id=11, name="Other Guest", tvmaze_updated=1),
        ]
    )
    await session.commit()


async def _guest_rows(session, person_id: int) -> list[m.EpisodeGuestCast]:
    return list(
        (
            await session.execute(
                select(m.EpisodeGuestCast)
                .where(m.EpisodeGuestCast.person_id == person_id)
                .order_by(m.EpisodeGuestCast.sort_order)
            )
        )
        .scalars()
        .all()
    )


async def test_insert_preserves_upstream_order_and_flags(session, catalog):
    await upsert_person_guest_cast(
        session,
        person_id=10,
        credits=[
            guest_credit(501, 900, name="Himself", is_self=True),
            guest_credit(500, 901, is_voice=True),
        ],
    )
    await session.commit()

    rows = await _guest_rows(session, 10)
    assert [(r.episode_id, r.character_id) for r in rows] == [(501, 900), (500, 901)]
    assert [r.sort_order for r in rows] == [0, 1]
    assert rows[0].is_self is True and rows[0].is_voice is False
    assert rows[1].is_self is False and rows[1].is_voice is True


async def test_characters_referenced_by_links_are_created(session, catalog):
    await upsert_person_guest_cast(
        session, person_id=10, credits=[guest_credit(500, 902, name="Barista")]
    )
    await session.commit()

    character = (
        await session.execute(select(m.Character).where(m.Character.id == 902))
    ).scalar_one()
    assert character.name == "Barista"


async def test_refresh_replaces_rather_than_appends(session, catalog):
    await upsert_person_guest_cast(
        session, person_id=10, credits=[guest_credit(500, 903), guest_credit(501, 903)]
    )
    await session.commit()
    assert len(await _guest_rows(session, 10)) == 2

    await upsert_person_guest_cast(session, person_id=10, credits=[guest_credit(501, 903)])
    await session.commit()

    rows = await _guest_rows(session, 10)
    assert [(r.episode_id, r.character_id) for r in rows] == [(501, 903)]


async def test_refresh_is_scoped_to_the_person_not_the_episode(session, catalog):
    """The single easiest thing to get wrong: two people guest on the same
    episode, and re-syncing one must not delete the other's credit."""
    await upsert_person_guest_cast(session, person_id=10, credits=[guest_credit(500, 904)])
    await upsert_person_guest_cast(session, person_id=11, credits=[guest_credit(500, 905)])
    await session.commit()

    await upsert_person_guest_cast(session, person_id=10, credits=[guest_credit(501, 904)])
    await session.commit()

    assert [r.episode_id for r in await _guest_rows(session, 11)] == [500]
    assert [r.episode_id for r in await _guest_rows(session, 10)] == [501]


async def test_empty_credit_list_clears_the_persons_rows(session, catalog):
    await upsert_person_guest_cast(session, person_id=10, credits=[guest_credit(500, 906)])
    await session.commit()

    await upsert_person_guest_cast(session, person_id=10, credits=[])
    await session.commit()

    assert await _guest_rows(session, 10) == []


async def test_credits_missing_either_link_are_skipped(session, catalog):
    await upsert_person_guest_cast(
        session,
        person_id=10,
        credits=[
            guest_credit(None, 907),
            guest_credit(500, None),
            guest_credit(500, 907),
        ],
    )
    await session.commit()

    rows = await _guest_rows(session, 10)
    assert [(r.episode_id, r.character_id) for r in rows] == [(500, 907)]


async def test_duplicate_credits_collapse_to_one_row(session, catalog):
    await upsert_person_guest_cast(
        session, person_id=10, credits=[guest_credit(500, 908), guest_credit(500, 908)]
    )
    await session.commit()

    assert len(await _guest_rows(session, 10)) == 1


async def test_a_credit_for_an_unmirrored_episode_raises(session, catalog):
    """The FK doing its job. ~6% of guest-credited episodes are specials that
    only pass A fetches; a nonzero rate of these in prod means pass A never
    landed, and pass C's per-person error handling counts each one."""
    with pytest.raises(IntegrityError):
        await upsert_person_guest_cast(session, person_id=10, credits=[guest_credit(999999, 909)])
    await session.rollback()


async def test_a_character_written_by_the_show_axis_is_not_degraded(session, catalog):
    """Guest links carry only an id and a name. Upserting them would null the
    images pass A wrote from the full embedded character object, and a link
    with no name would overwrite a real name with ""."""
    session.add(
        m.Character(
            id=910,
            name="Detective Ramirez",
            image_medium="https://static.tvmaze.com/cm.jpg",
            image_original="https://static.tvmaze.com/co.jpg",
        )
    )
    await session.commit()

    await upsert_person_guest_cast(
        session,
        person_id=10,
        credits=[
            TVMazeGuestCastCredit.model_validate(
                {
                    "_links": {
                        "episode": {"href": "https://api.tvmaze.com/episodes/500"},
                        "character": {"href": "https://api.tvmaze.com/characters/910"},
                    }
                }
            )
        ],
    )
    await session.commit()

    character = (
        await session.execute(
            select(m.Character)
            .where(m.Character.id == 910)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert character.name == "Detective Ramirez"
    assert character.image_medium == "https://static.tvmaze.com/cm.jpg"
    assert character.image_original == "https://static.tvmaze.com/co.jpg"
    assert [r.character_id for r in await _guest_rows(session, 10)] == [910]


async def test_mark_person_credits_synced_stamps_the_watermark(session, catalog):
    await mark_person_credits_synced(session, person_id=10)
    await session.commit()

    person = (
        await session.execute(
            select(m.Person).where(m.Person.id == 10).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert person.credits_synced_at is not None

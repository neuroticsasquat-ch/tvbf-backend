"""The `catalog` credit tables (NEU-1038).

Same discipline as `test_catalog_models.py`: not a tour of the columns, but the
properties that would silently destroy credits if a convention were undone.

The two the ticket names as acceptance criteria both concern **per-show character
interning**, which is the one real model change here — TMDB has no character
entity, so `catalog.character` is `(show_id, name)` where `tvmaze.character` was a
global upstream id. Interning has to preserve recasting (two people, one
character, one show — 2,621 such characters in prod) while separating the same
name across shows (exactly one character in prod spans two, and it stops being
one thing).
"""

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from tvbf.catalog import models as m


async def _show(session, name="A Show", **kwargs) -> m.Show:
    show = m.Show(name=name, **kwargs)
    session.add(show)
    await session.flush()
    return show


async def _episode(session, show, number=1) -> m.Episode:
    episode = m.Episode(
        show_id=show.id, tmdb_id=None, season_number=1, episode_number=number, name=f"E{number}"
    )
    session.add(episode)
    await session.flush()
    return episode


async def _person(session, name, tmdb_id=None) -> m.Person:
    person = m.Person(name=name, tmdb_id=tmdb_id)
    session.add(person)
    await session.flush()
    return person


async def _character(session, show, name) -> m.Character:
    character = m.Character(show_id=show.id, name=name)
    session.add(character)
    await session.flush()
    return character


async def _role(session, department, job) -> m.CrewRole:
    role = m.CrewRole(department=department, job=job)
    session.add(role)
    await session.flush()
    return role


class TestCharacterInterning:
    """The ticket's two acceptance criteria, and the constraint that produces
    both of them."""

    async def test_two_people_as_one_character_make_two_credits_and_one_character(self, session):
        """Recasting and voice ensembles. The Simpsons credits both Hank Azaria
        and Harry Shearer as Carl Carlson; 2,621 characters in prod are like it,
        and all of them survive because interning is per show, not per person."""
        show = await _show(session, name="The Simpsons", tmdb_id=456)
        carl = await _character(session, show, "Carl Carlson")
        azaria = await _person(session, "Hank Azaria", tmdb_id=886)
        shearer = await _person(session, "Harry Shearer", tmdb_id=1226)
        session.add_all(
            [
                m.ShowCast(show_id=show.id, person_id=azaria.id, character_id=carl.id),
                m.ShowCast(show_id=show.id, person_id=shearer.id, character_id=carl.id),
            ]
        )
        await session.flush()

        credits = (
            await session.execute(select(m.ShowCast).where(m.ShowCast.show_id == show.id))
        ).scalars()
        characters = (
            await session.execute(
                select(func.count()).select_from(m.Character).where(m.Character.show_id == show.id)
            )
        ).scalar_one()

        assert len(list(credits)) == 2
        assert characters == 1

    async def test_one_name_on_two_shows_makes_two_characters(self, session):
        """The loss the narrowing accepts, stated as behaviour. Two shows that
        both credit "The Doctor" are crediting two different roles, and after
        NEU-1038 the schema says so."""
        first = await _show(session, name="First Show", tmdb_id=1)
        second = await _show(session, name="Second Show", tmdb_id=2)
        await _character(session, first, "The Doctor")
        await _character(session, second, "The Doctor")

        rows = (
            await session.execute(select(m.Character).where(m.Character.name == "The Doctor"))
        ).scalars()
        assert len({r.show_id for r in rows}) == 2

    async def test_one_name_is_interned_once_within_a_show(self, session):
        """The other half of the same constraint: without it, interning is not
        interning and a re-ingest grows a character row per pass."""
        show = await _show(session, tmdb_id=1)
        await _character(session, show, "Walter White")
        session.add(m.Character(show_id=show.id, name="Walter White"))

        with pytest.raises(IntegrityError):
            await session.flush()


class TestShowCastUniqueness:
    """No `UNIQUE (show_id, person_id, character_id)` — carried forward from
    `tvmaze.show_cast` deliberately."""

    async def test_the_same_person_and_character_may_be_credited_twice(self, session):
        """Refresh is delete-then-insert, so there is nothing to conflict on, and
        a uniqueness assumption over upstream data is what broke ingestion on
        `tvmaze.season`. If this test starts failing, a unique key has been added
        and a re-ingest will abort mid-pass on some long-tail show."""
        show = await _show(session, tmdb_id=1)
        character = await _character(session, show, "Self")
        person = await _person(session, "A Person", tmdb_id=10)
        session.add_all(
            [
                m.ShowCast(show_id=show.id, person_id=person.id, character_id=character.id),
                m.ShowCast(show_id=show.id, person_id=person.id, character_id=character.id),
            ]
        )
        await session.flush()

        credits = (
            await session.execute(select(m.ShowCast).where(m.ShowCast.show_id == show.id))
        ).scalars()
        assert len(list(credits)) == 2

    async def test_a_credit_may_carry_no_character(self, session):
        """TMDB sends free text where TV Maze sent an object, and free text can be
        empty — 1 blank of 7,629 sampled roles. NOT NULL would abort a
        multi-hour pass on that one row."""
        show = await _show(session, tmdb_id=1)
        person = await _person(session, "Uncredited", tmdb_id=10)
        session.add(m.ShowCast(show_id=show.id, person_id=person.id, credit_id="abc"))
        await session.flush()

        stored = (await session.execute(select(m.ShowCast))).scalar_one()
        assert stored.character_id is None


class TestCrewRolesShareOneLookup:
    """`tvmaze` keeps show and episode crew roles in two tables because its two
    vocabularies are disjoint. TMDB's are not — all 78 episode-level
    `(department, job)` pairs also appear at show level (100% overlap, measured by
    `scripts/probe_tmdb_credit_shapes.py`) — so a second lookup would hold a copy
    of the same values and split a person's credits across two tables.
    """

    async def test_a_show_credit_and_an_episode_credit_share_a_role_row(self, session):
        show = await _show(session, tmdb_id=1)
        episode = await _episode(session, show)
        director = await _role(session, "Directing", "Director")
        person = await _person(session, "Vince Gilligan", tmdb_id=66633)
        session.add_all(
            [
                m.ShowCrew(show_id=show.id, person_id=person.id, role_id=director.id),
                m.EpisodeCrew(episode_id=episode.id, person_id=person.id, role_id=director.id),
            ]
        )
        await session.flush()

        roles = (await session.execute(select(m.CrewRole))).scalars().all()
        assert len(roles) == 1

    async def test_one_job_name_in_two_departments_is_two_roles(self, session):
        """Interning is on the pair, not the job. `job` alone is ambiguous —
        "Other" appears under several departments — and `department` is what a
        person page groups by."""
        await _role(session, "Directing", "Other")
        await _role(session, "Production", "Other")

        roles = (await session.execute(select(m.CrewRole))).scalars().all()
        assert len(roles) == 2

    async def test_the_same_pair_is_interned_once(self, session):
        await _role(session, "Writing", "Writer")
        session.add(m.CrewRole(department="Writing", job="Writer"))

        with pytest.raises(IntegrityError):
            await session.flush()


class TestEpisodeCreditUniqueness:
    """Three-part keys on both episode tables, carried forward with their
    measurements — the grain is one season's episodes at a time, so unlike show
    credits there is a single writer and a unique key is possible."""

    async def test_one_character_played_by_two_people_survives(self, session):
        """17 of 1,043 sampled episodes do this, so `(episode_id, character_id)`
        would silently drop legitimate rows."""
        show = await _show(session, tmdb_id=1)
        episode = await _episode(session, show)
        character = await _character(session, show, "The Narrator")
        first = await _person(session, "First", tmdb_id=1)
        second = await _person(session, "Second", tmdb_id=2)
        session.add_all(
            [
                m.EpisodeGuestCast(
                    episode_id=episode.id, person_id=first.id, character_id=character.id
                ),
                m.EpisodeGuestCast(
                    episode_id=episode.id, person_id=second.id, character_id=character.id
                ),
            ]
        )
        await session.flush()

        rows = (await session.execute(select(m.EpisodeGuestCast))).scalars().all()
        assert len(rows) == 2

    async def test_the_same_guest_credit_cannot_be_recorded_twice(self, session):
        show = await _show(session, tmdb_id=1)
        episode = await _episode(session, show)
        character = await _character(session, show, "Guest")
        person = await _person(session, "A Person", tmdb_id=1)
        session.add(
            m.EpisodeGuestCast(
                episode_id=episode.id, person_id=person.id, character_id=character.id
            )
        )
        await session.flush()
        session.add(
            m.EpisodeGuestCast(
                episode_id=episode.id, person_id=person.id, character_id=character.id
            )
        )

        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_two_characterless_guest_credits_still_conflict(self, session):
        """`NULLS NOT DISTINCT`. Under Postgres's default two NULL characters
        never conflict, so a re-ingest would add another copy of the row every
        time —         the same trap `uq_watch_archive_source_row` (retired in NEU-1158) avoids."""
        show = await _show(session, tmdb_id=1)
        episode = await _episode(session, show)
        person = await _person(session, "A Person", tmdb_id=1)
        session.add(m.EpisodeGuestCast(episode_id=episode.id, person_id=person.id))
        await session.flush()
        session.add(m.EpisodeGuestCast(episode_id=episode.id, person_id=person.id))

        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_one_person_may_hold_two_crew_roles_on_an_episode(self, session):
        """36 of 1,043 sampled episodes do this — writer and director on the same
        episode is the common shape."""
        show = await _show(session, tmdb_id=1)
        episode = await _episode(session, show)
        person = await _person(session, "Vince Gilligan", tmdb_id=66633)
        writer = await _role(session, "Writing", "Writer")
        director = await _role(session, "Directing", "Director")
        session.add_all(
            [
                m.EpisodeCrew(episode_id=episode.id, person_id=person.id, role_id=writer.id),
                m.EpisodeCrew(episode_id=episode.id, person_id=person.id, role_id=director.id),
            ]
        )
        await session.flush()

        rows = (await session.execute(select(m.EpisodeCrew))).scalars().all()
        assert len(rows) == 2

    async def test_the_same_episode_crew_credit_cannot_be_recorded_twice(self, session):
        show = await _show(session, tmdb_id=1)
        episode = await _episode(session, show)
        person = await _person(session, "A Person", tmdb_id=1)
        role = await _role(session, "Editing", "Editor")
        session.add(m.EpisodeCrew(episode_id=episode.id, person_id=person.id, role_id=role.id))
        await session.flush()
        session.add(m.EpisodeCrew(episode_id=episode.id, person_id=person.id, role_id=role.id))

        with pytest.raises(IntegrityError):
            await session.flush()


class TestCreditDeleteBehaviour:
    """Credits are derived data and cascade freely — unlike a show row, which is
    tombstoned rather than deleted because user watch history hangs off it."""

    async def test_deleting_a_show_takes_its_characters_and_credits(self, session):
        show = await _show(session, tmdb_id=1)
        character = await _character(session, show, "Someone")
        person = await _person(session, "A Person", tmdb_id=1)
        role = await _role(session, "Directing", "Director")
        session.add_all(
            [
                m.ShowCast(show_id=show.id, person_id=person.id, character_id=character.id),
                m.ShowCrew(show_id=show.id, person_id=person.id, role_id=role.id),
            ]
        )
        await session.flush()

        await session.delete(show)
        await session.flush()

        assert (await session.execute(select(m.Character))).scalars().all() == []
        assert (await session.execute(select(m.ShowCast))).scalars().all() == []
        assert (await session.execute(select(m.ShowCrew))).scalars().all() == []
        # The person outlives the show — they are credited elsewhere.
        assert len((await session.execute(select(m.Person))).scalars().all()) == 1

    async def test_deleting_an_episode_takes_its_guest_cast_and_crew(self, session):
        show = await _show(session, tmdb_id=1)
        episode = await _episode(session, show)
        person = await _person(session, "A Person", tmdb_id=1)
        role = await _role(session, "Writing", "Writer")
        session.add_all(
            [
                m.EpisodeGuestCast(episode_id=episode.id, person_id=person.id),
                m.EpisodeCrew(episode_id=episode.id, person_id=person.id, role_id=role.id),
            ]
        )
        await session.flush()

        await session.delete(episode)
        await session.flush()

        assert (await session.execute(select(m.EpisodeGuestCast))).scalars().all() == []
        assert (await session.execute(select(m.EpisodeCrew))).scalars().all() == []


class TestPersonIdentity:
    """ADR-0008's convention, one more time: the surrogate is ours, `tmdb_id` is
    upstream's, and NULL there means locally-authored."""

    async def test_two_people_may_not_share_an_upstream_id(self, session):
        await _person(session, "Real", tmdb_id=66633)
        session.add(m.Person(name="Impostor", tmdb_id=66633))

        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_many_people_may_have_no_upstream_id(self, session):
        first = await _person(session, "Local one")
        second = await _person(session, "Local two")

        assert first.id != second.id

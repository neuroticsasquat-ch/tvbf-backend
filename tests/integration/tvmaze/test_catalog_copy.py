"""The id-preserving copy of `tvmaze.*` into `catalog.*` (NEU-1042).

Everything here is a property the migration's no-loss guarantee rests on. The
central one is in `TestIdsArePreserved`: if ids move, `app.user_episode_watch`
stops resolving and the migration turns from re-pointing a constraint into
rewriting user data.
"""

from datetime import date

import pytest
from sqlalchemy import select, text

from tvbf.app import models as a
from tvbf.catalog import models as c
from tvbf.tvmaze import models as t
from tvbf.tvmaze.catalog_copy import copy_to_catalog, verify_copy


async def _show(session, show_id: int, **kwargs) -> t.Show:
    show = t.Show(
        id=show_id,
        name=kwargs.pop("name", f"Show {show_id}"),
        tvmaze_updated=kwargs.pop("tvmaze_updated", 1),
        **kwargs,
    )
    session.add(show)
    await session.flush()
    return show


async def _season(session, season_id: int, show_id: int, number: int, **kwargs) -> t.Season:
    season = t.Season(id=season_id, show_id=show_id, number=number, **kwargs)
    session.add(season)
    await session.flush()
    return season


async def _episode(session, episode_id: int, show_id: int, season: int, **kwargs) -> t.Episode:
    episode = t.Episode(id=episode_id, show_id=show_id, season=season, **kwargs)
    session.add(episode)
    await session.flush()
    return episode


@pytest.fixture
async def copied(session):
    """A small mirror, copied. Returns the `CopyResult`."""
    show = await _show(
        session,
        169,
        name="Breaking Bad",
        status="Ended",
        type="Scripted",
        language="English",
        summary="<p>A teacher.</p>",
        official_site="https://example.test",
        premiered=date(2008, 1, 20),
        ended=date(2013, 9, 29),
        runtime=45,
        image_medium="https://static.tvmaze.com/medium.jpg",
        externals_imdb="tt0903747",
        externals_tvdb=81189,
    )
    await _season(session, 1000, show.id, 1, name="Season 1", episode_order=7)
    await _episode(session, 5000, show.id, 1, season_id=1000, number=1, name="Pilot")
    await _episode(session, 5001, show.id, 1, season_id=1000, number=2, name="Cat's in the Bag")
    session.add(t.ShowAka(id=77, show_id=show.id, name="Breaking Bad DE", country_code="DE"))
    await session.flush()

    result = await copy_to_catalog(session)
    await session.commit()
    return result


class TestIdsArePreserved:
    """The whole point of the ticket. `catalog.show.id` *is* the old
    `tvmaze.show.id`, so `app.user_show_watch` and `app.user_episode_watch`
    never have to be rewritten."""

    async def test_show_season_and_episode_keep_their_ids(self, session, copied):
        show = (await session.execute(select(c.Show))).scalar_one()
        season = (await session.execute(select(c.Season))).scalar_one()
        episodes = (await session.execute(select(c.Episode).order_by(c.Episode.id))).scalars().all()

        assert show.id == 169
        assert season.id == 1000
        assert [e.id for e in episodes] == [5000, 5001]

    async def test_every_row_lands_locally_authored(self, session, copied):
        """`tmdb_id IS NULL` is not a placeholder — enrichment is NEU-1043's, and
        a show TMDB never matches stays this way permanently."""
        assert (await session.execute(select(c.Show.tmdb_id))).scalar_one() is None
        assert (await session.execute(select(c.Season.tmdb_id))).scalar_one() is None
        assert set((await session.execute(select(c.Episode.tmdb_id))).scalars()) == {None}

    async def test_a_watched_episode_id_resolves_on_the_new_spine(self, session, make_user):
        """The acceptance criterion, against real `app` rows rather than a
        restatement of the copy's own anti-join.

        This is what NEU-1046 will re-point the foreign key to, and the whole
        reason ids are preserved: the watch row is not rewritten, the constraint
        underneath it moves.
        """
        user = await make_user(email="watcher@example.test")
        show = await _show(session, 169)
        await _episode(session, 5000, show.id, 1, number=1)
        await _episode(session, 5001, show.id, 1, number=None)
        session.add_all(
            [
                a.UserShowWatch(user_id=user.id, show_id=show.id),
                a.UserEpisodeWatch(user_id=user.id, episode_id=5000),
                # A special, which is where the synthetic numbering has to hold
                # up: 156 of prod's watch rows point at one.
                a.UserEpisodeWatch(user_id=user.id, episode_id=5001),
            ]
        )
        await session.flush()

        await copy_to_catalog(session)

        unresolved_episodes = (
            await session.execute(
                text(
                    "SELECT count(*) FROM app.user_episode_watch w "
                    "WHERE NOT EXISTS (SELECT 1 FROM catalog.episode e WHERE e.id = w.episode_id)"
                )
            )
        ).scalar_one()
        unresolved_shows = (
            await session.execute(
                text(
                    "SELECT count(*) FROM app.user_show_watch s "
                    "WHERE NOT EXISTS (SELECT 1 FROM catalog.show c WHERE c.id = s.show_id)"
                )
            )
        ).scalar_one()

        assert (unresolved_episodes, unresolved_shows) == (0, 0)


class TestColumnsThatTravel:
    """Copied where the stored value is still a correct instance of what the
    destination column means — and left null where it is not."""

    async def test_the_values_that_transfer_do(self, session, copied):
        show = (await session.execute(select(c.Show))).scalar_one()

        assert show.name == "Breaking Bad"
        assert show.overview == "<p>A teacher.</p>"
        assert show.homepage == "https://example.test"
        assert show.first_air_date == date(2008, 1, 20)
        assert show.last_air_date == date(2013, 9, 29)
        assert (show.imdb_id, show.tvdb_id) == ("tt0903747", 81189)

    async def test_status_travels_because_is_ended_is_generated_from_it(self, session, copied):
        """The knowing exception. `Ended` is not TMDB's vocabulary, but a null
        status makes every unmatched show read as still-running — wrong for the
        ~76% of the mirror that has ended."""
        show = (await session.execute(select(c.Show))).scalar_one()

        assert show.status == "Ended"
        assert show.is_ended is True

    async def test_an_image_url_is_not_copied_into_a_path_column(self, session, copied):
        """`image_medium` is a full URL; `poster_path` holds a TMDB path
        fragment that a consumer resolves against the image base."""
        show = (await session.execute(select(c.Show))).scalar_one()

        assert show.poster_path is None
        assert show.backdrop_path is None

    async def test_a_language_name_is_not_copied_into_an_iso_code_column(self, session, copied):
        """TV Maze stores `English` where `original_language` holds `en`, and the
        browse filter's exact-match semantics read that column."""
        show = (await session.execute(select(c.Show))).scalar_one()

        assert show.original_language is None

    async def test_akas_travel_under_the_glossary_name(self, session, copied):
        aka = (await session.execute(select(c.ShowAka))).scalar_one()

        assert (aka.id, aka.show_id) == (77, 169)
        assert aka.title == "Breaking Bad DE"
        assert aka.country_code == "DE"

    async def test_lookup_tables_are_deliberately_not_copied(self, session):
        """`catalog.genre` and `catalog.network` are keyed on `tmdb_id`, so a
        copied row would never match the one the ingest creates and every genre
        would end up stored twice."""
        show = await _show(session, 169)
        genre = t.Genre(name="Drama")
        session.add(genre)
        await session.flush()
        session.add(t.ShowGenre(show_id=show.id, genre_id=genre.id))
        await session.flush()

        await copy_to_catalog(session)

        assert (await session.execute(select(c.Genre))).scalars().all() == []
        assert (await session.execute(select(c.ShowGenre))).scalars().all() == []


class TestSpecialsGetASyntheticNumber:
    """`catalog.episode.episode_number` is NOT NULL because a TMDB special is
    season 0 with a real number (D2). TV Maze marked a special with a *null*
    number instead — 27,498 of them in prod, 156 watched by a real user — so the
    copy has to give them one or lose the rows.

    They are numbered negative. A real episode number always starts at 1, so a
    negative one can never be collided with by an episode the daily adds later —
    and it says plainly that we made the number up.
    """

    async def test_they_are_numbered_negative_within_their_season(self, session):
        show = await _show(session, 169)
        await _season(session, 1000, show.id, 3)
        await _episode(session, 5000, show.id, 3, season_id=1000, number=1)
        await _episode(session, 5001, show.id, 3, season_id=1000, number=2)
        await _episode(session, 5002, show.id, 3, season_id=1000, number=None)
        await _episode(session, 5003, show.id, 3, season_id=1000, number=None)

        await copy_to_catalog(session)

        rows = (await session.execute(select(c.Episode).order_by(c.Episode.id))).scalars().all()
        numbered = {e.id: e.episode_number for e in rows}
        assert numbered[5000] == 1
        assert numbered[5001] == 2
        assert numbered[5002] == -1
        assert numbered[5003] == -2

    async def test_a_special_cannot_collide_with_a_real_episode(self, session):
        """The whole reason for negatives. A season numbering its specials 24, 25
        after a 23-episode run breaks the moment TV Maze adds a genuine episode
        24 — which it does, daily, right up until cutover."""
        show = await _show(session, 169)
        await _episode(session, 5000, show.id, 1, number=1)
        await _episode(session, 5001, show.id, 1, number=None)

        await copy_to_catalog(session)

        numbers = (await session.execute(select(c.Episode.episode_number))).scalars().all()
        assert sorted(numbers) == [-1, 1]

    async def test_a_shows_duplicate_season_numbers_carry_through_untouched(self, session):
        """Measured on the full mirror: 13 shows carry two seasons with the same
        number, and their episodes account for all 2,298 duplicate
        `(show, season, number)` triples — every one of which already exists in
        `tvmaze`. The copy neither introduces nor repairs them, which is why
        neither schema carries a unique key on the pair.
        """
        show = await _show(session, 169)
        await _season(session, 1000, show.id, 3)
        await _season(session, 1001, show.id, 3)
        await _episode(session, 5000, show.id, 3, season_id=1000, number=1)
        await _episode(session, 5001, show.id, 3, season_id=1001, number=1)
        await _episode(session, 5002, show.id, 3, season_id=1001, number=None)

        await copy_to_catalog(session)

        rows = (await session.execute(select(c.Episode).order_by(c.Episode.id))).scalars().all()
        assert [(e.season_id, e.episode_number) for e in rows] == [
            (1000, 1),
            (1001, 1),
            (1001, -1),
        ]

    async def test_the_real_season_number_is_kept_rather_than_moved_to_zero(self, session):
        """27,458 of prod's null-number specials carry a real season number, and
        season 0 would discard exactly that."""
        show = await _show(session, 169)
        await _episode(session, 5000, show.id, 3, number=None)

        await copy_to_catalog(session)

        episode = (await session.execute(select(c.Episode))).scalar_one()
        assert episode.season_number == 3
        assert episode.episode_number == -1

    async def test_numbering_is_stable_across_runs(self, session):
        """Deterministic, so a re-run and a human reconciling afterwards see the
        same answer. `(airdate, id)` is a total order."""
        show = await _show(session, 169)
        for episode_id in (5000, 5001, 5002):
            await _episode(session, episode_id, show.id, 1, number=None)
        await copy_to_catalog(session)
        first = {
            e.id: e.episode_number
            for e in (await session.execute(select(c.Episode))).scalars().all()
        }

        await session.execute(text("DELETE FROM catalog.episode"))
        await copy_to_catalog(session)

        second = {
            e.id: e.episode_number
            for e in (
                await session.execute(select(c.Episode).execution_options(populate_existing=True))
            )
            .scalars()
            .all()
        }
        assert first == second

    async def test_a_later_run_does_not_reuse_a_number_it_already_handed_out(self, session):
        """The job is re-runnable *and* `tvmaze` keeps moving under it — the
        daily update writes that schema until NEU-1050.

        So the floor comes from what `catalog` already holds, not from what this
        batch can see in the source. Otherwise the count restarts and the next
        special lands on an ordinal an earlier run already gave away — silently,
        because there is no unique key on `(show_id, season_number,
        episode_number)` and `ON CONFLICT DO NOTHING` leaves the earlier row be.
        """
        show = await _show(session, 169)
        await _episode(session, 5000, show.id, 1, number=1)
        await _episode(session, 5001, show.id, 1, number=None)
        await copy_to_catalog(session)
        await session.commit()

        # The daily brings in a real episode 2 and another special.
        await _episode(session, 5002, show.id, 1, number=2)
        await _episode(session, 5003, show.id, 1, number=None)
        await copy_to_catalog(session)
        await session.commit()

        numbers = (
            (
                await session.execute(
                    select(c.Episode.episode_number)
                    .where(c.Episode.season_number == 1)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        assert sorted(numbers) == [-2, -1, 1, 2]
        assert len(numbers) == len(set(numbers))

    async def test_an_out_of_order_upstream_id_still_appends(self, session):
        """A special backfilled with an id *below* one already copied must take
        the next ordinal down, not re-take an ordinal in use."""
        show = await _show(session, 169)
        await _episode(session, 5005, show.id, 1, number=None)
        await copy_to_catalog(session)
        await session.commit()

        await _episode(session, 5001, show.id, 1, number=None)
        await copy_to_catalog(session)
        await session.commit()

        numbers = (
            (
                await session.execute(
                    select(c.Episode.episode_number).execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        assert sorted(numbers) == [-2, -1]


class TestIdempotence:
    async def test_a_second_run_copies_nothing_new(self, session, copied):
        before = (
            (await session.execute(select(c.Episode.id).order_by(c.Episode.id))).scalars().all()
        )

        result = await copy_to_catalog(session)
        await session.commit()

        after = (await session.execute(select(c.Episode.id).order_by(c.Episode.id))).scalars().all()
        assert after == before
        assert result.complete

    async def test_a_re_run_does_not_undo_enrichment(self, session, copied):
        """NEU-1043 stamps `tmdb_id` on these rows and NEU-1034 then updates them
        from TMDB. `ON CONFLICT (id) DO NOTHING` is what stops a later re-run of
        this job reverting that to the TV Maze values."""
        await session.execute(
            text("UPDATE catalog.show SET tmdb_id = 1396, name = 'Enriched' WHERE id = 169")
        )
        await session.commit()

        await copy_to_catalog(session)
        await session.commit()

        show = (
            await session.execute(select(c.Show).execution_options(populate_existing=True))
        ).scalar_one()
        assert (show.tmdb_id, show.name) == (1396, "Enriched")


class TestIdentitySequences:
    """A generated id must never land on one the copy already placed."""

    async def test_show_aka_restarts_clear_of_the_copied_ids(self, session, copied):
        """The one that would actually have bitten: `catalog.show_aka`'s identity
        starts at 1, well inside the 85,707 ids the copy brings across."""
        session.add(c.ShowAka(show_id=169, title="Generated"))
        await session.flush()

        generated = (
            await session.execute(select(c.ShowAka).where(c.ShowAka.title == "Generated"))
        ).scalar_one()
        assert generated.id > 77

    async def test_the_spine_tables_stay_above_their_configured_starts(self, session, copied):
        """Prod's maxima (93,485 / 204,059 / 3,695,163) already sit below
        NEU-1032's starts, so this asserts the invariant rather than repairing
        it — but the job re-asserts it either way."""
        assert copied.sequences["catalog.show"] == 1_000_000
        assert copied.sequences["catalog.season"] == 1_000_000
        assert copied.sequences["catalog.episode"] == 10_000_000

    async def test_a_copied_id_above_the_configured_start_pushes_the_sequence_past_it(
        self, session
    ):
        await _show(session, 2_000_000)

        result = await copy_to_catalog(session)

        assert result.sequences["catalog.show"] == 2_000_001
        session.add(c.Show(name="Born here"))
        await session.flush()
        born = (
            await session.execute(select(c.Show).where(c.Show.name == "Born here"))
        ).scalar_one()
        assert born.id == 2_000_001

    async def test_the_counter_never_moves_backwards(self, session, copied):
        """A re-run after the TMDB ingest has minted ids of its own must not
        rewind into territory already handed out."""
        session.add(c.Show(name="Ingested", tmdb_id=1399))
        await session.flush()
        await session.commit()

        result = await copy_to_catalog(session)

        assert result.sequences["catalog.show"] > 1_000_000


class TestVerification:
    """The run reports what landed by anti-join, not by comparing totals."""

    async def test_it_reports_complete_when_every_row_arrived(self, session, copied):
        by_table = {t.table: t for t in copied.tables}

        assert copied.complete
        assert by_table["catalog.show"].source_rows == 1
        assert by_table["catalog.episode"].copied_rows == 2
        assert all(t.missing_rows == 0 for t in copied.tables)

    async def test_a_missing_row_is_caught_even_when_the_totals_balance(self, session, copied):
        """Why the check is an anti-join rather than the ticket's matching counts.

        Drop one copied show and add an unrelated one — which is exactly what the
        TMDB ingest does to this table — and the two totals still agree while a
        show is genuinely absent from the new spine.
        """
        await session.execute(text("DELETE FROM catalog.show WHERE id = 169"))
        await session.execute(
            text("INSERT INTO catalog.show (id, name, tmdb_id) VALUES (500001, 'Ingested', 1399)")
        )
        await session.commit()

        tables = await verify_copy(session)

        show_table = next(t for t in tables if t.table == "catalog.show")
        assert show_table.source_rows == show_table.copied_rows == 1
        assert show_table.missing_rows == 1

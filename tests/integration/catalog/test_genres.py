"""The genre vocabulary on the `catalog` spine (NEU-1064, ADR-0011).

The acceptance criteria of a vocabulary change, stated as behaviour: the list
`GET /genres` will return is TMDB's, filtering by each of its values finds the
shows carrying it, AND semantics across repeated parameters survive, and a show
TMDB never matched — which therefore has no genres at all — filters and
serialises like any other.

That genres are *only* ever TMDB's is `tests/integration/tvmaze/test_catalog_copy.py`'s
to prove; it is the reason the empty case below is ordinary rather than exotic.
"""

from tvbf.catalog import genres as q
from tvbf.catalog import models as m
from tvbf.tvmaze import models as t
from tvbf.tvmaze.schemas import GenreOut, build_show_detail, build_show_summary

# `GET /genre/tv/list`, read 2026-08-09. The whole published TV vocabulary —
# the list every value below has to come from, and the seven names shared with
# TV Maze are deliberately not marked out: nothing in the code treats them
# differently.
TMDB_TV_GENRES = {
    10759: "Action & Adventure",
    16: "Animation",
    35: "Comedy",
    80: "Crime",
    99: "Documentary",
    18: "Drama",
    10751: "Family",
    10762: "Kids",
    9648: "Mystery",
    10763: "News",
    10764: "Reality",
    10765: "Sci-Fi & Fantasy",
    10766: "Soap",
    10767: "Talk",
    10768: "War & Politics",
    37: "Western",
}


async def _show(session, name: str, *, tmdb_id: int | None) -> m.Show:
    show = m.Show(name=name, tmdb_id=tmdb_id)
    session.add(show)
    await session.flush()
    return show


async def _seed_vocabulary(session) -> dict[str, m.Genre]:
    rows = [m.Genre(tmdb_id=tid, name=name) for tid, name in TMDB_TV_GENRES.items()]
    session.add_all(rows)
    await session.flush()
    return {g.name: g for g in rows}


async def _tag(session, show: m.Show, *genres: m.Genre) -> None:
    session.add_all([m.ShowGenre(show_id=show.id, genre_id=g.id) for g in genres])
    await session.flush()


class TestTheVocabularyIsTmdbs:
    async def test_list_genres_returns_tmdbs_names_alphabetically(self, session):
        await _seed_vocabulary(session)

        listed = await q.list_genres(session)

        assert [g.name for g in listed] == sorted(TMDB_TV_GENRES.values())

    async def test_the_response_model_serialises_a_catalog_row(self, session):
        """`GET /genres` answers with `GenreOut`, whose `id` is the surrogate
        (ADR-0008) and not `tmdb_id` — so the ids in the body change at cutover
        along with the names."""
        vocabulary = await _seed_vocabulary(session)

        body = [GenreOut.model_validate(g) for g in await q.list_genres(session)]

        drama = next(g for g in body if g.name == "Drama")
        assert drama.id == vocabulary["Drama"].id != 18

    async def test_every_value_it_returns_filters_to_the_shows_carrying_it(self, session):
        """The half of the criterion a name list cannot prove on its own: each
        value is not merely present, it selects."""
        vocabulary = await _seed_vocabulary(session)
        for name, genre in vocabulary.items():
            show = await _show(session, f"Only {name}", tmdb_id=genre.tmdb_id)
            await _tag(session, show, genre)

        for name in vocabulary:
            matched = (await session.execute(q.shows_with_all_genres([name]))).scalars().all()

            assert len(matched) == 1, name

    async def test_naming_no_genres_selects_nothing_rather_than_everything(self, session):
        """`WHERE name IN ()`, as it reads. The caller filters only when the
        parameter was supplied — `list_shows` guards with `if filters.genres:`
        — and this pins that precondition so NEU-1047 does not rediscover it as
        an empty browse page."""
        vocabulary = await _seed_vocabulary(session)
        show = await _show(session, "Tagged", tmdb_id=1)
        await _tag(session, show, vocabulary["Drama"])

        matched = (await session.execute(q.shows_with_all_genres([]))).scalars().all()

        assert matched == []

    async def test_a_tv_maze_only_name_matches_nothing(self, session):
        """`?genre=Anime` returning an empty list is the cutover's cost, not a
        bug — `Anime` disappears into `Animation` and no row carries it."""
        await _seed_vocabulary(session)

        matched = (await session.execute(q.shows_with_all_genres(["Anime"]))).scalars().all()

        assert matched == []


class TestAndSemantics:
    async def test_a_show_needs_every_named_genre_not_just_one(self, session):
        vocabulary = await _seed_vocabulary(session)
        both = await _show(session, "Both", tmdb_id=1)
        await _tag(session, both, vocabulary["Comedy"], vocabulary["Drama"])
        one = await _show(session, "One", tmdb_id=2)
        await _tag(session, one, vocabulary["Comedy"])

        matched = (
            (await session.execute(q.shows_with_all_genres(["Comedy", "Drama"]))).scalars().all()
        )

        assert matched == [both.id]

    async def test_a_repeated_value_names_one_genre_not_two(self, session):
        """Counting the raw parameters instead would set the bar at two names a
        show cannot both carry, and the filter would return nothing."""
        vocabulary = await _seed_vocabulary(session)
        show = await _show(session, "Comedy show", tmdb_id=1)
        await _tag(session, show, vocabulary["Comedy"])

        matched = (
            (await session.execute(q.shows_with_all_genres(["Comedy", "Comedy"]))).scalars().all()
        )

        assert matched == [show.id]

    async def test_two_rows_sharing_a_name_still_satisfy_that_name(self, session):
        """`catalog.genre` has no `UNIQUE (name)` — only `UNIQUE (tmdb_id)` —
        so two rows may carry one name where `tvmaze.genre` made that
        impossible. Counting distinct ids would read the show as carrying two
        of the one genre asked for and drop it."""
        vocabulary = await _seed_vocabulary(session)
        alias = m.Genre(tmdb_id=999_999, name="Comedy")
        session.add(alias)
        await session.flush()
        show = await _show(session, "Doubly comic", tmdb_id=1)
        await _tag(session, show, vocabulary["Comedy"], alias)

        matched = (await session.execute(q.shows_with_all_genres(["Comedy"]))).scalars().all()

        assert matched == [show.id]


class TestAShowWithNoGenres:
    """Every show TMDB never matched — roughly 26k of them, since NEU-1042
    copied no genre rows and the ingest is the only writer."""

    async def test_it_is_hydrated_as_an_empty_list_not_a_missing_key(self, session):
        await _seed_vocabulary(session)
        unmatched = await _show(session, "Never matched", tmdb_id=None)

        by_show = await q.genres_by_show(session, [unmatched.id])

        assert by_show == {unmatched.id: []}

    async def test_its_detail_genres_are_empty(self, session):
        unmatched = await _show(session, "Never matched", tmdb_id=None)

        assert await q.genres_for_show(session, unmatched.id) == []

    async def test_it_serialises(self, session):
        """Both response builders take what the queries above return and reach
        a body, with `genres: []` rather than a null or a missing key.

        The show handed to them is a `tvmaze.Show` because they still read
        `show.language`, which `catalog.show` does not have — that rename is
        the audit's D1 and repointing the builders is NEU-1047's. What is being
        pinned here is the genre argument, and it is the same empty list either
        spine produces.
        """
        unmatched = await _show(session, "Never matched", tmdb_id=None)
        by_show = await q.genres_by_show(session, [unmatched.id])
        # `tvmaze_updated` is the one required field with no null: a TV Maze
        # artefact `ShowDetail` still carries, and NEU-1047's to resolve.
        renderable = t.Show(id=unmatched.id, name="Never matched", tvmaze_updated=0)

        summary = build_show_summary(renderable, by_show[unmatched.id], None, None)
        detail = build_show_detail(
            renderable, [], await q.genres_for_show(session, unmatched.id), None, None
        )

        assert summary.genres == []
        assert detail.genres == []

    async def test_it_is_simply_absent_from_a_filtered_result(self, session):
        vocabulary = await _seed_vocabulary(session)
        unmatched = await _show(session, "Never matched", tmdb_id=None)
        tagged = await _show(session, "Tagged", tmdb_id=1)
        await _tag(session, tagged, vocabulary["Drama"])

        matched = (await session.execute(q.shows_with_all_genres(["Drama"]))).scalars().all()

        assert unmatched.id not in matched
        assert matched == [tagged.id]


class TestHydration:
    async def test_it_covers_a_page_in_one_query_and_keys_every_show(self, session):
        vocabulary = await _seed_vocabulary(session)
        comic = await _show(session, "Comic", tmdb_id=1)
        await _tag(session, comic, vocabulary["Comedy"], vocabulary["Drama"])
        bare = await _show(session, "Bare", tmdb_id=2)

        by_show = await q.genres_by_show(session, [comic.id, bare.id])

        assert sorted(by_show[comic.id]) == ["Comedy", "Drama"]
        assert by_show[bare.id] == []

    async def test_no_shows_is_no_query(self, session):
        assert await q.genres_by_show(session, []) == {}

    async def test_detail_genres_are_alphabetical(self, session):
        vocabulary = await _seed_vocabulary(session)
        show = await _show(session, "Many", tmdb_id=1)
        await _tag(session, show, vocabulary["Western"], vocabulary["Comedy"], vocabulary["Drama"])

        assert [g.name for g in await q.genres_for_show(session, show.id)] == [
            "Comedy",
            "Drama",
            "Western",
        ]


class TestTheMirrorIsWhatTheIngestWrote:
    async def test_list_genres_reports_only_what_a_payload_named(self, session):
        """Before a full pass the list is a subset, which is why `GET /genres`
        is read out of the mirror rather than served from a constant."""
        session.add_all([m.Genre(tmdb_id=18, name="Drama"), m.Genre(tmdb_id=35, name="Comedy")])
        await session.flush()

        listed = await q.list_genres(session)

        assert [g.name for g in listed] == ["Comedy", "Drama"]

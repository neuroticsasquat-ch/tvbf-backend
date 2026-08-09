"""Writing a TMDB payload into `catalog` (NEU-1033).

The properties here are the ones an ingest is unsafe without: that a re-run
changes nothing, that user data's surrogate keys survive an upstream rewrite,
that a locally-authored row is never touched, and that pruning a season cannot
take watch history with it.
"""

from sqlalchemy import func, select

from tests.fixtures.tmdb.series_factory import (
    make_episode,
    make_season_detail,
    make_season_summary,
    make_series,
)
from tvbf.catalog import models as m
from tvbf.tmdb.api_payloads import TMDBSeasonDetail, TMDBSeries
from tvbf.tmdb.upsert import upsert_series_payload


async def _write(session, payload: dict, *, prune_seasons: bool = False, seasons=None) -> int:
    show_id = await upsert_series_payload(
        session,
        TMDBSeries.model_validate(payload),
        seasons=seasons,
        prune_seasons=prune_seasons,
    )
    await session.commit()
    return show_id


async def _count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _episodes(session, show_id: int) -> list[m.Episode]:
    return list(
        (
            await session.execute(
                select(m.Episode)
                .where(m.Episode.show_id == show_id)
                .order_by(m.Episode.season_number, m.Episode.episode_number)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


class TestConflictTargetIsTheUpstreamId:
    """The primary key is ours (ADR-0008) and upstream has never heard of it.

    Under TV Maze the two were the same column; getting this wrong would either
    write over the surrogate `app` references or insert a duplicate show on
    every pass.
    """

    async def test_a_migrated_show_keeps_its_preserved_id(self, session):
        """The migration seeds surrogates from TV Maze's ids so user rows never
        move. An upsert has to find that row by `tmdb_id` and leave the id be."""
        session.add(m.Show(id=93_469, tmdb_id=1396, name="Stale name"))
        await session.commit()

        show_id = await _write(session, make_series(1396))

        assert show_id == 93_469
        assert await _count(session, m.Show) == 1
        stored = (
            await session.execute(
                select(m.Show).where(m.Show.id == 93_469).execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert stored.name == "Show 1396"

    async def test_a_new_show_draws_a_surrogate_clear_of_the_migrated_range(self, session):
        show_id = await _write(session, make_series(1396))

        assert show_id >= 1_000_000

    async def test_re_upserting_the_same_payload_is_a_no_op(self, session):
        payload = make_series(1396, seasons=2, episodes_per_season=3)
        first = await _write(session, payload)
        before = {
            "seasons": [(s.id, s.tmdb_id) for s in await _seasons(session, first)],
            "episodes": [(e.id, e.tmdb_id) for e in await _episodes(session, first)],
        }

        second = await _write(session, payload)

        assert second == first
        assert await _count(session, m.Show) == 1
        assert [(s.id, s.tmdb_id) for s in await _seasons(session, first)] == before["seasons"]
        assert [(e.id, e.tmdb_id) for e in await _episodes(session, first)] == before["episodes"]


class TestLocallyAuthoredRows:
    """`tmdb_id IS NULL` is the sanctioned way to hold something TMDB does not
    list. Ingest must never overwrite or delete one — it is the fallback the
    migration's no-loss guarantee rests on."""

    async def test_a_locally_authored_show_is_not_overwritten(self, session):
        session.add(m.Show(name="Show 1396", tmdb_id=None))
        await session.commit()

        show_id = await _write(session, make_series(1396))

        rows = (
            (
                await session.execute(
                    select(m.Show).order_by(m.Show.id).execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        local = next(r for r in rows if r.tmdb_id is None)
        assert local.name == "Show 1396"
        assert local.id != show_id

    async def test_a_locally_authored_season_survives_the_prune(self, session):
        show_id = await _write(session, make_series(1396, seasons=1))
        session.add(m.Season(show_id=show_id, tmdb_id=None, season_number=99, name="Hand-added"))
        await session.commit()

        await _write(session, make_series(1396, seasons=1), prune_seasons=True)

        numbers = sorted(s.season_number for s in await _seasons(session, show_id))
        assert numbers == [1, 99]

    async def test_hand_added_rows_survive_a_namespace_rewrite(self, session):
        """The show-scoped namespaces replace their rows wholesale, and three of
        them can hold a locally-authored one — a video, an episode group or a
        creator whose upstream id is NULL. The delete has to step around those
        just as the season prune does; `ON CONFLICT` protects an upsert from a
        NULL for free, a `DELETE` does not."""
        payload = make_series(1396, videos={"results": []}, episode_groups={"results": []})
        show_id = await _write(session, payload)
        session.add_all(
            [
                m.Video(show_id=show_id, tmdb_id=None, name="Hand-added trailer"),
                m.EpisodeGroup(show_id=show_id, tmdb_id=None, name="Hand-added ordering"),
                m.ShowCreator(show_id=show_id, credit_id=None, name="Hand-added creator"),
            ]
        )
        await session.commit()

        await _write(session, payload)

        assert await _count(session, m.Video) == 1
        assert await _count(session, m.EpisodeGroup) == 1
        assert await _count(session, m.ShowCreator) == 1

    async def test_upstream_rows_are_still_replaced_around_them(self, session):
        payload = make_series(
            1396, videos={"results": [{"id": "abc", "key": "k", "site": "YouTube"}]}
        )
        show_id = await _write(session, payload)
        session.add(m.Video(show_id=show_id, tmdb_id=None, name="Hand-added"))
        await session.commit()

        await _write(session, make_series(1396, videos={"results": []}))

        remaining = (await session.execute(select(m.Video))).scalars().all()
        assert [v.name for v in remaining] == ["Hand-added"]


class TestEpisodeBatching:
    """Postgres caps bind parameters at 32,767 and an episode binds 15 columns,
    so an unbatched insert dies somewhere past ~2,180 episodes — on soaps, daily
    talk and news, which is to say on the long tail this catalog exists to
    cover."""

    async def test_a_show_with_more_than_2730_episodes_upserts(self, session):
        episodes = [make_episode(500_000 + n, 1, n) for n in range(1, 3_001)]
        payload = make_series(1396, append_seasons=False)
        payload["seasons"] = [make_season_summary(139_601, 1, episode_count=3_000)]
        payload["season/1"] = make_season_detail(1, episodes)

        show_id = await _write(session, payload)

        assert len(await _episodes(session, show_id)) == 3_000


class TestSeasonPrune:
    """ADR-0004 ports: the payload owns the season set when seasons were
    requested. Both halves of that are tested — what a prune removes, and what
    it is forbidden from taking with it."""

    async def test_it_is_opt_in(self, session):
        show_id = await _write(session, make_series(1396, seasons=2))

        await _write(session, make_series(1396, seasons=1))

        assert len(await _seasons(session, show_id)) == 2

    async def test_a_season_the_payload_stops_naming_is_deleted(self, session):
        show_id = await _write(session, make_series(1396, seasons=2))

        await _write(session, make_series(1396, seasons=1), prune_seasons=True)

        assert [s.season_number for s in await _seasons(session, show_id)] == [1]

    async def test_a_pruned_seasons_episodes_survive_with_a_null_season(self, session):
        """The acceptance criterion, and the reason `episode.season_id` is
        `ON DELETE SET NULL`: `app.user_episode_watch` cascades through the
        episode, so deleting one destroys watch history nothing can restore."""
        show_id = await _write(session, make_series(1396, seasons=2, episodes_per_season=2))
        orphaned = [e.id for e in await _episodes(session, show_id) if e.season_number == 2]

        await _write(
            session, make_series(1396, seasons=1, episodes_per_season=2), prune_seasons=True
        )

        stored = {e.id: e for e in await _episodes(session, show_id)}
        assert set(orphaned) <= set(stored)
        assert all(stored[eid].season_id is None for eid in orphaned)
        assert all(stored[eid].season_number == 2 for eid in orphaned)

    async def test_episodes_still_in_the_payload_re_point_onto_the_survivor(self, session):
        """Why the prune runs *between* the season write and the episode write.

        `catalog.season` carries no unique key on `(show_id, season_number)`, so
        two seasons can share a number. The episode write resolves its season
        from a live query — run after the prune, that query holds only
        survivors, and the phantom's episodes land on the one that remains.
        Prune afterwards and they would instead be bound to a row about to
        disappear and get nulled by the FK.
        """
        payload = make_series(1396, seasons=1, episodes_per_season=2)
        payload["seasons"] = [
            make_season_summary(139_601, 1),
            make_season_summary(139_602, 1, name="Duplicate numbering"),
        ]
        show_id = await _write(session, payload)
        assert len(await _seasons(session, show_id)) == 2

        survivor_payload = make_series(1396, seasons=1, episodes_per_season=2)
        survivor_payload["seasons"] = [make_season_summary(139_601, 1)]
        await _write(session, survivor_payload, prune_seasons=True)

        survivor = (await _seasons(session, show_id))[0]
        assert survivor.tmdb_id == 139_601
        assert all(e.season_id == survivor.id for e in await _episodes(session, show_id))

    async def test_an_authoritative_empty_season_set_clears_them(self, session):
        show_id = await _write(session, make_series(1396, seasons=2))
        payload = make_series(1396, append_seasons=False)
        payload["seasons"] = []

        await _write(session, payload, prune_seasons=True)

        assert await _seasons(session, show_id) == []


class TestSeasonIdentity:
    """An appended `season/N` block carries no `id` — measured. Identity has to
    come from `seasons[]`, matched by number."""

    async def test_a_season_takes_its_upstream_id_from_the_series_body(self, session):
        show_id = await _write(session, make_series(1396, seasons=2))

        assert [s.tmdb_id for s in await _seasons(session, show_id)] == [139_601, 139_602]

    async def test_overflow_seasons_are_merged_by_number(self, session):
        """A season too big for the 20-entry append budget arrives from
        `get_tv_season` instead, and the caller hands it over separately."""
        payload = make_series(1396, seasons=2, episodes_per_season=1, append_seasons=False)
        payload["season/1"] = make_season_detail(1, [make_episode(1, 1, 1)])
        overflow = TMDBSeasonDetail.model_validate(
            make_season_detail(2, [make_episode(2, 2, 1)], id=139_602)
        )

        show_id = await _write(session, payload, seasons=[overflow])

        episodes = await _episodes(session, show_id)
        assert [(e.season_number, e.episode_number) for e in episodes] == [(1, 1), (2, 1)]
        by_number = {s.season_number: s.id for s in await _seasons(session, show_id)}
        assert [e.season_id for e in episodes] == [by_number[1], by_number[2]]

    async def test_an_explicit_season_wins_over_the_appended_one(self, session):
        """A caller re-fetching one season hands over fresher data than the
        series call carried."""
        payload = make_series(1396, seasons=1, episodes_per_season=1)
        fresher = TMDBSeasonDetail.model_validate(
            make_season_detail(1, [make_episode(13_960_101, 1, 1, name="Corrected")])
        )

        show_id = await _write(session, payload, seasons=[fresher])

        assert [e.name for e in await _episodes(session, show_id)] == ["Corrected"]


class TestDerivedRuntime:
    """Audit D4: TMDB's own `episode_run_time` was empty for 6 of 7 sampled
    shows, so the scalar the SPA renders is the median of episode runtimes."""

    async def test_it_is_the_median_of_the_episode_runtimes(self, session):
        payload = make_series(1396, append_seasons=False)
        payload["seasons"] = [make_season_summary(139_601, 1)]
        payload["season/1"] = make_season_detail(
            1,
            [
                make_episode(1, 1, 1, runtime=22),
                make_episode(2, 1, 2, runtime=25),
                make_episode(3, 1, 3, runtime=90),
            ],
        )

        show_id = await _write(session, payload)

        assert (await _show(session, show_id)).runtime == 25

    async def test_a_payload_without_episodes_leaves_the_stored_value_alone(self, session):
        """The ordering constraint the audit calls out, stated as its failure:
        a delta fetched without seasons must not blank a runtime a full pass
        computed from every season it had in hand."""
        show_id = await _write(session, make_series(1396, seasons=1, episodes_per_season=2))
        assert (await _show(session, show_id)).runtime == 45

        await _write(session, make_series(1396, append_seasons=False))

        assert (await _show(session, show_id)).runtime == 45

    async def test_it_is_taken_over_every_mirrored_episode_not_just_this_payloads(self, session):
        """Which is why it is recomputed from the table. A delta carrying one
        season of many would otherwise overwrite the full pass's answer with a
        one-season one — staleness the audit accepts, a wrong value it does not.
        """
        full = make_series(1396, seasons=2, episodes_per_season=2, append_seasons=False)
        full["season/1"] = make_season_detail(
            1, [make_episode(1, 1, 1, runtime=22), make_episode(2, 1, 2, runtime=22)]
        )
        full["season/2"] = make_season_detail(
            2, [make_episode(3, 2, 1, runtime=22), make_episode(4, 2, 2, runtime=22)]
        )
        show_id = await _write(session, full)
        assert (await _show(session, show_id)).runtime == 22

        delta = make_series(1396, seasons=2, episodes_per_season=2, append_seasons=False)
        delta["season/2"] = make_season_detail(2, [make_episode(4, 2, 2, runtime=90)])
        await _write(session, delta)

        assert (await _show(session, show_id)).runtime == 22

    async def test_specials_do_not_count_toward_it(self, session):
        """D2 excludes season 0 from every other piece of completion math, and a
        four-minute webisode is not this show's runtime."""
        payload = make_series(1396, append_seasons=False)
        payload["seasons"] = [
            make_season_summary(139_600, 0),
            make_season_summary(139_601, 1),
        ]
        payload["season/0"] = make_season_detail(
            0, [make_episode(1, 0, 1, runtime=4), make_episode(2, 0, 2, runtime=4)]
        )
        payload["season/1"] = make_season_detail(1, [make_episode(3, 1, 1, runtime=42)])

        show_id = await _write(session, payload)

        assert (await _show(session, show_id)).runtime == 42


class TestAirPointers:
    """`last_episode_to_air` / `next_episode_to_air` are FKs to episodes this
    same write mints, so they are resolved last."""

    async def test_they_resolve_to_surrogate_ids(self, session):
        payload = make_series(1396, seasons=1, episodes_per_season=2)
        payload["last_episode_to_air"] = make_episode(13_960_102, 1, 2)

        show_id = await _write(session, payload)

        show = await _show(session, show_id)
        target = next(e for e in await _episodes(session, show_id) if e.tmdb_id == 13_960_102)
        assert show.last_episode_to_air_id == target.id
        assert show.next_episode_to_air_id is None

    async def test_an_episode_we_do_not_mirror_leaves_the_pointer_null(self, session):
        """These are a cache of TMDB's own answer; a pointer to a row we do not
        have would be worse than none."""
        payload = make_series(1396, seasons=1, episodes_per_season=1)
        payload["next_episode_to_air"] = make_episode(999_999, 4, 1)

        show_id = await _write(session, payload)

        assert (await _show(session, show_id)).next_episode_to_air_id is None


class TestNamespacesAreAuthoritativeOnlyWhenRequested:
    """The distinction `api_payloads` preserves, spent here: `None` means the
    caller did not ask, `[]` means upstream has none."""

    async def test_an_absent_namespace_leaves_existing_rows(self, session):
        await _write(
            session,
            make_series(1396, alternative_titles={"results": [{"iso_3166_1": "DE", "title": "X"}]}),
        )
        assert await _count(session, m.ShowAka) == 1

        await _write(session, make_series(1396))

        assert await _count(session, m.ShowAka) == 1

    async def test_an_empty_namespace_clears_them(self, session):
        await _write(
            session,
            make_series(1396, alternative_titles={"results": [{"iso_3166_1": "DE", "title": "X"}]}),
        )

        await _write(session, make_series(1396, alternative_titles={"results": []}))

        assert await _count(session, m.ShowAka) == 0

    async def test_screened_theatrically_is_not_cleared_by_a_fetch_that_did_not_ask(self, session):
        payload = make_series(1396, seasons=1, episodes_per_season=2)
        payload["screened_theatrically"] = {"results": [{"id": 13_960_101, "season_number": 1}]}
        show_id = await _write(session, payload)
        assert [e.screened_theatrically for e in await _episodes(session, show_id)] == [True, False]

        await _write(session, make_series(1396, seasons=1, episodes_per_season=2))

        assert [e.screened_theatrically for e in await _episodes(session, show_id)] == [True, False]


class TestNamespaceContents:
    """One assertion per namespace that lands in a table of its own — the
    audit's "a field with no column is a defect" criterion, one level down."""

    async def test_the_full_surface_lands(self, session):
        payload = make_series(
            1396,
            seasons=1,
            episodes_per_season=1,
            created_by=[{"id": 66633, "credit_id": "5254", "name": "Vince Gilligan", "gender": 2}],
            external_ids={"imdb_id": "tt0903747", "tvdb_id": 81189, "twitter_id": "BreakingBad"},
            content_ratings={
                "results": [{"iso_3166_1": "US", "rating": "TV-MA", "descriptors": ["violence"]}]
            },
            keywords={"results": [{"id": 271, "name": "drug dealer"}]},
            images={"posters": [{"file_path": "/p.jpg", "iso_639_1": "en", "width": 500}]},
            videos={"results": [{"id": "5759db2fc3a3683070007m", "key": "abc", "site": "YouTube"}]},
            episode_groups={
                "results": [{"id": "5f4a", "name": "Absolute", "type": 2, "group_count": 1}]
            },
            translations={
                "translations": [
                    {
                        "iso_639_1": "pt",
                        "iso_3166_1": "BR",
                        "name": "Português",
                        "english_name": "Portuguese",
                        "data": {"name": "Breaking Bad", "overview": "Um professor."},
                    }
                ]
            },
        )

        show_id = await _write(session, payload)

        show = await _show(session, show_id)
        assert (show.imdb_id, show.tvdb_id, show.twitter_id) == ("tt0903747", 81189, "BreakingBad")
        assert await _count(session, m.ShowCreator) == 1
        assert await _count(session, m.ShowGenre) == 1
        assert await _count(session, m.ShowNetwork) == 1
        assert await _count(session, m.ShowProductionCompany) == 1
        assert await _count(session, m.ShowKeyword) == 1
        assert await _count(session, m.ShowOriginCountry) == 1
        assert await _count(session, m.ShowProductionCountry) == 1
        assert await _count(session, m.ShowLanguage) == 1
        assert await _count(session, m.ShowSpokenLanguage) == 1
        assert await _count(session, m.Image) == 1
        assert await _count(session, m.Video) == 1
        assert await _count(session, m.EpisodeGroup) == 1
        rating = (await session.execute(select(m.ContentRating))).scalar_one()
        assert (rating.rating, rating.descriptors) == ("TV-MA", ["violence"])
        translation = (await session.execute(select(m.Translation))).scalar_one()
        assert (translation.language_code, translation.country_code) == ("pt", "BR")
        assert translation.name == "Breaking Bad"

    async def test_watch_providers_land_per_country_and_offer_type(self, session):
        payload = make_series(1396)
        payload["watch/providers"] = {
            "results": {
                "US": {
                    "link": "https://www.themoviedb.org/tv/1396/watch",
                    "flatrate": [
                        {"provider_id": 8, "provider_name": "Netflix", "display_priority": 3}
                    ],
                    "buy": [{"provider_id": 2, "provider_name": "Apple TV"}],
                }
            }
        }

        await _write(session, payload)

        rows = (
            (
                await session.execute(
                    select(m.ShowWatchProvider).order_by(m.ShowWatchProvider.offer_type)
                )
            )
            .scalars()
            .all()
        )
        assert [(r.country_code, r.offer_type) for r in rows] == [("US", "buy"), ("US", "flatrate")]
        assert all(r.link.endswith("/watch") for r in rows)
        assert await _count(session, m.WatchProvider) == 2

    async def test_one_credit_sent_twice_is_one_creator(self, session):
        """`uq_show_creator_show_credit` makes `credit_id` unique per show, so a
        duplicate upstream entry would abort the entire show on an integrity
        error rather than lose one row."""
        payload = make_series(
            1396,
            created_by=[
                {"id": 66633, "credit_id": "5254", "name": "Vince Gilligan"},
                {"id": 66633, "credit_id": "5254", "name": "Vince Gilligan"},
            ],
        )

        await _write(session, payload)

        assert await _count(session, m.ShowCreator) == 1

    async def test_a_season_names_its_own_networks(self, session):
        """Measured on the season payload and absent from the ticket's
        inventory (audit §8)."""
        payload = make_series(1396, seasons=1, episodes_per_season=1)
        payload["season/1"]["networks"] = [
            {"id": 213, "name": "Netflix", "logo_path": "/n.png", "origin_country": "US"}
        ]

        show_id = await _write(session, payload)

        joined = (await session.execute(select(m.SeasonNetwork))).scalars().all()
        assert len(joined) == 1
        network = (
            await session.execute(select(m.Network).where(m.Network.id == joined[0].network_id))
        ).scalar_one()
        assert network.name == "Netflix"
        # The series' own network is still interned separately.
        assert await _count(session, m.Network) == 2
        assert len(await _seasons(session, show_id)) == 1


class TestSharedVocabularies:
    """`country` and `language` are static external vocabularies with natural
    keys, populated as a side effect of whichever payload happens to name them."""

    async def test_a_bare_code_does_not_blank_a_name_already_known(self, session):
        await _write(session, make_series(1396))
        assert (await session.execute(select(m.Country.name))).scalar_one() == (
            "United States of America"
        )

        await _write(
            session,
            make_series(1399, production_countries=[], origin_country=["US"]),
        )

        names = (await session.execute(select(m.Country.name))).scalars().all()
        assert names == ["United States of America"]

    async def test_a_language_is_named_from_spoken_languages_only(self, session):
        payload = make_series(1396, languages=["en", "de"])

        await _write(session, payload)

        rows = {
            r.iso_639_1: r.english_name
            for r in (await session.execute(select(m.Language))).scalars()
        }
        assert rows == {"en": "English", "de": None}


async def _show(session, show_id: int) -> m.Show:
    return (
        await session.execute(
            select(m.Show).where(m.Show.id == show_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _seasons(session, show_id: int) -> list[m.Season]:
    return list(
        (
            await session.execute(
                select(m.Season)
                .where(m.Season.show_id == show_id)
                .order_by(m.Season.season_number, m.Season.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )

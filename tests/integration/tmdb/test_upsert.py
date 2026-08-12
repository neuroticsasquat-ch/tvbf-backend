"""Writing a TMDB payload into `catalog` (NEU-1033).

The properties here are the ones an ingest is unsafe without: that a re-run
changes nothing, that user data's surrogate keys survive an upstream rewrite,
that a locally-authored row is never touched, and that pruning a season cannot
take watch history with it.
"""

import logging

from sqlalchemy import func, select

from tests.fixtures.tmdb.series_factory import (
    make_aggregate_credits,
    make_cast_member,
    make_crew_member,
    make_episode,
    make_episode_crew_member,
    make_guest_star,
    make_job,
    make_role,
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


class TestShowCredits:
    """`aggregate_credits` into `person` / `character` / `crew_role` / `show_cast`
    / `show_crew` (NEU-1039).

    The properties are the ticket's acceptance criteria plus the two the writer
    would fail silently on: that a re-ingest adds nothing, and that a blank
    character is stored as no character rather than as one nobody played.
    """

    async def test_episode_count_is_stored_per_role_and_orders_the_cast(self, session):
        """The behaviour change the ticket exists for. TV Maze gave us billing
        order as a proxy for appearances; `roles[].episode_count` is the count
        itself, so the two orderings are deliberately opposed here — a cast
        listed by `order` would come back in the wrong sequence."""
        show_id = await _write(
            session,
            make_series(
                1396,
                aggregate_credits=make_aggregate_credits(
                    cast=[
                        make_cast_member(1, "Lead", [make_role("Walter", 62)], order=0),
                        make_cast_member(2, "Recurring", [make_role("Saul", 43)], order=5),
                        make_cast_member(3, "Guest", [make_role("Gale", 8)], order=1),
                    ]
                ),
            ),
        )

        rows = await _cast(session, show_id)
        assert [(r.name, c.episode_count) for c, _, r in rows] == [
            ("Lead", 62),
            ("Recurring", 43),
            ("Guest", 8),
        ]
        assert [c.billing_order for c, _, _ in rows] == [0, 5, 1]

    async def test_a_role_with_no_episode_count_sorts_last_not_first(self, session):
        """`episode_count` is nullable, and Postgres sorts NULLs first under a
        plain `DESC` — which would put a role upstream said nothing about ahead
        of the show's lead. The stored value is right either way; the ordering is
        what the ticket's acceptance criterion is about."""
        show_id = await _write(
            session,
            make_series(
                1396,
                aggregate_credits=make_aggregate_credits(
                    cast=[
                        make_cast_member(
                            1, "Unknown Size", [make_role("Extra", 0) | {"episode_count": None}]
                        ),
                        make_cast_member(2, "Lead", [make_role("Walter", 62)]),
                    ]
                ),
            ),
        )

        assert [person.name for _, _, person in await _cast(session, show_id)] == [
            "Lead",
            "Unknown Size",
        ]

    async def test_one_person_in_two_roles_is_two_credits_and_one_person(self, session):
        show_id = await _write(
            session,
            make_series(
                1396,
                aggregate_credits=make_aggregate_credits(
                    cast=[
                        make_cast_member(
                            1,
                            "Tatiana Maslany",
                            [make_role("Sarah", 50), make_role("Helena", 30)],
                        )
                    ]
                ),
            ),
        )

        rows = await _cast(session, show_id)
        assert [
            (char.name if char else None, credit.episode_count) for credit, char, _ in rows
        ] == [("Sarah", 50), ("Helena", 30)]
        assert len({credit.person_id for credit, _, _ in rows}) == 1
        assert await _count(session, m.Person) == 1
        # The entry-level total, denormalised onto each of the person's rows.
        assert {credit.total_episode_count for credit, _, _ in rows} == {80}

    async def test_two_people_as_one_character_is_two_credits_and_one_character(self, session):
        """Recasting, which per-show interning exists to preserve — 2,621
        characters in prod are played by more than one person."""
        show_id = await _write(
            session,
            make_series(
                1396,
                aggregate_credits=make_aggregate_credits(
                    cast=[
                        make_cast_member(1, "First Actor", [make_role("Doctor", 40)]),
                        make_cast_member(2, "Second Actor", [make_role("Doctor", 20)]),
                    ]
                ),
            ),
        )

        rows = await _cast(session, show_id)
        assert len(rows) == 2
        assert len({credit.character_id for credit, _, _ in rows}) == 1
        assert await _count(session, m.Character) == 1

    async def test_the_same_character_name_on_two_shows_is_two_characters(self, session):
        """The other half of per-show interning: cross-show identity is the one
        thing the narrowing gives up, and it has to give it up consistently."""
        credits = make_aggregate_credits(cast=[make_cast_member(1, "Actor", [make_role("Doc", 5)])])

        await _write(session, make_series(1396, aggregate_credits=credits))
        await _write(session, make_series(1399, aggregate_credits=credits))

        assert await _count(session, m.Character) == 2
        assert await _count(session, m.Person) == 1

    async def test_a_blank_character_is_stored_as_no_character(self, session):
        """1 of 7,629 sampled roles. Interning `''` would invent a role nobody
        played, and NOT NULL would abort the pass on that one row."""
        show_id = await _write(
            session,
            make_series(
                1396,
                aggregate_credits=make_aggregate_credits(
                    cast=[make_cast_member(1, "Themselves", [make_role("", 12)])]
                ),
            ),
        )

        rows = await _cast(session, show_id)
        assert [credit.character_id for credit, _, _ in rows] == [None]
        assert await _count(session, m.Character) == 0

    async def test_crew_is_one_row_per_job_against_a_shared_role_vocabulary(self, session):
        show_id = await _write(
            session,
            make_series(
                1396,
                aggregate_credits=make_aggregate_credits(
                    crew=[
                        make_crew_member(
                            9,
                            "Vince Gilligan",
                            "Writing",
                            [make_job("Writer", 29), make_job("Story", 3)],
                        ),
                        make_crew_member(
                            10, "Michelle MacLaren", "Directing", [make_job("Director", 11)]
                        ),
                    ]
                ),
            ),
        )

        rows = await _crew(session, show_id)
        assert [(role.department, role.job, credit.episode_count) for credit, role in rows] == [
            ("Writing", "Writer", 29),
            ("Directing", "Director", 11),
            ("Writing", "Story", 3),
        ]
        assert await _count(session, m.CrewRole) == 3

    async def test_a_crew_role_is_interned_across_shows(self, session):
        """`crew_role` is a TMDB-wide vocabulary, unlike `character` — a second
        show naming Directing/Director must reuse the row, not add one."""
        credits = make_aggregate_credits(
            crew=[make_crew_member(9, "A Director", "Directing", [make_job("Director", 2)])]
        )

        await _write(session, make_series(1396, aggregate_credits=credits))
        await _write(session, make_series(1399, aggregate_credits=credits))

        assert await _count(session, m.CrewRole) == 1
        assert await _count(session, m.ShowCrew) == 2

    async def test_re_ingesting_the_same_show_duplicates_nothing(self, session):
        """Neither credit table carries a unique key — `show_cast` deliberately
        so, since refresh is delete-then-insert. Nothing but this test catches a
        refresh that stopped being total."""
        payload = make_series(
            1396,
            aggregate_credits=make_aggregate_credits(
                cast=[make_cast_member(1, "Lead", [make_role("Walter", 62)])],
                crew=[make_crew_member(9, "Writer Person", "Writing", [make_job("Writer", 29)])],
            ),
        )
        await _write(session, payload)

        await _write(session, payload)

        assert await _count(session, m.ShowCast) == 1
        assert await _count(session, m.ShowCrew) == 1
        assert await _count(session, m.Person) == 2
        assert await _count(session, m.Character) == 1
        assert await _count(session, m.CrewRole) == 1

    async def test_a_dropped_credit_is_removed_on_the_next_pass(self, session):
        show_id = await _write(
            session,
            make_series(
                1396,
                aggregate_credits=make_aggregate_credits(
                    cast=[
                        make_cast_member(1, "Stays", [make_role("Walter", 62)]),
                        make_cast_member(2, "Goes", [make_role("Gale", 8)]),
                    ]
                ),
            ),
        )

        await _write(
            session,
            make_series(
                1396,
                aggregate_credits=make_aggregate_credits(
                    cast=[make_cast_member(1, "Stays", [make_role("Walter", 62)])]
                ),
            ),
        )

        rows = await _cast(session, show_id)
        assert [person.name for _, _, person in rows] == ["Stays"]
        # The dropped credit's character stays interned. Stated rather than
        # asserted-away: `catalog.character` is also the interning target for
        # episode guest cast (NEU-1040), written from a different payload, so a
        # prune scoped to show cast would delete rows an episode credit needs.
        assert await _count(session, m.Character) == 2

    async def test_a_crew_entry_missing_its_department_is_skipped_not_fatal(self, session):
        """`crew_role` is NOT NULL in both columns, so an unnamed pair cannot be
        interned. Measured never to happen — and if the measurement goes stale,
        one malformed entry must cost that credit rather than the show."""
        show_id = await _write(
            session,
            make_series(
                1396,
                aggregate_credits=make_aggregate_credits(
                    crew=[
                        make_crew_member(9, "No Department", "", [make_job("Writer", 4)]),
                        make_crew_member(
                            10, "Real Director", "Directing", [make_job("Director", 6)]
                        ),
                    ]
                ),
            ),
        )

        assert [role.job for _, role in await _crew(session, show_id)] == ["Director"]
        assert await _count(session, m.CrewRole) == 1

    async def test_a_person_upserts_on_tmdb_id_and_keeps_their_surrogate(self, session):
        """`catalog.person` conflict-targets `tmdb_id`, an acceptance criterion
        in its own right: an id that churned on every pass would break every
        credit row pointing at it mid-run."""
        first_id = await _write(
            session,
            make_series(
                1396,
                aggregate_credits=make_aggregate_credits(
                    cast=[make_cast_member(1, "Stale Name", [make_role("Walter", 1)])]
                ),
            ),
        )
        before = (await _cast(session, first_id))[0][2].id

        await _write(
            session,
            make_series(
                1396,
                aggregate_credits=make_aggregate_credits(
                    cast=[make_cast_member(1, "Bryan Cranston", [make_role("Walter", 62)])]
                ),
            ),
        )

        person = (await _cast(session, first_id))[0][2]
        assert person.id == before
        assert person.name == "Bryan Cranston"
        assert await _count(session, m.Person) == 1

    async def test_an_absent_namespace_leaves_the_credits_alone(self, session):
        """Same rule as every other namespace: a delta fetched without
        `aggregate_credits` must not empty a show's cast."""
        await _write(
            session,
            make_series(
                1396,
                aggregate_credits=make_aggregate_credits(
                    cast=[make_cast_member(1, "Lead", [make_role("Walter", 62)])]
                ),
            ),
        )

        await _write(session, make_series(1396))

        assert await _count(session, m.ShowCast) == 1

    async def test_an_empty_namespace_clears_the_credits(self, session):
        await _write(
            session,
            make_series(
                1396,
                aggregate_credits=make_aggregate_credits(
                    cast=[make_cast_member(1, "Lead", [make_role("Walter", 62)])]
                ),
            ),
        )

        await _write(session, make_series(1396, aggregate_credits=make_aggregate_credits()))

        assert await _count(session, m.ShowCast) == 0


class TestEpisodeCredits:
    """Episode `guest_stars` / `crew` into `episode_guest_cast` / `episode_crew`
    (NEU-1040).

    These rode a dedicated ~29-hour pass under TV Maze (ADR-0003) and ride the
    season payload here, so the properties that matter are about grain rather
    than about scheduling: that an episode's director lands as crew and not as
    guest cast, that a returning guest resolves to the show's character rather
    than a per-episode copy, and that a re-ingest neither duplicates nor drops.
    """

    async def test_a_director_is_crew_and_a_guest_credit_is_cast(self, session):
        """The ticket's acceptance criterion, and the one confusion the two flat
        lists invite: they are shaped almost identically."""
        show_id = await _write(session, _series_with_episode_credits())

        guests = await _guest_cast(session, show_id)
        crew = await _episode_crew(session, show_id)

        assert [(g.Person.name, g.Character.name) for g in guests] == [("Guest", "Victim")]
        assert [(c.Person.name, c.CrewRole.department, c.CrewRole.job) for c in crew] == [
            ("Director", "Directing", "Director")
        ]

    async def test_credit_order_is_stored_per_episode(self, session):
        show_id = await _write(
            session,
            _series_with_episode_credits(
                guest_stars=[
                    make_guest_star(11, "Second", "Bystander", order=1),
                    make_guest_star(12, "First", "Victim", order=0),
                ]
            ),
        )

        guests = await _guest_cast(session, show_id)

        assert [(g.Person.name, g.EpisodeGuestCast.credit_order) for g in guests] == [
            ("First", 0),
            ("Second", 1),
        ]

    async def test_a_guest_character_interns_against_the_show_not_the_episode(self, session):
        """A returning guest is the same character. Interning per episode would
        mint a row per appearance and break the character page the show cast
        already fills."""
        episodes = [
            make_episode(1, 1, 1, guest_stars=[make_guest_star(9, "Guest", "Tuco")]),
            make_episode(2, 1, 2, guest_stars=[make_guest_star(9, "Guest", "Tuco")]),
        ]
        show_id = await _write(session, _series_with_episodes(episodes))

        guests = await _guest_cast(session, show_id)

        assert len(guests) == 2
        assert len({g.Character.id for g in guests}) == 1
        assert await _count(session, m.Character) == 1

    async def test_a_guest_shares_the_character_the_show_cast_interned(self, session):
        """The two writers run off different payloads against one `character`
        table, which is why `_write_credits` declines to prune it."""
        show_id = await _write(
            session,
            _series_with_episodes(
                [make_episode(1, 1, 1, guest_stars=[make_guest_star(9, "Guest", "Tuco")])],
                aggregate_credits=make_aggregate_credits(
                    cast=[make_cast_member(9, "Guest", [make_role("Tuco", 4)])]
                ),
            ),
        )

        guests = await _guest_cast(session, show_id)
        cast = await _cast(session, show_id)

        assert await _count(session, m.Character) == 1
        show_character = cast[0][1]
        assert show_character is not None
        assert guests[0].Character.id == show_character.id
        # One person, credited at both grains.
        assert await _count(session, m.Person) == 1

    async def test_episode_crew_shares_the_show_crew_vocabulary(self, session):
        """One `crew_role` lookup, not `tvmaze`'s two — measured 100% overlap."""
        show_id = await _write(
            session,
            _series_with_episodes(
                [
                    make_episode(
                        1,
                        1,
                        1,
                        crew=[make_episode_crew_member(5, "Michelle", "Directing", "Director")],
                    )
                ],
                aggregate_credits=make_aggregate_credits(
                    crew=[
                        make_crew_member(6, "Vince", "Directing", [make_job("Director", 9)]),
                    ]
                ),
            ),
        )

        assert await _count(session, m.CrewRole) == 1
        episode_crew = await _episode_crew(session, show_id)
        show_crew = await _crew(session, show_id)
        assert episode_crew[0].CrewRole.id == show_crew[0][1].id

    async def test_one_person_in_two_crew_roles_on_one_episode_is_two_rows(self, session):
        """Three-part uniqueness. Two-part on `(episode, person)` would silently
        drop the second — one person holds more than one role on 36 of 1,043
        sampled episodes."""
        show_id = await _write(
            session,
            _series_with_episodes(
                [
                    make_episode(
                        1,
                        1,
                        1,
                        crew=[
                            make_episode_crew_member(5, "Vince", "Directing", "Director"),
                            make_episode_crew_member(5, "Vince", "Writing", "Writer"),
                        ],
                    )
                ]
            ),
        )

        crew = await _episode_crew(session, show_id)

        assert [(c.CrewRole.department, c.CrewRole.job) for c in crew] == [
            ("Directing", "Director"),
            ("Writing", "Writer"),
        ]
        assert await _count(session, m.Person) == 1

    async def test_two_people_as_one_character_on_one_episode_is_two_rows(self, session):
        """The other half of three-part uniqueness — one character played by two
        people on 17 of 1,043 sampled episodes."""
        show_id = await _write(
            session,
            _series_with_episodes(
                [
                    make_episode(
                        1,
                        1,
                        1,
                        guest_stars=[
                            make_guest_star(1, "Actor A", "Twin"),
                            make_guest_star(2, "Actor B", "Twin"),
                        ],
                    )
                ]
            ),
        )

        guests = await _guest_cast(session, show_id)

        assert [g.Person.name for g in guests] == ["Actor A", "Actor B"]
        assert await _count(session, m.Character) == 1

    async def test_a_blank_character_is_stored_as_no_character(self, session):
        """`character_id` is nullable and `uq_egc_episode_person_character` is
        `NULLS NOT DISTINCT`, so the row has to survive *and* stay unique."""
        show_id = await _write(
            session,
            _series_with_episodes(
                [make_episode(1, 1, 1, guest_stars=[make_guest_star(1, "Extra", "  ")])]
            ),
        )

        guests = await _guest_cast(session, show_id)

        assert len(guests) == 1
        assert guests[0].EpisodeGuestCast.character_id is None
        assert await _count(session, m.Character) == 0

    async def test_re_ingesting_the_same_show_duplicates_nothing(self, session):
        """`NULLS NOT DISTINCT` earns its keep here: a null-character guest would
        never conflict under the default and would accumulate a copy per pass."""
        payload = _series_with_episodes(
            [
                make_episode(
                    1,
                    1,
                    1,
                    guest_stars=[
                        make_guest_star(1, "Guest", "Victim"),
                        make_guest_star(2, "Extra", ""),
                    ],
                    crew=[make_episode_crew_member(5, "Vince", "Directing", "Director")],
                )
            ]
        )
        await _write(session, payload)

        await _write(session, payload)

        assert await _count(session, m.EpisodeGuestCast) == 2
        assert await _count(session, m.EpisodeCrew) == 1

    async def test_a_dropped_credit_is_removed_on_the_next_pass(self, session):
        show_id = await _write(
            session,
            _series_with_episodes(
                [
                    make_episode(
                        1,
                        1,
                        1,
                        guest_stars=[
                            make_guest_star(1, "Guest", "Victim"),
                            make_guest_star(2, "Cut", "Bystander"),
                        ],
                    )
                ]
            ),
        )

        await _write(
            session,
            _series_with_episodes(
                [make_episode(1, 1, 1, guest_stars=[make_guest_star(1, "Guest", "Victim")])]
            ),
        )

        assert [g.Person.name for g in await _guest_cast(session, show_id)] == ["Guest"]
        assert await _count(session, m.EpisodeGuestCast) == 1

    async def test_an_episode_without_the_key_keeps_the_credits_it_had(self, session):
        """ "Absent" and "empty" differ per episode, exactly as they do per
        namespace. A payload that never mentioned `guest_stars` must not clear a
        guest list a season fetch established."""
        show_id = await _write(
            session,
            _series_with_episodes(
                [make_episode(1, 1, 1, guest_stars=[make_guest_star(1, "Guest", "Victim")])]
            ),
        )
        bare = make_episode(1, 1, 1)
        del bare["guest_stars"]
        del bare["crew"]

        await _write(session, _series_with_episodes([bare]))

        assert await _count(session, m.EpisodeGuestCast) == 1
        assert len(await _guest_cast(session, show_id)) == 1

    async def test_an_empty_list_clears_the_credits(self, session):
        show_id = await _write(
            session,
            _series_with_episodes(
                [make_episode(1, 1, 1, guest_stars=[make_guest_star(1, "Guest", "Victim")])]
            ),
        )

        await _write(session, _series_with_episodes([make_episode(1, 1, 1)]))

        assert await _count(session, m.EpisodeGuestCast) == 0
        assert await _guest_cast(session, show_id) == []

    async def test_one_season_re_fetched_leaves_another_seasons_credits_alone(self, session):
        """The delete is scoped to the episodes the payload carried. A show-wide
        one would empty every season a narrow re-fetch did not include."""
        payload = make_series(1396, seasons=2, episodes_per_season=1)
        for number in (1, 2):
            payload[f"season/{number}"]["episodes"] = [
                make_episode(
                    number,
                    number,
                    1,
                    guest_stars=[make_guest_star(number, f"Guest {number}", "Victim")],
                )
            ]
        show_id = await _write(session, payload)

        narrow = make_series(1396, seasons=2, episodes_per_season=1, append_seasons=False)
        narrow["season/1"] = make_season_detail(
            1, [make_episode(1, 1, 1, guest_stars=[make_guest_star(1, "Guest 1", "Survivor")])]
        )
        await _write(session, narrow)

        guests = await _guest_cast(session, show_id)
        assert sorted(g.Character.name for g in guests) == ["Survivor", "Victim"]

    # --- an episode credit that names nobody (NEU-1128) -------------------
    #
    # TMDB sends entries with no `id` and no `name`. Before NEU-1128 each one
    # failed `TMDBEpisode`, so the *series* payload failed and the whole show was
    # lost — 12 of the first ~4,000 shows in NEU-1127's production backfill, and
    # the same wall the daily delta hits.

    async def test_a_guest_star_with_no_person_is_skipped_not_fatal(self, session):
        """AC: the payload parses and the episode's other credits still land."""
        nameless = make_guest_star(1, "Ignored", "Ghost")
        del nameless["id"]
        del nameless["name"]
        show_id = await _write(
            session,
            _series_with_episodes(
                [
                    make_episode(
                        1, 1, 1, guest_stars=[nameless, make_guest_star(2, "Real", "Victim")]
                    )
                ]
            ),
        )

        assert [g.Person.name for g in await _guest_cast(session, show_id)] == ["Real"]

    async def test_a_crew_entry_with_no_person_is_skipped_not_fatal(self, session):
        """The half the production traceback actually landed on."""
        nameless = make_episode_crew_member(5, "Ignored", "Directing", "Director")
        del nameless["id"]
        del nameless["name"]
        show_id = await _write(
            session,
            _series_with_episodes(
                [
                    make_episode(
                        1,
                        1,
                        1,
                        crew=[
                            nameless,
                            make_episode_crew_member(6, "Michelle", "Writing", "Writer"),
                        ],
                    )
                ]
            ),
        )

        assert [c.Person.name for c in await _episode_crew(session, show_id)] == ["Michelle"]

    async def test_an_episode_whose_every_guest_was_skipped_keeps_the_ones_it_had(self, session):
        """AC: all-skipped holds the episode out of the refresh rather than emptying it.

        The rule crew has carried since NEU-1040, which guest cast needed only
        once an entry here became skippable. Without it the episode reads as
        `guest_stars: []` — upstream stating a zero — and a guest list we simply
        failed to parse is destroyed.
        """
        show_id = await _write(
            session,
            _series_with_episodes(
                [make_episode(1, 1, 1, guest_stars=[make_guest_star(1, "Guest", "Victim")])]
            ),
        )
        nameless = make_guest_star(2, "Ignored", "Ghost")
        del nameless["id"]
        del nameless["name"]

        await _write(
            session, _series_with_episodes([make_episode(1, 1, 1, guest_stars=[nameless])])
        )

        assert [g.Person.name for g in await _guest_cast(session, show_id)] == ["Guest"]

    async def test_a_skipped_credit_is_logged_with_its_grain_and_upstream_ids(
        self, session, caplog
    ):
        """AC: skipped *and logged*, naming upstream's ids.

        The grain is what makes the line actionable — it is the one thing that
        says which of the two refresh scopes just held an episode back — and the
        ids are upstream's, because a surrogate cannot be looked up in the
        payload the warning is about.
        """
        nameless = make_guest_star(1, "Ignored", "Ghost")
        del nameless["id"]
        del nameless["name"]

        with caplog.at_level(logging.WARNING, logger="tvbf.tmdb.upsert"):
            await _write(
                session,
                _series_with_episodes([make_episode(4242, 1, 1, guest_stars=[nameless])]),
            )

        assert "skipped guest credit with no person" in caplog.text
        assert "TMDB episode 4242 (S01E01)" in caplog.text

    async def test_an_explicitly_empty_guest_list_still_clears(self, session):
        """AC: the guard must not swallow upstream's genuine zero.

        The pair to the test above, and the reason the scope keys on
        `not ep.guest_stars` rather than on the usable set alone.
        """
        show_id = await _write(
            session,
            _series_with_episodes(
                [make_episode(1, 1, 1, guest_stars=[make_guest_star(1, "Guest", "Victim")])]
            ),
        )

        await _write(session, _series_with_episodes([make_episode(1, 1, 1, guest_stars=[])]))

        assert await _guest_cast(session, show_id) == []

    async def test_a_crew_entry_missing_its_department_is_skipped_not_fatal(self, session):
        """`crew_role` is NOT NULL in both columns. Measured never to happen — so
        one malformed entry must cost that row and not the show's whole payload."""
        show_id = await _write(
            session,
            _series_with_episodes(
                [
                    make_episode(
                        1,
                        1,
                        1,
                        crew=[
                            make_episode_crew_member(5, "Vince", "", "Director"),
                            make_episode_crew_member(6, "Michelle", "Writing", "Writer"),
                        ],
                    )
                ]
            ),
        )

        crew = await _episode_crew(session, show_id)

        assert [(c.Person.name, c.CrewRole.job) for c in crew] == [("Michelle", "Writer")]

    async def test_an_episode_whose_crew_is_entirely_unparseable_keeps_what_it_had(self, session):
        """The skip-and-log promise is that one malformed entry costs that row
        and not the payload. It only holds if an episode that lost *every* entry
        is held out of the refresh — otherwise it stays in scope for the delete,
        contributes nothing to the insert, and a payload we could not read
        silently empties crew a payload we could read had stored."""
        show_id = await _write(
            session,
            _series_with_episodes(
                [
                    make_episode(
                        1,
                        1,
                        1,
                        crew=[make_episode_crew_member(5, "Vince", "Directing", "Director")],
                    )
                ]
            ),
        )

        await _write(
            session,
            _series_with_episodes(
                [make_episode(1, 1, 1, crew=[make_episode_crew_member(5, "Vince", "", "")])]
            ),
        )

        crew = await _episode_crew(session, show_id)
        assert [(c.Person.name, c.CrewRole.job) for c in crew] == [("Vince", "Director")]

    async def test_an_explicitly_empty_crew_list_still_clears(self, session):
        """The other side of that guard: `[]` is upstream stating a zero, which
        is a fact worth storing, not a payload we failed to read."""
        show_id = await _write(
            session,
            _series_with_episodes(
                [
                    make_episode(
                        1,
                        1,
                        1,
                        crew=[make_episode_crew_member(5, "Vince", "Directing", "Director")],
                    )
                ]
            ),
        )

        await _write(session, _series_with_episodes([make_episode(1, 1, 1)]))

        assert await _episode_crew(session, show_id) == []

    async def test_an_episode_arriving_twice_credits_it_once(self, session):
        """An appended `season/N` and a `get_tv_season` overflow can both carry
        the same episode. It resolves to one surrogate id, so its guest list must
        be spent once rather than merged with itself."""
        episode = make_episode(1, 1, 1, guest_stars=[make_guest_star(1, "Guest", "Victim")])
        payload = make_series(1396, seasons=1, episodes_per_season=1)
        payload["season/1"]["episodes"] = [episode]

        show_id = await _write(
            session,
            payload,
            seasons=[TMDBSeasonDetail.model_validate(make_season_detail(1, [episode]))],
        )

        assert await _count(session, m.EpisodeGuestCast) == 1
        assert len(await _guest_cast(session, show_id)) == 1


def _series_with_episodes(episodes: list[dict], **overrides) -> dict:
    """A one-season show whose appended `season/1` carries exactly `episodes`."""
    payload = make_series(1396, seasons=1, episodes_per_season=1, **overrides)
    payload["season/1"]["episodes"] = episodes
    return payload


def _series_with_episode_credits(**episode_overrides) -> dict:
    return _series_with_episodes(
        [
            make_episode(
                1,
                1,
                1,
                **{
                    "guest_stars": [make_guest_star(1, "Guest", "Victim")],
                    "crew": [make_episode_crew_member(2, "Director", "Directing", "Director")],
                    **episode_overrides,
                },
            )
        ]
    )


async def _guest_cast(session, show_id: int):
    """This episode-grain cast in credit order — the shape a read path wants."""
    rows = await session.execute(
        select(m.EpisodeGuestCast, m.Character, m.Person)
        .join(m.Person, m.Person.id == m.EpisodeGuestCast.person_id)
        .outerjoin(m.Character, m.Character.id == m.EpisodeGuestCast.character_id)
        .join(m.Episode, m.Episode.id == m.EpisodeGuestCast.episode_id)
        .where(m.Episode.show_id == show_id)
        .order_by(m.EpisodeGuestCast.credit_order.asc().nullslast(), m.Person.name)
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


async def _episode_crew(session, show_id: int):
    rows = await session.execute(
        select(m.EpisodeCrew, m.CrewRole, m.Person)
        .join(m.Person, m.Person.id == m.EpisodeCrew.person_id)
        .join(m.CrewRole, m.CrewRole.id == m.EpisodeCrew.role_id)
        .join(m.Episode, m.Episode.id == m.EpisodeCrew.episode_id)
        .where(m.Episode.show_id == show_id)
        .order_by(m.CrewRole.department, m.CrewRole.job)
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


async def _cast(session, show_id: int) -> list[tuple[m.ShowCast, m.Character | None, m.Person]]:
    """This show's cast in the order the read path will serve it.

    **`NULLS LAST` is not decoration.** `episode_count` is nullable — nothing
    guarantees TMDB states one — and Postgres sorts NULLs *first* under a plain
    `DESC`, which would put a role of unknown size ahead of the show's lead. This
    helper is the shape the cutover read path should copy, so it spells the
    ordering out rather than leaving the trap for it to inherit.
    """
    rows = await session.execute(
        select(m.ShowCast, m.Character, m.Person)
        .join(m.Person, m.Person.id == m.ShowCast.person_id)
        .outerjoin(m.Character, m.Character.id == m.ShowCast.character_id)
        .where(m.ShowCast.show_id == show_id)
        .order_by(m.ShowCast.episode_count.desc().nullslast())
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


async def _crew(session, show_id: int) -> list[tuple[m.ShowCrew, m.CrewRole]]:
    rows = await session.execute(
        select(m.ShowCrew, m.CrewRole)
        .join(m.CrewRole, m.CrewRole.id == m.ShowCrew.role_id)
        .where(m.ShowCrew.show_id == show_id)
        .order_by(m.ShowCrew.episode_count.desc().nullslast())
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


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

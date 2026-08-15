"""Parsing TMDB payloads (NEU-1033).

These are the properties an ingest breaks on if they are wrong, not a tour of
the fields. Every fixture is built from real upstream key names — see
`tests/fixtures/tmdb/series_factory.py` for why that is load-bearing.
"""

import pytest
from pydantic import ValidationError

from tests.fixtures.tmdb.series_factory import (
    make_aggregate_credits,
    make_cast_member,
    make_crew_member,
    make_episode,
    make_guest_star,
    make_job,
    make_role,
    make_season_detail,
    make_series,
)
from tvbf.tmdb.api_payloads import (
    TMDBAggregateCast,
    TMDBAggregateCredits,
    TMDBEpisode,
    TMDBRole,
    TMDBSeasonDetail,
    TMDBSeasonSummary,
    TMDBSeries,
    TMDBWatchProviders,
)


class TestEmptyStringCoercion:
    """TMDB sends `""` for an unknown date in the same places it sends `null`.

    The `BeforeValidator` pattern ports straight from TV Maze; without it,
    ingestion fails on real data rather than storing a null.
    """

    @pytest.mark.parametrize("blank", ["", None])
    def test_a_blank_air_date_parses_to_none(self, blank):
        episode = TMDBEpisode.model_validate(
            {"id": 1, "season_number": 1, "episode_number": 1, "air_date": blank}
        )

        assert episode.air_date is None

    @pytest.mark.parametrize("field", ["first_air_date", "last_air_date"])
    def test_blank_series_dates_parse_to_none(self, field):
        payload = make_series()
        payload[field] = ""

        series = TMDBSeries.model_validate(payload)

        assert getattr(series, field) is None

    def test_a_real_date_still_parses(self):
        series = TMDBSeries.model_validate(make_series())

        assert series.first_air_date is not None
        assert series.first_air_date.isoformat() == "2008-01-20"

    def test_a_blank_video_timestamp_parses_to_none(self):
        """The only timestamp TMDB returns, and it is nested inside the series
        payload — so a bare `datetime | None` would fail the whole show's parse
        over one video."""
        series = TMDBSeries.model_validate(
            make_series(videos={"results": [{"id": "abc", "published_at": ""}]})
        )

        assert series.videos is not None
        assert series.videos.results[0].published_at is None

    def test_a_blank_external_id_parses_to_none(self):
        """`""` here is worse than null: it is a mapping key that would be *used*
        — `/find/` called with an empty imdb id — rather than skipped."""
        series = TMDBSeries.model_validate(
            make_series(external_ids={"imdb_id": "", "tvdb_id": 81189})
        )

        assert series.external_ids is not None
        assert series.external_ids.imdb_id is None
        assert series.external_ids.tvdb_id == 81189


class TestAppendedSeasons:
    """`append_to_response` returns each season under a dynamic `season/N` key,
    which no static field can name."""

    def test_they_are_collected_off_the_dynamic_keys(self):
        series = TMDBSeries.model_validate(make_series(seasons=3, episodes_per_season=2))

        assert sorted(d.season_number for d in series.appended_seasons) == [1, 2, 3]
        assert all(len(d.episodes) == 2 for d in series.appended_seasons)

    def test_a_payload_with_none_appended_yields_an_empty_list(self):
        series = TMDBSeries.model_validate(make_series(append_seasons=False))

        assert series.appended_seasons == []

    def test_a_caller_cannot_smuggle_seasons_in_under_the_field_name(self):
        """The response is the only authority on what rode along. Honouring both
        would make a mistyped `season/N` key look like a season nobody asked for."""
        series = TMDBSeries.model_validate(
            make_series(append_seasons=False, appended_seasons=[make_season_detail(9)])
        )

        assert series.appended_seasons == []

    def test_an_appended_block_carries_no_season_id(self):
        """Measured, and the reason season identity comes from `seasons[]`: the
        appended form has `_id` (TMDB-internal) and no `id` at all."""
        detail = TMDBSeasonDetail.model_validate(make_season_detail(1))

        assert detail.tmdb_id is None
        assert detail.season_number == 1

    def test_a_standalone_season_fetch_does_carry_one(self):
        detail = TMDBSeasonDetail.model_validate(make_season_detail(1, id=3572))

        assert detail.tmdb_id == 3572


class TestNamespacePresence:
    """A namespace that was never requested is `None`, not `[]`.

    The writers spend that distinction: `None` means "the caller did not ask",
    and clearing a show's AKAs on that basis would empty the table for every
    show a narrower fetch touched.
    """

    @pytest.mark.parametrize(
        "field",
        [
            "aggregate_credits",
            "external_ids",
            "alternative_titles",
            "content_ratings",
            "episode_groups",
            "images",
            "keywords",
            "screened_theatrically",
            "translations",
            "videos",
            "watch_providers",
        ],
    )
    def test_an_unrequested_namespace_is_none(self, field):
        series = TMDBSeries.model_validate(make_series())

        assert getattr(series, field) is None

    def test_an_empty_namespace_is_not_none(self):
        series = TMDBSeries.model_validate(make_series(alternative_titles={"results": []}))

        assert series.alternative_titles is not None
        assert series.alternative_titles.results == []

    def test_watch_providers_reads_the_slashed_key(self):
        payload = make_series()
        payload["watch/providers"] = {"results": {"US": {"link": "https://tmdb/x"}}}

        series = TMDBSeries.model_validate(payload)

        assert series.watch_providers is not None
        assert list(series.watch_providers.results) == ["US"]


class TestWatchProviderOffers:
    def test_offers_come_back_typed_and_in_the_constraint_order(self):
        providers = TMDBWatchProviders.model_validate(
            {
                "results": {
                    "US": {
                        "link": "https://www.themoviedb.org/tv/1396/watch",
                        "buy": [{"provider_id": 2, "provider_name": "Apple TV"}],
                        "flatrate": [{"provider_id": 8, "provider_name": "Netflix"}],
                    }
                }
            }
        )

        offers = providers.results["US"].offers()

        assert [(kind, o.provider_name) for kind, o in offers] == [
            ("flatrate", "Netflix"),
            ("buy", "Apple TV"),
        ]


class TestAliasesOnly:
    """Fields are bound by upstream key alone — no `populate_by_name`.

    A fixture that spells the upstream key wrong must fail here rather than
    parse to `None` and read as missing data, which is the failure mode
    `TVMazeExternals` records from the original ingest.
    """

    def test_the_field_name_is_not_accepted_in_place_of_the_upstream_key(self):
        with pytest.raises(ValidationError):
            TMDBSeasonSummary.model_validate({"tmdb_id": 3572, "season_number": 1})

    def test_the_upstream_key_is(self):
        summary = TMDBSeasonSummary.model_validate({"id": 3572, "season_number": 1})

        assert summary.tmdb_id == 3572


class TestUnknownFields:
    def test_a_field_we_do_not_model_is_ignored(self):
        """`softcore` is real, undocumented and classified *skipped* (audit §3).
        TMDB adds keys without warning; a parser that raised on one would fail
        an entire pass over a field nobody reads."""
        series = TMDBSeries.model_validate(make_series(softcore=False, brand_new_field=1))

        assert series.name == "Show 1396"


class TestAggregateCredits:
    """The nesting is the whole point of taking this namespace over `credits`:
    `episode_count` arrives per role, not per person."""

    def test_a_cast_entry_carries_a_role_per_character(self):
        credits = TMDBAggregateCredits.model_validate(
            make_aggregate_credits(
                cast=[
                    make_cast_member(
                        1, "Tatiana Maslany", [make_role("Sarah", 50), make_role("Helena", 30)]
                    )
                ]
            )
        )

        member = credits.cast[0]
        assert [(r.character, r.episode_count) for r in member.roles] == [
            ("Sarah", 50),
            ("Helena", 30),
        ]
        assert member.total_episode_count == 80

    def test_a_crew_entry_carries_the_department_and_a_job_per_credit(self):
        """`department` sits on the entry and `job` on each nested credit — the
        `(department, job)` pair `crew_role` interns is assembled across the two
        levels, so a parser that lost either half would intern nothing."""
        credits = TMDBAggregateCredits.model_validate(
            make_aggregate_credits(
                crew=[
                    make_crew_member(
                        9,
                        "Vince Gilligan",
                        "Writing",
                        [make_job("Writer", 29), make_job("Story", 3)],
                    )
                ]
            )
        )

        member = credits.crew[0]
        assert member.department == "Writing"
        assert [(j.job, j.episode_count) for j in member.jobs] == [("Writer", 29), ("Story", 3)]

    def test_billing_order_reads_the_upstream_key_alone(self):
        """`order` is the upstream spelling; `billing_order` is the column. The
        alias binds one way only, so a fixture using the column name must fail
        rather than parse to `None` and read as an unranked cast."""
        member = make_cast_member(1, "Lead", [make_role("Walter", 62)], order=3)
        assert TMDBAggregateCast.model_validate(member).billing_order == 3

        member["billing_order"] = member.pop("order")
        assert TMDBAggregateCast.model_validate(member).billing_order is None

    @pytest.mark.parametrize("blank", ["", None])
    def test_a_blank_character_parses_to_none(self, blank):
        """Free text upstream, and blank in 1 of 7,629 sampled roles. `None` is
        what keeps `catalog.character` from interning a role nobody played."""
        role = TMDBRole.model_validate({"credit_id": "x", "character": blank, "episode_count": 1})

        assert role.character is None

    def test_a_cast_entry_with_no_person_is_still_a_hard_failure(self):
        """Show grain stays strict where episode grain was relaxed (NEU-1128).

        The two failure modes are not comparable. An episode credit with no
        person is one appearance we cannot store, and TMDB demonstrably sends
        them — 12 of the first ~4,000 shows in NEU-1127's production backfill.
        A show-level cast entry with none would be a payload we have
        misunderstood, and has never been measured, so it must still stop the
        parse rather than be dropped quietly.
        """
        nameless = make_cast_member(1, "Ignored", [make_role("Walt", 1)])
        del nameless["id"]
        del nameless["name"]

        with pytest.raises(ValidationError):
            TMDBAggregateCredits.model_validate(make_aggregate_credits(cast=[nameless]))

    def test_an_episode_credit_with_no_person_parses(self):
        """The episode-grain half of the same rule, at the level it is decided.

        `TMDBEpisodeGuestStar` and `TMDBEpisodeCrewMember` are siblings of
        `TMDBCreditPerson` rather than subclasses, because widening `int` to
        `int | None` in a subclass is exactly the substitutability violation it
        looks like. Dropping the entry is the writer's job — see
        `upsert._has_person`.
        """
        nameless = make_guest_star(1, "Ignored", "Ghost")
        del nameless["id"]
        del nameless["name"]

        episode = TMDBEpisode.model_validate(make_episode(1, 1, 1, guest_stars=[nameless]))

        assert episode.guest_stars is not None
        assert (episode.guest_stars[0].tmdb_person_id, episode.guest_stars[0].name) == (None, None)
        assert episode.guest_stars[0].character == "Ghost"

    def test_a_crew_entry_has_no_order(self):
        """Measured absent on all 2,066 sampled show-crew entries, which is why
        `catalog.show_crew` carries no ordering column and `episode_count` is the
        sort key instead."""
        member = make_crew_member(9, "A Writer", "Writing", [make_job("Writer", 29)])

        assert "order" not in member

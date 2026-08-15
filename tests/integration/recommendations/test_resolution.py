"""Resolving a model-authored title and year to a `catalog.show` id (NEU-1107).

Every test is DB-backed because the fold this module matches on is evaluated in
Postgres and is not reproducible in Python (`sql_fold`) — a test that folded its
own titles would pin a second, disagreeing definition.

The fixture catalog is seeded here rather than read from ambient data: `catalog`
is sparsely populated locally while the TMDB ingest runs, and a resolution test
that passes only on a full mirror is one that fails for the next person.
"""

from datetime import date

import pytest

from tests.fixtures import recommendations as recorded
from tvbf.app.models import MATCHED_VIA_AKA, MATCHED_VIA_NAME
from tvbf.catalog.models import Show, ShowAka
from tvbf.recommendations.resolution import resolve

SHOGUN = 971_000
SPIDER_MAN = 971_100
HEIST = 971_200
UNDATED = 971_300
REBOOT_POPULAR = 971_400
REBOOT_OBSCURE = 971_500
UNRATED_FIRST = 971_600
UNRATED_SECOND = 971_700
PUNCTUATION = 971_800
AKA_ONLY_POPULAR = 971_900


@pytest.fixture
async def catalog(session):
    session.add_all(
        [
            # Diacritic and punctuation folding, the two things the model's
            # spelling will differ by most often.
            Show(id=SHOGUN, name="Shōgun", first_air_date=date(2024, 2, 27), popularity=90.0),
            Show(
                id=SPIDER_MAN,
                name="Spider-Man: The Animated Series",
                first_air_date=date(1994, 11, 19),
                popularity=20.0,
            ),
            # Resolved by its English AKA, never by its own name.
            Show(
                id=HEIST, name="La casa de papel", first_air_date=date(2017, 5, 2), popularity=80.0
            ),
            Show(id=UNDATED, name="Undated Show", popularity=99.0),
            # Fold equal at the same year: only `popularity` separates them.
            Show(
                id=REBOOT_POPULAR, name="Re-Boot", first_air_date=date(2019, 3, 1), popularity=50.0
            ),
            Show(id=REBOOT_OBSCURE, name="ReBoot", first_air_date=date(2019, 8, 1), popularity=1.0),
            # Fold equal with no popularity at all on either side.
            Show(id=UNRATED_FIRST, name="Unrated", first_air_date=date(2010, 1, 1)),
            Show(id=UNRATED_SECOND, name="un-rated", first_air_date=date(2010, 6, 1)),
            # Folds to the empty string, as does the title asked about.
            Show(id=PUNCTUATION, name="!!!", first_air_date=date(2015, 1, 1), popularity=70.0),
            # Carries "Shogun" as an AKA and is far more popular than the show
            # actually named that — the name tier must still win.
            Show(
                id=AKA_ONLY_POPULAR,
                name="Some Other Series",
                first_air_date=date(2024, 6, 1),
                popularity=999.0,
            ),
        ]
    )
    await session.flush()
    session.add_all(
        [
            ShowAka(show_id=HEIST, title="Money Heist", country_code="US"),
            # Two AKAs folding equal on one show: the tier must answer once.
            ShowAka(show_id=HEIST, title="money-heist", country_code="GB"),
            ShowAka(show_id=AKA_ONLY_POPULAR, title="Shogun", country_code="US"),
        ]
    )
    await session.flush()


class TestTheNameTier:
    async def test_an_exact_title_and_year_resolve(self, session, catalog):
        resolution = await resolve(session, title="Shōgun", year=2024)

        assert resolution is not None
        assert resolution.show_id == SHOGUN
        assert resolution.matched_via == MATCHED_VIA_NAME

    async def test_the_model_may_drop_the_diacritics(self, session, catalog):
        """The fold is the whole point of the tier: a model writes "Shogun"."""
        resolution = await resolve(session, title="shogun", year=2024)

        assert resolution is not None
        assert resolution.show_id == SHOGUN

    async def test_the_model_may_drop_the_punctuation(self, session, catalog):
        resolution = await resolve(session, title="Spider Man The Animated Series", year=1994)

        assert resolution is not None
        assert resolution.show_id == SPIDER_MAN

    async def test_a_year_one_either_side_still_resolves(self, session, catalog):
        for year in (2023, 2025):
            resolution = await resolve(session, title="Shōgun", year=year)

            assert resolution is not None, year
            assert resolution.show_id == SHOGUN

    async def test_a_year_two_out_does_not(self, session, catalog):
        assert await resolve(session, title="Shōgun", year=2026) is None

    async def test_a_show_with_no_premiere_date_never_resolves(self, session, catalog):
        """There is no year to compare, and the year is the only disambiguator
        resolution has — so an undated row is unchecked rather than checked."""
        assert await resolve(session, title="Undated Show", year=2020) is None


class TestTheAkaTier:
    async def test_an_english_title_resolves_through_show_aka(self, session, catalog):
        resolution = await resolve(session, title="Money Heist", year=2017)

        assert resolution is not None
        assert resolution.show_id == HEIST
        assert resolution.matched_via == MATCHED_VIA_AKA

    async def test_two_akas_folding_equal_still_answer_once(self, session, catalog):
        """Both of *La casa de papel*'s AKAs fold to "moneyheist"; the join must
        not turn that into an ambiguity."""
        resolution = await resolve(session, title="MONEY HEIST!", year=2017)

        assert resolution is not None
        assert resolution.show_id == HEIST

    async def test_the_aka_tier_is_still_year_bounded(self, session, catalog):
        assert await resolve(session, title="Money Heist", year=2021) is None

    async def test_a_name_match_beats_a_more_popular_aka_match(self, session, catalog):
        """Tier order is not a popularity contest. `Some Other Series` carries
        "Shogun" as an AKA and is ten times as popular; the show *named* Shōgun
        still wins, because tier 2 is only reached when tier 1 found nothing."""
        resolution = await resolve(session, title="Shogun", year=2024)

        assert resolution is not None
        assert resolution.show_id == SHOGUN
        assert resolution.matched_via == MATCHED_VIA_NAME


class TestAmbiguity:
    async def test_the_more_popular_of_two_equal_titles_wins(self, session, catalog):
        """Deliberately unlike NEU-1043, where every ambiguity resolves to
        unmatched: the cost there was a user's watch history attached to the
        wrong show, here it is a less-likely card in a grid of twelve."""
        resolution = await resolve(session, title="Reboot", year=2019)

        assert resolution is not None
        assert resolution.show_id == REBOOT_POPULAR

    async def test_a_show_with_no_popularity_loses_to_one_with_any(self, session, catalog):
        """Postgres sorts NULLs first on DESC, so this is the one that breaks the
        moment `nulls_last()` is dropped."""
        session.add(Show(id=999_001, name="Reboot", first_air_date=date(2019, 5, 1)))
        await session.flush()

        resolution = await resolve(session, title="Reboot", year=2019)

        assert resolution is not None
        assert resolution.show_id == REBOOT_POPULAR

    async def test_an_unpopular_tie_still_answers_the_same_show_every_time(self, session, catalog):
        """Two rows, neither with a popularity: the id breaks the tie, so the
        stored set is debuggable rather than planner-dependent."""
        answers = set()
        for _ in range(3):
            resolution = await resolve(session, title="Unrated", year=2010)
            assert resolution is not None
            answers.add(resolution.show_id)

        assert answers == {UNRATED_FIRST}


class TestWhatDoesNotResolve:
    async def test_an_unknown_title_resolves_to_nothing(self, session, catalog):
        assert await resolve(session, title="A Show Nobody Made", year=2019) is None

    async def test_a_near_miss_is_not_papered_over(self, session, catalog):
        """No fuzzy or trigram fallback (project spec §8): a title one letter out
        is a hallucination or a catalog gap, and both belong in the logs."""
        assert await resolve(session, title="Shogunn", year=2024) is None
        assert await resolve(session, title="Money Heists", year=2017) is None

    async def test_a_title_that_folds_to_nothing_matches_nothing(self, session, catalog):
        """ "???" and "!!!" both fold to the empty string. Treating those as equal
        is the one way an *exact* match could name an unrelated show."""
        assert await resolve(session, title="???", year=2015) is None

    async def test_an_empty_catalog_resolves_to_nothing(self, session):
        assert await resolve(session, title="Shōgun", year=2024) is None


class TestAgainstRecordedResponses:
    """The resolver against titles a real model really wrote.

    `tests/fixtures/recommendations/README.md` records where these came from.
    The catalog rows are seeded here rather than read from the live mirror, which
    is what keeps these tests true on a laptop whose ingest has not finished —
    but the *claims* about which titles resolve were checked against the real
    local catalog first, so the fixtures are not shaped to agree with them.
    """

    async def test_every_title_in_the_clean_recording_resolves(self, session):
        """Seeded from the recording's own titles, so what this pins is the count,
        the tier, and that 25 real model-authored titles round-trip at all — not
        that the mirror happens to carry them. The README's claim about the real
        catalog is checked by hand, and deliberately not by this test."""
        entries = recorded.recommendations(recorded.CLEAN)
        session.add_all(
            [
                Show(
                    id=972_000 + index,
                    name=entry["title"],
                    first_air_date=date(entry["release_year"], 6, 1),
                )
                for index, entry in enumerate(entries)
            ]
        )
        await session.flush()

        resolved = [
            await resolve(session, title=entry["title"], year=entry["release_year"])
            for entry in entries
        ]

        assert len(resolved) == 25
        assert all(r is not None and r.matched_via == MATCHED_VIA_NAME for r in resolved)

    async def test_the_obscure_recording_names_a_show_the_catalog_does_not_have(self, session):
        """`Twin Peaks: The Return` is the real recorded miss: the series exists,
        but under that title nothing does — the mirror carries *Twin Peaks*
        (1990), which the 2017 revival is a season of. A genuine catalog gap
        rather than a hallucination, and both answer `None`, which the caller
        drops and logs."""
        session.add(Show(id=972_100, name="Twin Peaks", first_air_date=date(1990, 4, 8)))
        await session.flush()

        assert await resolve(session, title="Twin Peaks: The Return", year=2017) is None

    async def test_the_obscure_recording_needs_the_aka_tier(self, session):
        """`Trapped` is *Ófærð*: an English title for an Icelandic series, which
        is the case the AKA tier exists for and the one a name-only resolver
        silently drops. Four of that recording's 25 titles resolve this way."""
        session.add(Show(id=972_200, name="Ófærð", first_air_date=date(2015, 12, 27)))
        await session.flush()
        session.add(ShowAka(show_id=972_200, title="Trapped", country_code="US"))
        await session.flush()

        resolution = await resolve(session, title="Trapped", year=2015)

        assert resolution is not None
        assert resolution.show_id == 972_200
        assert resolution.matched_via == MATCHED_VIA_AKA

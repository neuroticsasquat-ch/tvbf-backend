"""The human matching queue and its three resolutions (NEU-1044).

Every test here is one of the ticket's acceptance criteria or one of the ways a
row could silently leave the queue without anybody having decided anything.

Seeding is doubled on purpose: the queue reads `catalog` for the mapping state
while `human_queue.unmirrored_user_touched_shows` reads `tvmaze` for the rows the
copy never reached, so a row has to exist on both spines under one id. That id is
the migration's premise (NEU-1042 preserved TV Maze ids as `catalog.show.id`) and
the only reason one query can span both — and since NEU-1046 it is also what the
`app.user_*` foreign keys resolve against.
"""

from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select, text

from tests.fixtures.spines import without_catalog_fk
from tvbf.app.models import (
    ActivityEvent,
    UserEpisodeRating,
    UserEpisodeWatch,
    UserShowRating,
    UserShowWatch,
)
from tvbf.catalog import models as cm
from tvbf.tmdb.client import TMDBClient
from tvbf.tmdb.enrichment import MATCH_HUMAN, MATCH_TITLE_YEAR, MATCH_TVDB_ID
from tvbf.tmdb.human_queue import (
    QueueError,
    annotate,
    build_queue,
    confirm,
    reject,
    unmirrored_user_touched_shows,
)

BASE = "https://api.themoviedb.org/3"

# Well clear of the fixtures' catalog, so these rows never collide with a seeded
# browse show and the assertions can name exact ids.
_ID = 9_400_000


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


async def _seed_show(session, *, name: str = "Queue Show", **catalog_kwargs) -> int:
    show_id = _next_id()
    session.add(cm.Show(id=show_id, name=name, **catalog_kwargs))
    await session.flush()
    await session.commit()
    return show_id


async def _seed_episode(session, show_id: int) -> int:
    episode_id = _next_id()
    session.add(cm.Episode(id=episode_id, show_id=show_id, season_number=1, episode_number=1))
    await session.flush()
    return episode_id


async def _rows_for(session, show_id: int):
    return [row for row in await build_queue(session) if row.show_id == show_id]


async def _match_method(session, show_id: int) -> tuple[int | None, str | None]:
    stmt = (
        select(cm.Show.tmdb_id, cm.Show.match_method)
        .where(cm.Show.id == show_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).one()


def _client(*, retry_max_attempts: int = 5) -> TMDBClient:
    return TMDBClient(
        base_url=BASE,
        read_access_token="eyJ-not-a-real-token",
        rate_calls=20,
        rate_window=1,
        retry_max_attempts=retry_max_attempts,
    )


@pytest.mark.asyncio
async def test_unmatched_user_touched_show_is_queued(session, make_user):
    user = await make_user(email="hq1@example.com")
    show_id = await _seed_show(session, name="Discretion")
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    await session.commit()

    (row,) = await _rows_for(session, show_id)

    assert row.name == "Discretion"
    assert row.tmdb_id is None
    assert row.match_method is None
    assert row.users == ("hq1@example.com",)


@pytest.mark.asyncio
async def test_title_year_guess_is_queued_for_review(session, make_user):
    user = await make_user(email="hq2@example.com")
    show_id = await _seed_show(session, tmdb_id=299737, match_method=MATCH_TITLE_YEAR)
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    await session.commit()

    (row,) = await _rows_for(session, show_id)

    # A tier-3 match is a guess, not a mapping — surfaced, not trusted silently.
    assert row.tmdb_id == 299737
    assert row.match_method == MATCH_TITLE_YEAR


@pytest.mark.asyncio
async def test_exact_match_and_direct_ingest_row_never_queue(session, make_user):
    user = await make_user(email="hq3@example.com")
    exact = await _seed_show(session, tmdb_id=1001, match_method=MATCH_TVDB_ID)
    ingested = await _seed_show(session, tmdb_id=1002)  # match_method NULL: knew its own id
    session.add(UserShowWatch(user_id=user.id, show_id=exact))
    session.add(UserShowWatch(user_id=user.id, show_id=ingested))
    await session.commit()

    assert await _rows_for(session, exact) == []
    assert await _rows_for(session, ingested) == []


@pytest.mark.asyncio
async def test_untouched_unmatched_show_never_queues(session):
    show_id = await _seed_show(session, name="Nobody Watches This")

    # The queue is scoped to shows users touched. 26k unmatched rows are the
    # migration's expected residue, not work for a person.
    assert await _rows_for(session, show_id) == []


@pytest.mark.asyncio
async def test_every_kind_of_touch_reaches_the_queue(session, make_user):
    user = await make_user(email="hq4@example.com")
    by_episode_watch = await _seed_show(session)
    by_show_rating = await _seed_show(session)
    by_episode_rating = await _seed_show(session)
    by_activity = await _seed_show(session)

    episode = await _seed_episode(session, by_episode_watch)
    rated_episode = await _seed_episode(session, by_episode_rating)
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=episode))
    session.add(UserShowRating(user_id=user.id, show_id=by_show_rating, stars=Decimal("4.0")))
    session.add(UserEpisodeRating(user_id=user.id, episode_id=rated_episode, stars=Decimal("3.0")))
    session.add(
        ActivityEvent(actor_id=user.id, verb="watched", target_type="show", target_id=by_activity)
    )
    await session.commit()

    for show_id in (by_episode_watch, by_show_rating, by_episode_rating, by_activity):
        assert len(await _rows_for(session, show_id)) == 1, show_id


@pytest.mark.asyncio
async def test_row_carries_the_context_needed_to_decide(session, make_user):
    user = await make_user(email="hq5a@example.com")
    other = await make_user(email="hq5b@example.com")
    show_id = await _seed_show(
        session,
        name="The Traitors Ireland Uncloaked",
        first_air_date=date(2025, 9, 1),
        original_language="en",
        status="Returning Series",
        tvdb_id=456789,
        imdb_id="tt1234567",
    )
    episode = await _seed_episode(session, show_id)
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=episode))
    session.add(UserEpisodeWatch(user_id=other.id, episode_id=episode))
    session.add(UserShowRating(user_id=user.id, show_id=show_id, stars=Decimal("5.0")))
    await session.commit()

    (row,) = await _rows_for(session, show_id)

    assert row.first_air_date == date(2025, 9, 1)
    assert row.original_language == "en"
    assert row.status == "Returning Series"
    assert row.tvdb_id == 456789
    assert row.imdb_id == "tt1234567"
    assert row.episode_watches == 2
    assert row.show_ratings == 1
    assert row.users == ("hq5a@example.com", "hq5b@example.com")
    assert row.carries_user_data is True
    assert row.to_dict()["first_air_date"] == "2025-09-01"


@pytest.mark.asyncio
async def test_queue_leads_with_the_rows_that_carry_history(session, make_user):
    user = await make_user(email="hq6@example.com")
    quiet = await _seed_show(session, name="No History")
    watched = await _seed_show(session, name="Has History")
    episode = await _seed_episode(session, watched)
    session.add(UserShowWatch(user_id=user.id, show_id=quiet))
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=episode))
    await session.commit()

    ordered = [row.show_id for row in await build_queue(session) if row.show_id in (quiet, watched)]

    assert ordered == [watched, quiet]


@pytest.mark.asyncio
async def test_a_show_the_copy_never_mirrored_is_reported_separately(session, make_user):
    """The one way the queue could read empty while a user's show has no mapping.

    A show a user has touched that has no `catalog.show` row at all is invisible
    to a query that reads *from* `catalog.show`. That state was reachable before
    cutover, when the TV Maze daily kept adding shows the copy had not seen; it
    is unreachable now that `catalog` is the only spine and the foreign key is
    enforced, which is exactly why the test has to stand the constraint down to
    reconstruct it. The report still earns its place: it is what would name the
    rows if one ever appeared.
    """
    user = await make_user(email="hq18@example.com")
    show_id = _next_id()
    async with without_catalog_fk(session, "user_show_watch"):
        session.add(UserShowWatch(user_id=user.id, show_id=show_id))
        await session.commit()

        assert await _rows_for(session, show_id) == []
        assert show_id in await unmirrored_user_touched_shows(session)


@pytest.mark.asyncio
async def test_a_mirrored_show_is_not_reported_as_unmirrored(session, make_user):
    user = await make_user(email="hq19@example.com")
    show_id = await _seed_show(session)
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    await session.commit()

    assert show_id not in await unmirrored_user_touched_shows(session)


@pytest.mark.asyncio
async def test_confirm_records_the_verdict_and_empties_the_row(session, make_user):
    user = await make_user(email="hq7@example.com")
    show_id = await _seed_show(session, name="Cunk on Earth")
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    await session.commit()

    message = await confirm(session, show_id, 122333)

    assert "confirmed" in message
    assert await _match_method(session, show_id) == (122333, MATCH_HUMAN)
    assert await _rows_for(session, show_id) == []


@pytest.mark.asyncio
async def test_confirming_an_unchanged_guess_re_stamps_it(session, make_user):
    """The four production guesses confirmed by hand — the id is already right."""
    user = await make_user(email="hq8@example.com")
    show_id = await _seed_show(session, tmdb_id=119955, match_method=MATCH_TITLE_YEAR)
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    await session.commit()

    message = await confirm(session, show_id, 119955)

    assert "re-stamped" in message
    assert await _match_method(session, show_id) == (119955, MATCH_HUMAN)
    assert await _rows_for(session, show_id) == []


@pytest.mark.asyncio
async def test_confirming_a_different_id_retracts_a_wrong_guess(session, make_user):
    user = await make_user(email="hq9@example.com")
    show_id = await _seed_show(session, tmdb_id=111, match_method=MATCH_TITLE_YEAR)
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    await session.commit()

    message = await confirm(session, show_id, 222)

    assert "re-pointed" in message
    assert await _match_method(session, show_id) == (222, MATCH_HUMAN)


@pytest.mark.asyncio
async def test_confirm_refuses_an_id_another_row_already_holds(session, make_user):
    user = await make_user(email="hq10@example.com")
    show_id = await _seed_show(session, name="Contested")
    holder = await _seed_show(
        session, name="Already Mapped", tmdb_id=747, match_method=MATCH_TVDB_ID
    )
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    await session.commit()

    with pytest.raises(QueueError, match=f"already held by catalog show {holder}"):
        await confirm(session, show_id, 747)

    # Refused, not half-applied: the row is untouched and still in the queue.
    assert await _match_method(session, show_id) == (None, None)
    assert len(await _rows_for(session, show_id)) == 1


@pytest.mark.asyncio
async def test_resolutions_refuse_a_row_matched_exactly(session, make_user):
    user = await make_user(email="hq11@example.com")
    show_id = await _seed_show(session, tmdb_id=555, match_method=MATCH_TVDB_ID)
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    await session.commit()

    with pytest.raises(QueueError, match="which is exact"):
        await confirm(session, show_id, 556)
    with pytest.raises(QueueError, match="which is exact"):
        await reject(session, show_id)

    assert await _match_method(session, show_id) == (555, MATCH_TVDB_ID)


@pytest.mark.asyncio
async def test_confirm_refuses_to_overwrite_another_human_verdict(session, make_user):
    user = await make_user(email="hq12@example.com")
    show_id = await _seed_show(session)
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    await session.commit()
    await confirm(session, show_id, 900)

    with pytest.raises(QueueError, match="already resolved by hand"):
        await confirm(session, show_id, 901)

    assert await _match_method(session, show_id) == (900, MATCH_HUMAN)


@pytest.mark.asyncio
async def test_confirm_refuses_an_unknown_show(session):
    with pytest.raises(QueueError, match="no catalog.show with id"):
        await confirm(session, 1, 2)


@pytest.mark.asyncio
async def test_reject_leaves_the_row_locally_authored_with_its_watches(session, make_user):
    user = await make_user(email="hq13@example.com")
    show_id = await _seed_show(session, name="Not On TMDB")
    episode = await _seed_episode(session, show_id)
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=episode))
    await session.commit()

    message = await reject(session, show_id)

    assert "locally-authored" in message
    assert await _match_method(session, show_id) == (None, MATCH_HUMAN)
    assert await _rows_for(session, show_id) == []
    # The whole promise of resolution 2: the history is untouched.
    watches = await session.execute(
        text("SELECT count(*) FROM app.user_episode_watch WHERE episode_id = :id"),
        {"id": episode},
    )
    assert watches.scalar_one() == 1


@pytest.mark.asyncio
async def test_reject_retracts_a_guess_it_disagrees_with(session, make_user):
    user = await make_user(email="hq14@example.com")
    show_id = await _seed_show(session, tmdb_id=333, match_method=MATCH_TITLE_YEAR)
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    await session.commit()

    message = await reject(session, show_id)

    assert "TMDB 333" in message
    assert await _match_method(session, show_id) == (None, MATCH_HUMAN)


@pytest.mark.asyncio
@respx.mock
async def test_annotate_carries_candidates_and_the_guess_under_review(session, make_user):
    user = await make_user(email="hq15@example.com")
    show_id = await _seed_show(
        session, name="Cunk on Earth", tmdb_id=42, match_method=MATCH_TITLE_YEAR
    )
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    await session.commit()

    respx.get(f"{BASE}/search/tv", params={"query": "Cunk on Earth"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "total_results": 2,
                "results": [
                    {"id": 42, "name": "Cunk on Earth", "first_air_date": "2022-09-19"},
                    {"id": 43, "name": "Cunk on Britain", "first_air_date": "2018-04-03"},
                ],
            },
        )
    )
    respx.get(f"{BASE}/tv/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "name": "Cunk on Earth"})
    )

    rows = await _rows_for(session, show_id)
    async with _client() as client:
        (entry,) = await annotate(client, rows)

    # Ambiguity is the reviewer's input, not something to filter out first.
    assert [c["tmdb_id"] for c in entry["candidates"]] == [42, 43]
    assert entry["current_match"]["name"] == "Cunk on Earth"
    assert "tmdb_error" not in entry


@pytest.mark.asyncio
@respx.mock
async def test_annotate_degrades_the_row_rather_than_the_report(session, make_user):
    user = await make_user(email="hq16@example.com")
    show_id = await _seed_show(session, name="Discretion")
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    await session.commit()

    respx.get(f"{BASE}/search/tv").mock(return_value=httpx.Response(503))

    rows = await _rows_for(session, show_id)
    # One attempt: the retry ladder is the client's business and tested there.
    async with _client(retry_max_attempts=1) as client:
        (entry,) = await annotate(client, rows)

    assert entry["show_id"] == show_id
    assert "tmdb_error" in entry
    assert "candidates" not in entry


@pytest.mark.asyncio
async def test_annotate_without_a_client_spends_no_upstream_call(session, make_user):
    user = await make_user(email="hq17@example.com")
    show_id = await _seed_show(session)
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    await session.commit()

    rows = await _rows_for(session, show_id)
    (entry,) = await annotate(None, rows)

    assert entry["show_id"] == show_id
    assert "candidates" not in entry

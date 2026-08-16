"""The current-set definition, as behaviour (NEU-1108).

Every property asserted here is one the weekly pass and `GET /me/recommendations`
both depend on holding the *same* answer: which set a user is currently seeing.
Two implementations of that query is how the pass decides a user is up to date
while the API serves something else, which is the whole reason these functions
exist in one module.
"""

from datetime import UTC, datetime, timedelta

import pytest

from tvbf.app.models import (
    MATCHED_VIA_AKA,
    MATCHED_VIA_NAME,
    SET_STATUS_FAILED,
    SET_STATUS_INSUFFICIENT_HISTORY,
    SET_STATUS_NO_MATCHES,
    SET_STATUS_SUCCEEDED,
    User,
    UserRecommendation,
    UserRecommendationSet,
)
from tvbf.app.repos import recommendation_repo
from tvbf.catalog.models import Show

_BASE = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


async def _show(session, show_id: int, name: str, **overrides) -> Show:
    show = Show(id=show_id, name=name, **overrides)
    session.add(show)
    await session.flush()
    return show


async def _set(
    session,
    user: User,
    *,
    status: str = SET_STATUS_SUCCEEDED,
    generated_at: datetime | None = None,
    payload_hash: str = "abc123",
) -> UserRecommendationSet:
    rec_set = UserRecommendationSet(
        user_id=user.id,
        payload_hash=payload_hash,
        prompt_version="1",
        model="deepseek-ai/DeepSeek-V4-Pro-0813",
        status=status,
        compiled_payload={"liked": []},
    )
    if generated_at is not None:
        rec_set.generated_at = generated_at
    session.add(rec_set)
    await session.flush()
    return rec_set


async def _rec(
    session,
    rec_set: UserRecommendationSet,
    *,
    rank: int,
    show: Show,
    matched_via: str = MATCHED_VIA_NAME,
) -> UserRecommendation:
    row = UserRecommendation(
        set_id=rec_set.id,
        rank=rank,
        show_id=show.id,
        reason=f"Reason {rank}",
        matched_via=matched_via,
    )
    session.add(row)
    await session.flush()
    return row


@pytest.mark.asyncio
async def test_no_sets_at_all_reads_as_no_current_set(session, make_user):
    user = await make_user(email="none@example.com")

    assert await recommendation_repo.get_current_set(session, user_id=user.id) is None
    assert await recommendation_repo.list_current_recommendations(session, user_id=user.id) == []


@pytest.mark.asyncio
async def test_the_newest_succeeded_set_is_the_current_one(session, make_user):
    user = await make_user(email="newest@example.com")
    await _set(session, user, generated_at=_BASE - timedelta(days=14), payload_hash="old")
    newest = await _set(session, user, generated_at=_BASE, payload_hash="new")

    current = await recommendation_repo.get_current_set(session, user_id=user.id)

    assert current is not None
    assert current.id == newest.id
    assert current.payload_hash == "new"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [SET_STATUS_FAILED, SET_STATUS_NO_MATCHES, SET_STATUS_INSUFFICIENT_HISTORY]
)
async def test_a_newer_unsucceeded_set_is_recorded_but_invisible(session, make_user, status):
    """The `succeeded` filter is what makes an unhappy run non-destructive.

    A provider outage records a `failed` set that is newest, and last week's
    recommendations have to keep standing behind it (project spec §9).
    """
    user = await make_user(email=f"{status}@example.com")
    succeeded = await _set(session, user, generated_at=_BASE - timedelta(days=7))
    show = await _show(session, 920001, "Kept Standing")
    await _rec(session, succeeded, rank=1, show=show)
    await _set(session, user, status=status, generated_at=_BASE)
    await session.commit()

    current = await recommendation_repo.get_current_set(session, user_id=user.id)
    rows = await recommendation_repo.list_current_recommendations(session, user_id=user.id)

    assert current is not None and current.id == succeeded.id
    assert [(rec.rank, found.id) for rec, found in rows] == [(1, show.id)]


@pytest.mark.asyncio
async def test_sets_are_scoped_per_user(session, make_user):
    mine = await make_user(email="mine@example.com")
    theirs = await make_user(email="theirs@example.com")
    my_set = await _set(session, mine, generated_at=_BASE - timedelta(days=7))
    their_set = await _set(session, theirs, generated_at=_BASE)
    my_show = await _show(session, 920010, "Mine")
    their_show = await _show(session, 920011, "Theirs")
    await _rec(session, my_set, rank=1, show=my_show)
    await _rec(session, their_set, rank=1, show=their_show)
    await session.commit()

    current = await recommendation_repo.get_current_set(session, user_id=mine.id)
    rows = await recommendation_repo.list_current_recommendations(session, user_id=mine.id)

    assert current is not None and current.id == my_set.id
    assert [show.id for _, show in rows] == [my_show.id]


@pytest.mark.asyncio
async def test_rows_come_from_the_current_set_in_rank_order_with_their_show(session, make_user):
    user = await make_user(email="rows@example.com")
    superseded = await _set(session, user, generated_at=_BASE - timedelta(days=7))
    stale_show = await _show(session, 920020, "Superseded")
    await _rec(session, superseded, rank=1, show=stale_show)

    current = await _set(session, user, generated_at=_BASE)
    second = await _show(session, 920021, "Second")
    first = await _show(session, 920022, "First")
    await _rec(session, current, rank=2, show=second, matched_via=MATCHED_VIA_AKA)
    await _rec(session, current, rank=1, show=first)
    await session.commit()

    rows = await recommendation_repo.list_current_recommendations(session, user_id=user.id)

    assert [(rec.rank, rec.matched_via, show.id, show.name) for rec, show in rows] == [
        (1, MATCHED_VIA_NAME, first.id, "First"),
        (2, MATCHED_VIA_AKA, second.id, "Second"),
    ]


@pytest.mark.asyncio
async def test_adult_and_tombstoned_shows_are_filtered_at_read_time(session, make_user):
    """A set generated in March can name a show tombstoned in June.

    The 25-asked-for / 12-displayed headroom (project spec §7) is what absorbs
    this, so the filter belongs on the read rather than on the write.
    """
    user = await make_user(email="filtered@example.com")
    rec_set = await _set(session, user, generated_at=_BASE)
    kept = await _show(session, 920030, "Kept")
    adult = await _show(session, 920031, "Adult", adult=True)
    gone = await _show(session, 920032, "Gone", deleted_upstream_at=_BASE)
    await _rec(session, rec_set, rank=1, show=adult)
    await _rec(session, rec_set, rank=2, show=gone)
    await _rec(session, rec_set, rank=3, show=kept)
    await session.commit()

    rows = await recommendation_repo.list_current_recommendations(session, user_id=user.id)

    assert [show.id for _, show in rows] == [kept.id]


@pytest.mark.asyncio
async def test_two_sets_generated_at_the_same_instant_resolve_to_the_same_one(session, make_user):
    """`generated_at` defaults to the transaction timestamp, so ties are real.

    Two sets written in one transaction carry the identical value, and without a
    total order the gate and the API could each pick a different row as current.
    """
    user = await make_user(email="tie@example.com")
    first = await _set(session, user, generated_at=_BASE)
    second = await _set(session, user, generated_at=_BASE)
    show_a = await _show(session, 920040, "A")
    show_b = await _show(session, 920041, "B")
    await _rec(session, first, rank=1, show=show_a)
    await _rec(session, second, rank=1, show=show_b)
    await session.commit()

    current = await recommendation_repo.get_current_set(session, user_id=user.id)
    rows = await recommendation_repo.list_current_recommendations(session, user_id=user.id)

    # The id is what breaks the tie, so which row wins is pinned rather than
    # merely consistent — a partial order would satisfy the agreement assertion
    # below on any given run and still let the two callers diverge on the next.
    assert current is not None
    assert current.id == max(first.id, second.id)
    assert [rec.set_id for rec, _ in rows] == [current.id]


class TestWritingASet:
    """`write_set` (NEU-1109), the other half of this module."""

    async def test_the_set_and_its_rows_land_together(self, session, make_user):
        user = await make_user()
        first = await _show(session, 974_000, "Dark", popularity=90.0)
        second = await _show(session, 974_001, "Shōgun", popularity=80.0)

        written = await recommendation_repo.write_set(
            session,
            user_id=user.id,
            status=SET_STATUS_SUCCEEDED,
            payload_hash="hash",
            prompt_version="1",
            model="deepseek-ai/DeepSeek-V4-Pro-0813",
            compiled_payload={"liked": []},
            raw_response={"recommendations": []},
            input_tokens=6_748,
            output_tokens=1_100,
            recommendations=[
                recommendation_repo.NewRecommendation(
                    show_id=first.id, reason="One.", matched_via=MATCHED_VIA_NAME
                ),
                recommendation_repo.NewRecommendation(
                    show_id=second.id, reason="Two.", matched_via=MATCHED_VIA_AKA
                ),
            ],
        )
        await session.commit()

        rows = await recommendation_repo.list_current_recommendations(session, user_id=user.id)
        assert written.status == SET_STATUS_SUCCEEDED
        assert [(rec.rank, rec.show_id, rec.matched_via) for rec, _ in rows] == [
            (1, first.id, MATCHED_VIA_NAME),
            (2, second.id, MATCHED_VIA_AKA),
        ]

    async def test_rank_is_the_order_it_was_given_rather_than_the_callers_to_assign(
        self, session, make_user
    ):
        """The model's ordering is the only ordering there is, and a caller that
        has to number the rows itself is a caller that can get it wrong."""
        user = await make_user()
        shows = [await _show(session, 974_100 + n, f"Show {n}") for n in range(3)]

        await recommendation_repo.write_set(
            session,
            user_id=user.id,
            status=SET_STATUS_SUCCEEDED,
            payload_hash="hash",
            prompt_version="1",
            model="m",
            compiled_payload={},
            recommendations=[
                recommendation_repo.NewRecommendation(
                    show_id=show.id, reason="r", matched_via=MATCHED_VIA_NAME
                )
                for show in reversed(shows)
            ],
        )
        await session.commit()

        rows = await recommendation_repo.list_current_recommendations(session, user_id=user.id)
        assert [rec.rank for rec, _ in rows] == [1, 2, 3]
        assert [show.id for _, show in rows] == [s.id for s in reversed(shows)]

    async def test_an_unhappy_status_is_recorded_and_stays_invisible_to_readers(
        self, session, make_user
    ):
        """All four statuses come through here — the row is the only place a
        failure becomes visible at 3-5 users, and only `succeeded` is served."""
        user = await make_user()
        await _set(session, user, generated_at=_BASE - timedelta(days=7))

        for status in (SET_STATUS_FAILED, SET_STATUS_NO_MATCHES, SET_STATUS_INSUFFICIENT_HISTORY):
            await recommendation_repo.write_set(
                session,
                user_id=user.id,
                status=status,
                payload_hash=f"hash-{status}",
                prompt_version="1",
                model="m",
                compiled_payload={},
            )
        await session.commit()

        current = await recommendation_repo.get_current_set(session, user_id=user.id)
        assert current is not None
        assert current.status == SET_STATUS_SUCCEEDED
        assert current.payload_hash == "abc123"

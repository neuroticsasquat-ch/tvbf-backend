"""The two recommendation tables, as behaviour rather than DDL (NEU-1106).

What is asserted here is what the project spec's §9 actually rests on: that a
set is scoped to a user and disappears with them, that its rows disappear with
it, that both vocabularies are closed, and that a show leaving the catalog takes
the suggestion pointing at it rather than leaving a dangling row. These are the
properties the weekly pass and the surface are written against.
"""

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from tvbf.app.models import (
    MATCHED_VIA_AKA,
    MATCHED_VIA_NAME,
    RECOMMENDATION_MATCHED_VIA,
    RECOMMENDATION_SET_STATUSES,
    SET_STATUS_SUCCEEDED,
    User,
    UserRecommendation,
    UserRecommendationSet,
)
from tvbf.catalog.models import Show


async def _user(session, email: str = "rec@example.com") -> User:
    user = User(email=email, password_hash="x", display_name="Rec")
    session.add(user)
    await session.flush()
    return user


def _set(user: User, **overrides) -> UserRecommendationSet:
    kwargs = {
        "user_id": user.id,
        "payload_hash": "abc123",
        "prompt_version": "1",
        "model": "deepseek-ai/DeepSeek-V4-Pro-0813",
        "status": SET_STATUS_SUCCEEDED,
        "compiled_payload": {"liked": [["Show", 2019, 100, 4.5]]},
    }
    kwargs.update(overrides)
    return UserRecommendationSet(**kwargs)


@pytest.mark.asyncio
async def test_set_and_its_rows_roundtrip(session):
    user = await _user(session)
    show = Show(id=910001, name="Recommended")
    session.add(show)
    rec_set = _set(user, input_tokens=6748, output_tokens=1100, raw_response={"choices": []})
    session.add(rec_set)
    await session.flush()
    session.add(
        UserRecommendation(
            set_id=rec_set.id,
            rank=1,
            show_id=show.id,
            reason="Because it is the sort of thing you finish.",
            matched_via=MATCHED_VIA_NAME,
        )
    )
    await session.commit()

    found = (
        await session.execute(
            select(UserRecommendationSet).where(UserRecommendationSet.user_id == user.id)
        )
    ).scalar_one()
    assert found.status == SET_STATUS_SUCCEEDED
    assert found.compiled_payload == {"liked": [["Show", 2019, 100, 4.5]]}
    assert found.generated_at is not None

    row = (
        await session.execute(
            select(UserRecommendation).where(UserRecommendation.set_id == rec_set.id)
        )
    ).scalar_one()
    assert (row.rank, row.show_id, row.matched_via) == (1, show.id, MATCHED_VIA_NAME)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", RECOMMENDATION_SET_STATUSES)
async def test_every_declared_status_is_accepted(session, status):
    """The constant the writers import and the CHECK constraint are one thing.

    `weekly_recommendations` dispatches on these four; a value it can write that
    the table rejects would abort the pass for that user at the last statement.
    """
    user = await _user(session, email=f"{status}@example.com")
    session.add(_set(user, status=status))
    await session.commit()


@pytest.mark.asyncio
async def test_an_undeclared_status_is_rejected(session):
    user = await _user(session)
    session.add(_set(user, status="pending"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("matched_via", RECOMMENDATION_MATCHED_VIA)
async def test_every_declared_resolution_tier_is_accepted(session, matched_via):
    user = await _user(session, email=f"{matched_via}@example.com")
    show = Show(id=910002, name="Tiered")
    session.add(show)
    rec_set = _set(user)
    session.add(rec_set)
    await session.flush()
    session.add(
        UserRecommendation(
            set_id=rec_set.id,
            rank=1,
            show_id=show.id,
            reason="r",
            matched_via=matched_via,
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_an_undeclared_resolution_tier_is_rejected(session):
    user = await _user(session)
    show = Show(id=910003, name="Fuzzy")
    session.add(show)
    rec_set = _set(user)
    session.add(rec_set)
    await session.flush()
    session.add(
        UserRecommendation(
            set_id=rec_set.id, rank=1, show_id=show.id, reason="r", matched_via="trigram"
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_a_rank_appears_once_in_a_set(session):
    """The model's own ordering is the only ordering there is."""
    user = await _user(session)
    session.add_all([Show(id=910004, name="A"), Show(id=910005, name="B")])
    rec_set = _set(user)
    session.add(rec_set)
    await session.flush()
    session.add(
        UserRecommendation(
            set_id=rec_set.id, rank=1, show_id=910004, reason="r", matched_via=MATCHED_VIA_NAME
        )
    )
    await session.commit()
    session.add(
        UserRecommendation(
            set_id=rec_set.id, rank=1, show_id=910005, reason="r", matched_via=MATCHED_VIA_AKA
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_deleting_the_user_takes_their_sets_and_rows(session):
    """The half `watch_archive` deliberately does not have.

    A set holds `compiled_payload`, which is a second copy of the user's watch
    history. The `user_id` CASCADE is what keeps account deletion complete, and
    the rows go with the set rather than being left orphaned by it.
    """
    user = await _user(session)
    show = Show(id=910006, name="Cascading")
    session.add(show)
    rec_set = _set(user)
    session.add(rec_set)
    await session.flush()
    session.add(
        UserRecommendation(
            set_id=rec_set.id, rank=1, show_id=show.id, reason="r", matched_via=MATCHED_VIA_NAME
        )
    )
    await session.commit()

    await session.execute(delete(User).where(User.id == user.id))
    await session.commit()

    assert (await session.execute(select(UserRecommendationSet))).all() == []
    assert (await session.execute(select(UserRecommendation))).all() == []


@pytest.mark.asyncio
async def test_deleting_the_set_takes_its_rows(session):
    user = await _user(session)
    show = Show(id=910007, name="Superseded")
    session.add(show)
    rec_set = _set(user)
    session.add(rec_set)
    await session.flush()
    session.add(
        UserRecommendation(
            set_id=rec_set.id, rank=1, show_id=show.id, reason="r", matched_via=MATCHED_VIA_NAME
        )
    )
    await session.commit()

    await session.execute(
        delete(UserRecommendationSet).where(UserRecommendationSet.id == rec_set.id)
    )
    await session.commit()

    assert (await session.execute(select(UserRecommendation))).all() == []


@pytest.mark.asyncio
async def test_a_recommendation_hangs_off_catalog_and_cascades_from_it(session):
    """A suggestion points at `catalog.show`, so it goes when the show does.

    The tombstone pass and `orphan_retire` both delete shows in bulk; a
    recommendation left pointing at nothing would surface as a broken card, and
    the set's own row survives because the 25/12 headroom absorbs the loss.
    """
    user = await _user(session)
    show = Show(id=910008, name="Retired")
    session.add(show)
    rec_set = _set(user)
    session.add(rec_set)
    await session.flush()
    session.add(
        UserRecommendation(
            set_id=rec_set.id, rank=1, show_id=show.id, reason="r", matched_via=MATCHED_VIA_NAME
        )
    )
    await session.commit()

    await session.execute(text("DELETE FROM catalog.show WHERE id = :s"), {"s": show.id})
    await session.commit()

    assert (await session.execute(select(UserRecommendation))).all() == []
    assert (await session.execute(select(UserRecommendationSet))).scalars().all() != []

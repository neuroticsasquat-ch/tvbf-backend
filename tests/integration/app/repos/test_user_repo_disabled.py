"""`user_repo`'s two NEU-1162 predicates, at the repo rather than through a
route: the weekly recommendations work list (§9, AC 14) and the set-shaped
filter the two connection seams read through (§4)."""

from datetime import UTC, datetime

import pytest

from tvbf.app.repos import user_repo


@pytest.mark.asyncio
async def test_list_ids_excludes_disabled_users(session, make_user):
    """AC 14. A disabled account cannot see a recommendation, so the DeepInfra
    call their changed taste would buy is money spent on nobody."""
    active = await make_user(email="active@example.com")
    disabled = await make_user(email="disabled@example.com")
    disabled.disabled_at = datetime.now(UTC)
    await session.commit()

    ids = await user_repo.list_ids(session)
    assert active.id in ids
    assert disabled.id not in ids


@pytest.mark.asyncio
async def test_list_ids_includes_them_again_once_cleared(session, make_user):
    user = await make_user(email="u@example.com")
    user.disabled_at = datetime.now(UTC)
    await session.commit()
    assert user.id not in await user_repo.list_ids(session)

    user.disabled_at = None
    await session.commit()
    assert user.id in await user_repo.list_ids(session)


@pytest.mark.asyncio
async def test_filter_enabled_drops_disabled_and_unknown_ids(session, make_user):
    from uuid import uuid4

    active = await make_user(email="active@example.com")
    disabled = await make_user(email="disabled@example.com")
    disabled.disabled_at = datetime.now(UTC)
    await session.commit()

    kept = await user_repo.filter_enabled(session, {active.id, disabled.id, uuid4()})
    assert kept == {active.id}


@pytest.mark.asyncio
async def test_filter_enabled_of_nothing_is_nothing(session):
    """The empty set short-circuits rather than issuing a query with an empty
    IN list."""
    assert await user_repo.filter_enabled(session, set()) == set()

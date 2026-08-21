"""Claiming, changing and releasing a handle (NEU-1163 §4, §6, §7, §8)."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from tests.fixtures.handles import new_handle
from tvbf.app.models import HandleRelease, User
from tvbf.app.repos import handle_release_repo
from tvbf.main import app


@pytest.fixture
async def client(session):
    """An anonymous client, for the signup half. `authed_client` cannot stand in
    — signup is the one write site reached without a session."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c


def _signup_body(invite: str, *, email: str = "new@example.com", handle: str = "newcomer"):
    return {
        "email": email,
        "password": "hunter2hunter2",
        "display_name": "New",
        "handle": handle,
        "invite_code": invite,
    }


# ---------------------------------------------------------------------------
# Signup (§6.1, §6.3, §6.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signup_stores_the_normalised_handle(client, make_invite):
    invite = await make_invite()
    r = await client.post("/auth/signup", json=_signup_body(invite, handle="  @NewComer "))
    assert r.status_code == 201
    assert r.json()["handle"] == "newcomer"


@pytest.mark.asyncio
async def test_signup_with_a_taken_handle_is_409_and_creates_no_account(
    client, session, make_invite, make_user
):
    await make_user(email="held@example.com", handle="taken_one")
    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json=_signup_body(invite, email="other@example.com", handle="taken_one"),
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "handle_unavailable"
    rows = (
        (await session.execute(select(User).where(User.email == "other@example.com")))
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_signup_with_a_handle_someone_else_released_is_the_same_409(
    client, session, make_invite, make_user
):
    """§6.3. Byte-identical to the taken case, deliberately: distinguishing the
    two turns this into a *has this handle ever existed* oracle, including for
    accounts since deleted."""
    owner = await make_user(email="owner@example.com", handle="was_mine")
    await handle_release_repo.record(session, handle="was_mine", user_id=owner.id)
    await session.commit()

    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json=_signup_body(invite, email="stranger@example.com", handle="was_mine"),
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "handle_unavailable"


@pytest.mark.asyncio
async def test_a_duplicate_email_still_names_the_email(client, make_invite, make_user):
    """§6.4. The blanket `except IntegrityError → EmailInUse` became wrong the
    moment `app.user` carried a second unique constraint; both refusals must
    still name the right field."""
    await make_user(email="dupe@example.com")
    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json=_signup_body(invite, email="dupe@example.com", handle="fresh_one"),
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "email_in_use"


@pytest.mark.asyncio
async def test_the_integrity_error_branch_names_the_handle(session, make_invite, make_user):
    """The race fallback, reached with the pre-check bypassed: two signups
    claiming one handle in the same instant must both get a correct answer, and
    `uq_user_handle` is the only thing that can decide the second one."""
    from tvbf.app.errors import HandleUnavailable
    from tvbf.app.services import account_service, handle_service

    await make_user(email="racer@example.com", handle="raced_one")
    invite = await make_invite()

    async def _no_precheck(*args, **kwargs):
        return None

    real = handle_service.ensure_claimable
    handle_service.ensure_claimable = _no_precheck
    try:
        with pytest.raises(HandleUnavailable):
            await account_service.signup(
                session,
                email="loser@example.com",
                password="hunter2hunter2",
                display_name="Loser",
                handle="raced_one",
                invite_code=invite,
                ttl_days=30,
                user_agent=None,
                ip=None,
            )
    finally:
        handle_service.ensure_claimable = real


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handle", ["ab", "a_very_long_handle_of_thirty_one", "9lives", "_tom", "tom-boone", "admin"]
)
async def test_signup_refuses_a_bad_shape_with_a_field_scoped_422(client, make_invite, handle):
    invite = await make_invite()
    r = await client.post("/auth/signup", json=_signup_body(invite, handle=handle))
    assert r.status_code == 422
    assert any(d["loc"] == ["body", "handle"] for d in r.json()["detail"])


# ---------------------------------------------------------------------------
# PATCH /me/handle (§6.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changing_writes_the_release_row_and_returns_the_new_value(authed_client, session):
    old = authed_client.user.handle
    r = await authed_client.patch("/me/handle", json={"handle": "brand_new"})
    assert r.status_code == 200
    assert r.json()["handle"] == "brand_new"

    row = await handle_release_repo.get(session, old)
    assert row is not None
    assert row.user_id == authed_client.user.id


@pytest.mark.asyncio
@pytest.mark.parametrize("handle", ["ab", "9lives", "tom-boone", "settings", "user_3f4a2b1c"])
async def test_the_same_rules_apply_at_the_second_door(authed_client, handle):
    r = await authed_client.patch("/me/handle", json={"handle": handle})
    assert r.status_code == 422
    assert any(d["loc"] == ["body", "handle"] for d in r.json()["detail"])


@pytest.mark.asyncio
async def test_a_handle_a_live_account_holds_is_409(authed_client, make_user):
    await make_user(email="holder@example.com", handle="already_held")
    r = await authed_client.patch("/me/handle", json={"handle": "already_held"})
    assert r.status_code == 409
    assert r.json()["detail"] == "handle_unavailable"


@pytest.mark.asyncio
async def test_the_original_owner_may_reclaim_but_a_stranger_never_may(
    authed_client, session, make_user
):
    """§4.2's same-owner exemption is what keeps "never reusable" from being a
    trap: changing your mind is free, and only strangers are refused."""
    first = authed_client.user.handle
    assert (
        await authed_client.patch("/me/handle", json={"handle": "second_one"})
    ).status_code == 200
    reclaimed = await authed_client.patch("/me/handle", json={"handle": first})
    assert reclaimed.status_code == 200
    assert reclaimed.json()["handle"] == first

    # `second_one` is now released, and belongs to nobody else forever.
    other = await make_user(email="other@example.com", handle="other_one")
    from tvbf.app.errors import HandleUnavailable
    from tvbf.app.services import handle_service

    with pytest.raises(HandleUnavailable):
        await handle_service.ensure_claimable(session, handle="second_one", claimant_id=other.id)


@pytest.mark.asyncio
async def test_setting_the_handle_you_already_hold_is_a_no_op(authed_client, session):
    current = authed_client.user.handle
    r = await authed_client.patch("/me/handle", json={"handle": current})
    assert r.status_code == 200
    assert r.json()["handle"] == current
    assert (await handle_release_repo.get(session, current)) is None


@pytest.mark.asyncio
async def test_the_fourth_change_inside_the_window_is_429(authed_client):
    """§6.2. Three rather than one because a new user fixing a typo, then
    fixing their mind, should not be locked out for a month."""
    for handle in ("first_new", "second_new", "third_new"):
        assert (await authed_client.patch("/me/handle", json={"handle": handle})).status_code == 200
    r = await authed_client.patch("/me/handle", json={"handle": "fourth_new"})
    assert r.status_code == 429
    assert r.json()["detail"] == "rate_limited"
    assert r.headers["Retry-After"] == str(30 * 24 * 60 * 60)


@pytest.mark.asyncio
async def test_a_release_outside_the_window_does_not_count(authed_client, session):
    for handle in ("aged_one", "aged_two", "aged_three"):
        assert (await authed_client.patch("/me/handle", json={"handle": handle})).status_code == 200
    long_ago = datetime.now(UTC) - timedelta(days=40)
    await session.execute(update(HandleRelease).values(released_at=long_ago))
    await session.commit()
    assert (
        await authed_client.patch("/me/handle", json={"handle": "fresh_again"})
    ).status_code == 200


@pytest.mark.asyncio
async def test_the_route_requires_csrf(authed_client):
    r = await authed_client.patch(
        "/me/handle", json={"handle": "no_token"}, headers={"X-CSRF-Token": ""}
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /me (§4.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_an_account_orphans_its_handle_rather_than_freeing_it(
    authed_client, session, make_user
):
    """Without this, self-service account deletion would be a supported route
    to handing your identity to a stranger."""
    handle = authed_client.user.handle
    r = await authed_client.request("DELETE", "/me", json={"password": "hunter2hunter2"})
    assert r.status_code == 204

    session.expire_all()
    row = await handle_release_repo.get(session, handle)
    assert row is not None
    assert row.user_id is None

    from tvbf.app.errors import HandleUnavailable
    from tvbf.app.services import handle_service

    other = await make_user(email="claimant@example.com", handle=new_handle())
    with pytest.raises(HandleUnavailable):
        await handle_service.ensure_claimable(session, handle=handle, claimant_id=other.id)


# ---------------------------------------------------------------------------
# Search (§8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_matches_a_handle_substring_and_tolerates_the_sigil(authed_client, make_user):
    await make_user(email="tb@example.com", display_name="Thomas", handle="tom_b", verified=True)
    for q in ("tom_b", "@tom_b", "om_"):
        r = await authed_client.get("/users/search", params={"q": q})
        assert r.status_code == 200
        assert [row["handle"] for row in r.json()] == ["tom_b"], q


@pytest.mark.asyncio
async def test_an_exact_handle_match_sorts_first(authed_client, make_user):
    """You were given `@tom_b` precisely because three people are called Tom; a
    list that buries the exact match alphabetically has answered the wrong
    question."""
    await make_user(email="a@example.com", display_name="Aaron", handle="tom_bb", verified=True)
    await make_user(email="b@example.com", display_name="Beth", handle="tom_b", verified=True)
    r = await authed_client.get("/users/search", params={"q": "tom_b"})
    assert [row["handle"] for row in r.json()][0] == "tom_b"


@pytest.mark.asyncio
async def test_display_name_search_and_exact_email_still_work(authed_client, make_user):
    await make_user(
        email="boone@example.com", display_name="Tom Boone", handle="tb_one", verified=True
    )
    by_name = await authed_client.get("/users/search", params={"q": "boone"})
    assert [row["display_name"] for row in by_name.json()] == ["Tom Boone"]

    by_email = await authed_client.get("/users/search", params={"q": "boone@example.com"})
    assert [row["handle"] for row in by_email.json()] == ["tb_one"]

    # Email stays exact-match-only: a substring must not enumerate addresses.
    partial = await authed_client.get("/users/search", params={"q": "boone@example"})
    assert partial.json() == []


# ---------------------------------------------------------------------------
# Exposure (§7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_payload_that_names_a_user_carries_the_handle(
    authed_client, session, make_user
):
    """§7 in one test. Every construction site already holds a full `User` row,
    so none of these gained a query — the exposure is free, which is why the
    list is wider than AC 6 asked for."""
    me = authed_client.user
    friend = await make_user(
        email="friend@example.com", display_name="Friend", handle="friend_one", verified=True
    )

    # UserOut / AuthedUserOut, and the export beside them.
    assert (await authed_client.get("/me")).json()["handle"] == me.handle

    # UserSearchResult
    found = (await authed_client.get("/users/search", params={"q": "friend_one"})).json()
    assert found[0]["handle"] == "friend_one"

    # UserBrief, through a connection request the friend can see on their side.
    request = await authed_client.post(
        "/connection-requests", json={"addressee_id": str(friend.id)}
    )
    assert request.status_code == 201
    body = request.json()
    assert body["requester"]["handle"] == me.handle
    assert body["addressee"]["handle"] == "friend_one"

    # AdminUserOut
    me_row = await session.get(User, me.id)
    assert me_row is not None
    me_row.is_admin = True
    await session.commit()
    admin_rows = (await authed_client.get("/admin/users")).json()
    assert all("handle" in row for row in admin_rows)


@pytest.mark.asyncio
async def test_the_export_carries_the_handle(authed_client):
    """It is account data, and the export is this app's portability answer."""
    r = await authed_client.get("/me/export")
    assert r.status_code == 200
    assert r.json()["account"]["handle"] == authed_client.user.handle

"""Integration tests for /users/search."""

import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.app.services import connection_service
from tvbf.main import app


@pytest.mark.asyncio
async def test_search_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/users/search", params={"q": "alice"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_search_rejects_short_query(authed_client):
    r = await authed_client.get("/users/search", params={"q": "a"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_search_rejects_empty_query(authed_client):
    r = await authed_client.get("/users/search", params={"q": ""})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_search_substring_on_display_name(authed_client, make_user):
    target = await make_user(
        email="alice@example.com", display_name="Alice Anderson", verified=True
    )
    await make_user(email="bob@example.com", display_name="Bob Brown", verified=True)

    r = await authed_client.get("/users/search", params={"q": "ander"})
    assert r.status_code == 200
    body = r.json()
    ids = {row["id"] for row in body}
    assert str(target.id) in ids


@pytest.mark.asyncio
async def test_search_email_exact_only(authed_client, make_user):
    target = await make_user(email="needle@example.com", display_name="Hidden Name", verified=True)

    # Substring on email must NOT match.
    r = await authed_client.get("/users/search", params={"q": "needl"})
    assert r.status_code == 200
    assert all(row["id"] != str(target.id) for row in r.json())

    # Exact email match (case-insensitive via CITEXT).
    r = await authed_client.get("/users/search", params={"q": "NEEDLE@example.com"})
    assert r.status_code == 200
    assert any(row["id"] == str(target.id) for row in r.json())


@pytest.mark.asyncio
async def test_search_excludes_self(authed_client):
    me_id = str(authed_client.user.id)  # type: ignore[attr-defined]
    me_name = authed_client.user.display_name  # type: ignore[attr-defined]

    r = await authed_client.get("/users/search", params={"q": me_name[:4]})
    assert r.status_code == 200
    assert all(row["id"] != me_id for row in r.json())


@pytest.mark.asyncio
async def test_search_excludes_blocked_either_way(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    blocked_by_me = await make_user(
        email="bbm@example.com", display_name="BlockedByMe Person", verified=True
    )
    blocked_me = await make_user(
        email="bm@example.com", display_name="BlockedMe Person", verified=True
    )
    visible = await make_user(email="ok@example.com", display_name="Visible Person", verified=True)

    await connection_service.block(session, blocker_id=me.id, blocked_id=blocked_by_me.id)
    await connection_service.block(session, blocker_id=blocked_me.id, blocked_id=me.id)

    r = await authed_client.get("/users/search", params={"q": "Person"})
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()}
    assert str(visible.id) in ids
    assert str(blocked_by_me.id) not in ids
    assert str(blocked_me.id) not in ids


@pytest.mark.asyncio
async def test_search_case_insensitive_display_name(authed_client, make_user):
    target = await make_user(email="zelda@example.com", display_name="Zelda Z", verified=True)
    r = await authed_client.get("/users/search", params={"q": "ZELDA"})
    assert r.status_code == 200
    assert any(row["id"] == str(target.id) for row in r.json())


@pytest.mark.asyncio
async def test_search_caps_at_20(authed_client, make_user):
    for i in range(25):
        await make_user(
            email=f"user{i}@example.com", display_name=f"BulkPerson{i:02d}", verified=True
        )

    r = await authed_client.get("/users/search", params={"q": "BulkPerson"})
    assert r.status_code == 200
    assert len(r.json()) == 20


# ---------------------------------------------------------------------------
# Verification gate on discoverability (NEU-1161 §3.1, §3.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_excludes_unverified_users(authed_client, make_user):
    verified = await make_user(
        email="seen@example.com", display_name="Seen Searchable", verified=True
    )
    unverified = await make_user(email="unseen@example.com", display_name="Unseen Searchable")

    r = await authed_client.get("/users/search", params={"q": "Searchable"})
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()}
    assert str(verified.id) in ids
    assert str(unverified.id) not in ids


@pytest.mark.asyncio
async def test_search_excludes_unverified_user_on_exact_email(authed_client, make_user):
    unverified = await make_user(email="exact@example.com", display_name="Exact Match")

    r = await authed_client.get("/users/search", params={"q": "exact@example.com"})
    assert r.status_code == 200
    assert all(row["id"] != str(unverified.id) for row in r.json())


@pytest.mark.asyncio
async def test_search_excludes_unverified_even_when_connected(authed_client, make_user, session):
    """The exclusion is blanket: an accepted connection does not exempt it.
    The friend stays reachable through /me/connections."""
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="friend@example.com", display_name="Friend Person")
    request = await connection_service.send_request(
        session, requester_id=friend.id, addressee_id=me.id
    )
    await connection_service.accept(session, id=request.id, accepting_user_id=me.id)
    await session.commit()

    r = await authed_client.get("/users/search", params={"q": "Friend Person"})
    assert r.status_code == 200
    assert all(row["id"] != str(friend.id) for row in r.json())


@pytest.mark.asyncio
async def test_search_returns_a_full_page_despite_unverified_rows(authed_client, make_user):
    """The predicate is in the SQL, not the router: interleaving unverified
    rows must not eat into the page."""
    for i in range(25):
        await make_user(
            email=f"pageverified{i}@example.com",
            display_name=f"PagePerson{i:02d}V",
            verified=True,
        )
        await make_user(email=f"pageunverified{i}@example.com", display_name=f"PagePerson{i:02d}U")

    r = await authed_client.get("/users/search", params={"q": "PagePerson"})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 20
    assert all(row["display_name"].endswith("V") for row in body)


@pytest.mark.asyncio
async def test_search_works_for_an_unverified_caller(unverified_client, make_user):
    """Searching is neither of the two gated things — the route answers 200."""
    target = await make_user(
        email="target@example.com", display_name="Target Person", verified=True
    )

    r = await unverified_client.get("/users/search", params={"q": "Target"})
    assert r.status_code == 200
    assert any(row["id"] == str(target.id) for row in r.json())

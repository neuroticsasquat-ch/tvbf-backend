"""`POST /connection-requests` under its budget (NEU-1157 §5, §6).

The check order is what most of this file is about: **429 → cooldown 409 → 404 →
400 → pair 409 → 201**. Throttled last, the endpoint becomes a free silent
oracle — `409` for anyone who already has a relationship with you or has blocked
you, `429` for everyone else, with nothing created and no cost.
"""

from uuid import uuid4

import pytest

from tvbf.app.models import ConnectionRequestLog
from tvbf.app.repos import connection_request_log_repo as ledger
from tvbf.app.services import connection_service
from tvbf.config import Settings, get_settings
from tvbf.main import app


@pytest.fixture
def throttle_max():
    """Lower the ceiling for one test, then restore the real settings.

    A dependency override rather than a patched environment: `get_settings` is
    `lru_cache`d, so an env var set here would leak into every later test in the
    session. Tests that want the *default* ceiling simply don't request this.
    """

    def _apply(max_attempts: int) -> None:
        settings = Settings(CONNECTION_REQUEST_THROTTLE_MAX=max_attempts)  # type: ignore[call-arg]
        app.dependency_overrides[get_settings] = lambda: settings

    yield _apply
    app.dependency_overrides.pop(get_settings, None)


async def _count_rows(session) -> int:
    from sqlalchemy import func, select

    return (
        await session.execute(select(func.count()).select_from(ConnectionRequestLog))
    ).scalar_one()


async def _age_out_of_the_daily_window(session) -> None:
    """Backdate every ledger row two days.

    The reputation window is 30 days and the cap's is 24 hours, so this leaves
    rows counting toward the ceiling *selection* while freeing the budget they
    consumed — which is the only way to watch a demotion take effect rather than
    just watching the seed data hit the cap it was built from.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    await session.execute(
        update(ConnectionRequestLog).values(created_at=datetime.now(UTC) - timedelta(days=2))
    )
    await session.commit()


@pytest.mark.asyncio
async def test_over_the_cap_returns_429_with_retry_after(authed_client, make_user, throttle_max):
    throttle_max(2)
    for i in range(2):
        target = await make_user(email=f"t{i}@example.com", display_name=f"T{i}")
        r = await authed_client.post("/connection-requests", json={"addressee_id": str(target.id)})
        assert r.status_code == 201

    third = await make_user(email="third@example.com", display_name="Third")
    r = await authed_client.post("/connection-requests", json={"addressee_id": str(third.id)})
    assert r.status_code == 429
    assert r.json()["detail"] == "rate_limited"
    assert r.headers["Retry-After"] == str(1440 * 60)


@pytest.mark.asyncio
async def test_cancel_and_resend_consumes_two_slots(authed_client, make_user, throttle_max):
    """AC 3 end to end."""
    throttle_max(2)
    a = await make_user(email="a@example.com", display_name="A")
    b = await make_user(email="b@example.com", display_name="B")

    first = await authed_client.post("/connection-requests", json={"addressee_id": str(a.id)})
    assert first.status_code == 201
    assert (
        await authed_client.delete(f"/connection-requests/{first.json()['id']}")
    ).status_code == 204

    assert (
        await authed_client.post("/connection-requests", json={"addressee_id": str(a.id)})
    ).status_code == 201
    assert (
        await authed_client.post("/connection-requests", json={"addressee_id": str(b.id)})
    ).status_code == 429


@pytest.mark.asyncio
async def test_at_the_cap_every_target_answers_the_same_429(
    authed_client, make_user, session, throttle_max
):
    """AC 5 — the non-oracle property. A non-existent user, oneself, and an
    already-related user are indistinguishable once the budget is spent."""
    throttle_max(1)
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="friend@example.com", display_name="Friend")
    # Spends the single slot *and* establishes the pair, so the three probes
    # below differ in every way except the answer they get.
    await connection_service.send_request(session, requester_id=me.id, addressee_id=friend.id)

    for target_id in (str(uuid4()), str(me.id), str(friend.id)):
        r = await authed_client.post("/connection-requests", json={"addressee_id": target_id})
        assert r.status_code == 429, target_id
        assert r.json()["detail"] == "rate_limited"


@pytest.mark.asyncio
async def test_nothing_refused_writes_a_ledger_row(authed_client, make_user, session, throttle_max):
    """AC 7 — every refusal costs no budget: 404, 400, pair 409 and cooldown 409
    leave the ledger exactly as they found it. The 429 arm is
    `test_a_429_writes_no_ledger_row` below, which needs the opposite ceiling."""
    throttle_max(10)
    me = authed_client.user  # type: ignore[attr-defined]
    related = await make_user(email="rel@example.com", display_name="Rel")
    await connection_service.send_request(session, requester_id=me.id, addressee_id=related.id)
    decliner = await make_user(email="dec@example.com", display_name="Dec")
    declined = await connection_service.send_request(
        session, requester_id=me.id, addressee_id=decliner.id
    )
    await connection_service.delete_pending_request(session, id=declined.id, caller_id=decliner.id)
    before = await _count_rows(session)

    for target_id, expected in (
        (str(uuid4()), 404),
        (str(me.id), 400),
        (str(related.id), 409),
        (str(decliner.id), 409),
    ):
        r = await authed_client.post("/connection-requests", json={"addressee_id": target_id})
        assert r.status_code == expected, target_id
    assert await _count_rows(session) == before


@pytest.mark.asyncio
async def test_a_429_writes_no_ledger_row(authed_client, make_user, session, throttle_max):
    """The other half of AC 7, in its own test because it needs the ceiling spent
    where the one above needs it generous. A refused request must not deepen the
    hole it was refused from — otherwise a throttled caller could hold themselves
    at the cap indefinitely, which is `auth_throttle`'s "a request rejected with
    429 is not itself recorded" one budget over."""
    throttle_max(1)
    me = authed_client.user  # type: ignore[attr-defined]
    spent = await make_user(email="one@example.com", display_name="One")
    await connection_service.send_request(session, requester_id=me.id, addressee_id=spent.id)
    before = await _count_rows(session)

    for _ in range(3):
        target = await make_user(
            email=f"over{_}@example.com", display_name=f"Over{_}", verified=True
        )
        r = await authed_client.post("/connection-requests", json={"addressee_id": str(target.id)})
        assert r.status_code == 429
    assert await _count_rows(session) == before


@pytest.mark.asyncio
async def test_at_the_cap_a_blocked_relationship_answers_the_same_429(
    authed_client, make_user, session, throttle_max
):
    """AC 5 names `blocked` explicitly, and it is the variant that matters most:
    without the throttle running first, this pair is the one the `409`/`429`
    split would identify for free."""
    throttle_max(1)
    me = authed_client.user  # type: ignore[attr-defined]
    blocker = await make_user(email="blocker@example.com", display_name="Blocker")
    await connection_service.block(session, blocker_id=blocker.id, blocked_id=me.id)
    spent = await make_user(email="budget@example.com", display_name="Budget")
    assert (
        await authed_client.post("/connection-requests", json={"addressee_id": str(spent.id)})
    ).status_code == 201

    stranger = await make_user(email="stranger@example.com", display_name="Stranger")
    for target_id in (str(blocker.id), str(stranger.id)):
        r = await authed_client.post("/connection-requests", json={"addressee_id": target_id})
        assert r.status_code == 429, target_id
        assert r.json()["detail"] == "rate_limited"


@pytest.mark.asyncio
async def test_declined_within_the_cooldown_is_an_indistinguishable_409(
    authed_client, make_user, session
):
    """AC 6. Same status and same body as the pair conflict — the caller cannot
    tell "they declined me" from "we are already related"."""
    me = authed_client.user  # type: ignore[attr-defined]
    decliner = await make_user(email="no@example.com", display_name="No")
    row = await connection_service.send_request(
        session, requester_id=me.id, addressee_id=decliner.id
    )
    await connection_service.delete_pending_request(session, id=row.id, caller_id=decliner.id)

    r = await authed_client.post("/connection-requests", json={"addressee_id": str(decliner.id)})
    assert r.status_code == 409
    assert r.json()["detail"] == "connection_exists"


@pytest.mark.asyncio
async def test_the_cooldown_lapses(authed_client, make_user, session):
    """The same request succeeds once the decline has aged past the window."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    me = authed_client.user  # type: ignore[attr-defined]
    decliner = await make_user(email="later@example.com", display_name="Later")
    row = await connection_service.send_request(
        session, requester_id=me.id, addressee_id=decliner.id
    )
    await connection_service.delete_pending_request(session, id=row.id, caller_id=decliner.id)
    await session.execute(
        update(ConnectionRequestLog)
        .where(ConnectionRequestLog.outcome == ledger.DECLINED)
        .values(resolved_at=datetime.now(UTC) - timedelta(days=31))
    )
    await session.commit()

    r = await authed_client.post("/connection-requests", json={"addressee_id": str(decliner.id)})
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_a_cancelled_request_does_not_start_a_cooldown(authed_client, make_user, session):
    """Your own withdrawal must not lock you out of correcting it."""
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="oops@example.com", display_name="Oops")
    row = await connection_service.send_request(session, requester_id=me.id, addressee_id=other.id)
    await connection_service.delete_pending_request(session, id=row.id, caller_id=me.id)

    r = await authed_client.post("/connection-requests", json={"addressee_id": str(other.id)})
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_the_cooldown_does_not_bar_the_decliner(authed_client, make_user, session):
    """It is directional: the person who said no may still ask."""
    me = authed_client.user  # type: ignore[attr-defined]
    asker = await make_user(email="asker@example.com", display_name="Asker", verified=True)
    row = await connection_service.send_request(session, requester_id=asker.id, addressee_id=me.id)
    await connection_service.delete_pending_request(session, id=row.id, caller_id=me.id)

    r = await authed_client.post("/connection-requests", json={"addressee_id": str(asker.id)})
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_accept_decline_and_cancel_are_never_throttled(
    authed_client, make_user, session, throttle_max
):
    """AC 4 — the cap governs creation only, and a caller who has spent it can
    still work through their inbox."""
    throttle_max(1)
    me = authed_client.user  # type: ignore[attr-defined]
    spent = await make_user(email="s@example.com", display_name="S")
    assert (
        await authed_client.post("/connection-requests", json={"addressee_id": str(spent.id)})
    ).status_code == 201

    inbound = await make_user(email="in@example.com", display_name="In")
    to_accept = await connection_service.send_request(
        session, requester_id=inbound.id, addressee_id=me.id
    )
    other = await make_user(email="in2@example.com", display_name="In2")
    to_decline = await connection_service.send_request(
        session, requester_id=other.id, addressee_id=me.id
    )

    assert (
        await authed_client.post(f"/connection-requests/{to_accept.id}/accept")
    ).status_code == 200
    assert (await authed_client.delete(f"/connection-requests/{to_decline.id}")).status_code == 204


@pytest.mark.asyncio
async def test_a_demoted_account_is_held_to_the_floor(authed_client, make_user, session):
    """AC 2 end to end: five resolved requests, three of them declined, and the
    ceiling drops from 10 to 2."""
    me = authed_client.user  # type: ignore[attr-defined]
    for i in range(3):
        target = await make_user(email=f"d{i}@example.com", display_name=f"D{i}")
        row = await connection_service.send_request(
            session, requester_id=me.id, addressee_id=target.id
        )
        await connection_service.delete_pending_request(session, id=row.id, caller_id=target.id)
    for i in range(2):
        target = await make_user(email=f"y{i}@example.com", display_name=f"Y{i}")
        row = await connection_service.send_request(
            session, requester_id=me.id, addressee_id=target.id
        )
        await connection_service.accept(session, id=row.id, accepting_user_id=target.id)
    await _age_out_of_the_daily_window(session)

    for i in range(2):
        fresh = await make_user(email=f"n{i}@example.com", display_name=f"N{i}")
        assert (
            await authed_client.post("/connection-requests", json={"addressee_id": str(fresh.id)})
        ).status_code == 201

    last = await make_user(email="last@example.com", display_name="Last")
    r = await authed_client.post("/connection-requests", json={"addressee_id": str(last.id)})
    assert r.status_code == 429
    assert r.json()["detail"] == "rate_limited"


@pytest.mark.asyncio
async def test_a_good_acceptance_rate_keeps_the_full_ceiling(authed_client, make_user, session):
    """The other half of AC 2 — five resolved requests, only one adverse."""
    me = authed_client.user  # type: ignore[attr-defined]
    target = await make_user(email="one@example.com", display_name="One")
    row = await connection_service.send_request(session, requester_id=me.id, addressee_id=target.id)
    await connection_service.delete_pending_request(session, id=row.id, caller_id=target.id)
    for i in range(4):
        friend = await make_user(email=f"f{i}@example.com", display_name=f"F{i}")
        row = await connection_service.send_request(
            session, requester_id=me.id, addressee_id=friend.id
        )
        await connection_service.accept(session, id=row.id, accepting_user_id=friend.id)
    await _age_out_of_the_daily_window(session)

    for i in range(5):
        fresh = await make_user(email=f"g{i}@example.com", display_name=f"G{i}")
        assert (
            await authed_client.post("/connection-requests", json={"addressee_id": str(fresh.id)})
        ).status_code == 201, i

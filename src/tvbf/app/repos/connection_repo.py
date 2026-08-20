from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import Connection, User


async def find_pair(db: AsyncSession, user_a: UUID, user_b: UUID) -> Connection | None:
    """Return the connection row for the unordered pair, or None."""
    stmt = select(Connection).where(
        or_(
            and_(
                Connection.requester_id == user_a,
                Connection.addressee_id == user_b,
            ),
            and_(
                Connection.requester_id == user_b,
                Connection.addressee_id == user_a,
            ),
        )
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def get(db: AsyncSession, connection_id: UUID) -> Connection | None:
    return (
        await db.execute(select(Connection).where(Connection.id == connection_id))
    ).scalar_one_or_none()


async def list_accepted_for_user(db: AsyncSession, user_id: UUID) -> list[tuple[Connection, UUID]]:
    """All accepted connections for the user, paired with the *other* user_id.

    Disabled accounts are **not** filtered here (NEU-1162 §4.1). This is the row
    `GET /me/connections` renders, and an existing accepted friend is not the
    stranger invisibility exists to protect: hiding the row makes a connection
    vanish and re-appear for someone who did nothing wrong. The *engagement*
    surfaces filter instead, in `connection_service.accepted_friend_ids`.
    """
    rows = (
        (
            await db.execute(
                select(Connection).where(
                    Connection.state == "accepted",
                    or_(
                        Connection.requester_id == user_id,
                        Connection.addressee_id == user_id,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    return [(c, c.addressee_id if c.requester_id == user_id else c.requester_id) for c in rows]


async def list_pending_for_user(
    db: AsyncSession, user_id: UUID
) -> tuple[list[Connection], list[Connection]]:
    """Return (incoming, outgoing) pending requests for the user.

    A request whose *other* party is disabled is dropped from both lists
    (NEU-1162 §4): the request a griefer sent before being disabled is sitting
    in a stranger's inbox, and it is exactly the harassment disabling exists to
    stop. The row is not deleted — clearing the flag brings it back, with no
    backfill step, which is the reversibility the whole feature is buying.

    The predicate lives here rather than in the router because it is a join, not
    a post-filter: the caller has one query's worth of rows and no user rows to
    test.
    """
    other_id = case(
        (Connection.requester_id == user_id, Connection.addressee_id),
        else_=Connection.requester_id,
    )
    rows = (
        (
            await db.execute(
                select(Connection)
                .join(User, User.id == other_id)
                .where(
                    Connection.state == "pending",
                    or_(
                        Connection.requester_id == user_id,
                        Connection.addressee_id == user_id,
                    ),
                    User.disabled_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    incoming = [c for c in rows if c.addressee_id == user_id]
    outgoing = [c for c in rows if c.requester_id == user_id]
    return incoming, outgoing


async def list_blocked_user_ids(db: AsyncSession, user_id: UUID) -> set[UUID]:
    """All user_ids that share a blocked row with `user_id` (either direction)."""
    rows = (
        await db.execute(
            select(Connection.requester_id, Connection.addressee_id).where(
                Connection.state == "blocked",
                or_(
                    Connection.requester_id == user_id,
                    Connection.addressee_id == user_id,
                ),
            )
        )
    ).all()
    out: set[UUID] = set()
    for requester_id, addressee_id in rows:
        out.add(addressee_id if requester_id == user_id else requester_id)
    return out


async def list_blocked_by(db: AsyncSession, user_id: UUID) -> list[Connection]:
    """Rows where user_id is the blocker (requester_id on a blocked row)."""
    rows = (
        (
            await db.execute(
                select(Connection).where(
                    Connection.state == "blocked",
                    Connection.requester_id == user_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def insert(
    db: AsyncSession,
    *,
    requester_id: UUID,
    addressee_id: UUID,
    state: str,
) -> Connection:
    responded_at = datetime.now(UTC) if state in ("accepted", "blocked") else None
    row = Connection(
        requester_id=requester_id,
        addressee_id=addressee_id,
        state=state,
        responded_at=responded_at,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def update_state(
    db: AsyncSession,
    *,
    id: UUID,
    state: str,
    responded_at: datetime | None,
) -> Connection:
    await db.execute(
        update(Connection)
        .where(Connection.id == id)
        .values(
            state=state,
            responded_at=responded_at,
            updated_at=datetime.now(UTC),
        )
    )
    return (
        await db.execute(
            select(Connection).where(Connection.id == id).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def delete(db: AsyncSession, connection_id: UUID) -> None:
    await db.execute(sa_delete(Connection).where(Connection.id == connection_id))

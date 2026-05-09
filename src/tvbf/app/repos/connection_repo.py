from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import Connection


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
    """All accepted connections for the user, paired with the *other* user_id."""
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
    """Return (incoming, outgoing) pending requests for the user."""
    rows = (
        (
            await db.execute(
                select(Connection).where(
                    Connection.state == "pending",
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
    incoming = [c for c in rows if c.addressee_id == user_id]
    outgoing = [c for c in rows if c.requester_id == user_id]
    return incoming, outgoing


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

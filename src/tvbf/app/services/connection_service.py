from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import (
    ConnectionAlreadyExists,
    ConnectionBlocked,
    ConnectionWrongState,
    NotAConnectionParty,
    NotFound,
    SelfConnectionForbidden,
)
from tvbf.app.models import Connection
from tvbf.app.repos import connection_repo


async def send_request(db: AsyncSession, *, requester_id: UUID, addressee_id: UUID) -> Connection:
    """Create a pending connection request. Raises:
    - SelfConnectionForbidden if requester == addressee
    - ConnectionBlocked if either side has blocked the other
    - ConnectionAlreadyExists if a non-blocked pair row already exists
    """
    if requester_id == addressee_id:
        raise SelfConnectionForbidden()
    existing = await connection_repo.find_pair(db, requester_id, addressee_id)
    if existing is not None:
        if existing.state == "blocked":
            raise ConnectionBlocked()
        raise ConnectionAlreadyExists(existing)
    row = await connection_repo.insert(
        db,
        requester_id=requester_id,
        addressee_id=addressee_id,
        state="pending",
    )
    await db.commit()
    return row


async def accept(db: AsyncSession, *, id: UUID, accepting_user_id: UUID) -> Connection:
    row = await connection_repo.get(db, id)
    if row is None:
        raise NotFound()
    if row.addressee_id != accepting_user_id:
        raise NotAConnectionParty()
    if row.state != "pending":
        raise ConnectionWrongState()
    updated = await connection_repo.update_state(
        db, id=id, state="accepted", responded_at=datetime.now(UTC)
    )
    await db.commit()
    return updated


async def delete_pending_request(db: AsyncSession, *, id: UUID, caller_id: UUID) -> None:
    """Reject (addressee) or cancel (requester) a pending connection request.
    Raises NotFound if the row doesn't exist or isn't pending; raises
    NotAConnectionParty if caller isn't requester or addressee."""
    row = await connection_repo.get(db, id)
    if row is None or row.state != "pending":
        raise NotFound()
    if caller_id not in (row.requester_id, row.addressee_id):
        raise NotAConnectionParty()
    await connection_repo.delete(db, id)
    await db.commit()


async def delete(db: AsyncSession, *, id: UUID, caller_id: UUID) -> None:
    """Delete a connection row. Used by reject (addressee), cancel (requester),
    and the explicit delete endpoint. Caller must be one of the two parties."""
    row = await connection_repo.get(db, id)
    if row is None:
        raise NotFound()
    if caller_id not in (row.requester_id, row.addressee_id):
        raise NotAConnectionParty()
    await connection_repo.delete(db, id)
    await db.commit()


async def remove_connection(db: AsyncSession, *, user_a: UUID, user_b: UUID) -> None:
    row = await connection_repo.find_pair(db, user_a, user_b)
    if row is None or row.state != "accepted":
        raise NotFound()
    await connection_repo.delete(db, row.id)
    await db.commit()


async def block(db: AsyncSession, *, blocker_id: UUID, blocked_id: UUID) -> Connection:
    """Block another user. Replaces any existing pair row with a blocked row
    where the blocker is requester_id."""
    if blocker_id == blocked_id:
        raise SelfConnectionForbidden()
    existing = await connection_repo.find_pair(db, blocker_id, blocked_id)
    if existing is not None:
        await connection_repo.delete(db, existing.id)
        await db.flush()
    row = await connection_repo.insert(
        db,
        requester_id=blocker_id,
        addressee_id=blocked_id,
        state="blocked",
    )
    await db.commit()
    return row


async def unblock(db: AsyncSession, *, blocker_id: UUID, blocked_id: UUID) -> None:
    """Remove a blocked row. Only the original blocker can unblock.
    Raises NotFound if no blocked row exists; raises NotAConnectionParty if
    the row exists but caller isn't the blocker."""
    row = await connection_repo.find_pair(db, blocker_id, blocked_id)
    if row is None or row.state != "blocked":
        raise NotFound()
    if row.requester_id != blocker_id:
        raise NotAConnectionParty()
    await connection_repo.delete(db, row.id)
    await db.commit()


async def is_blocked_either_way(db: AsyncSession, *, user_a: UUID, user_b: UUID) -> bool:
    row = await connection_repo.find_pair(db, user_a, user_b)
    return row is not None and row.state == "blocked"

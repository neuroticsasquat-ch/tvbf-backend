from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import User


async def create(
    db: AsyncSession,
    *,
    email: str,
    password_hash: str,
    display_name: str,
) -> User:
    """Add a new user row and flush so that generated fields (id, created_at)
    are populated. Caller is responsible for committing."""
    user = User(email=email, password_hash=password_hash, display_name=display_name)
    db.add(user)
    await db.flush()
    return user


async def get_by_id(db: AsyncSession, user_id: UUID) -> User | None:
    return await db.get(User, user_id)


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_many_by_ids(db: AsyncSession, ids: set[UUID]) -> dict[UUID, User]:
    if not ids:
        return {}
    rows = (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()
    return {row.id: row for row in rows}


async def delete_user(db: AsyncSession, user_id: UUID) -> None:
    await db.execute(sa_delete(User).where(User.id == user_id))


async def update_password_hash(db: AsyncSession, user: User, new_hash: str) -> None:
    """Set the password_hash attribute on the loaded model. Caller commits."""
    user.password_hash = new_hash


async def search(
    db: AsyncSession,
    *,
    query: str,
    limit: int,
    exclude_ids: set[UUID],
) -> list[User]:
    """Find users by display_name substring (ILIKE) OR exact email match.

    Email is exact-match only to prevent enumeration. Display name supports
    substring since it's the public handle.
    """
    pattern = f"%{query}%"
    stmt = select(User).where((User.display_name.ilike(pattern)) | (User.email == query))
    if exclude_ids:
        stmt = stmt.where(User.id.notin_(exclude_ids))
    stmt = stmt.order_by(User.display_name).limit(limit)
    return list((await db.execute(stmt)).scalars().all())

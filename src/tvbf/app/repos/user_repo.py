from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import User

# Backslash is the escape character named in the `ilike(..., escape=...)` calls below.
_LIKE_SPECIALS = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def _escape_like(value: str) -> str:
    """Neutralise LIKE metacharacters so a query means itself."""
    return value.translate(_LIKE_SPECIALS)


async def create(
    db: AsyncSession,
    *,
    email: str,
    password_hash: str,
    display_name: str,
    handle: str,
) -> User:
    """Add a new user row and flush so that generated fields (id, created_at)
    are populated. Caller is responsible for committing."""
    user = User(email=email, password_hash=password_hash, display_name=display_name, handle=handle)
    db.add(user)
    await db.flush()
    return user


async def get_by_id(db: AsyncSession, user_id: UUID) -> User | None:
    return await db.get(User, user_id)


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_by_handle(db: AsyncSession, handle: str) -> User | None:
    """The live account holding `handle`, if any (NEU-1163 §6.3).

    Disabled accounts are **not** excluded. Every other lookup on this table
    filters them out, and this one must not: a disabled account still holds its
    handle, and handing it to someone else would let a stranger inherit exactly
    the identity moderation has just taken out of circulation.
    """
    result = await db.execute(select(User).where(User.handle == handle))
    return result.scalar_one_or_none()


async def list_ids(db: AsyncSession) -> list[UUID]:
    """Every user id, oldest account first.

    The weekly recommendations pass's work list (project spec §10). It is every
    user rather than every user with history: the generation floor is what
    decides who is worth a call, and it decides that from the compiled payload
    rather than from a query that would have to reproduce the tier rules a
    second time.

    Ordered by `created_at` so a pass that aborts on consecutive failures
    (`jobs/weekly_recommendations`) covered the same users it would have covered
    yesterday, rather than a different arbitrary prefix each week.

    Disabled accounts are excluded (NEU-1162 §9). One of them cannot see a
    recommendation, so the DeepInfra call their changed taste would buy is money
    spent on nobody. This narrows the *universe*, not the floor — the reasoning
    above about every user rather than every user with history is untouched.
    """
    return list(
        (
            await db.execute(
                select(User.id).where(User.disabled_at.is_(None)).order_by(User.created_at, User.id)
            )
        )
        .scalars()
        .all()
    )


async def filter_enabled(db: AsyncSession, ids: set[UUID]) -> set[UUID]:
    """The subset of `ids` belonging to accounts that are not disabled.

    The set-shaped half of the NEU-1162 §4 predicate, for the two seams that
    already hold a set of ids rather than a query they can join
    (`connection_service.accepted_friend_ids` and `are_connected`). An id no
    account owns is dropped, which is the same answer disabling gives.
    """
    if not ids:
        return set()
    rows = (
        await db.execute(select(User.id).where(User.id.in_(ids), User.disabled_at.is_(None)))
    ).scalars()
    return set(rows)


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
    """Find users by display_name or handle substring (ILIKE), OR exact email.

    Email is exact-match only to prevent enumeration. Display name and handle
    both support substring: **a handle is the opposite of enumeration-sensitive**
    (NEU-1163 §8). It is printed on every card that names its owner, so matching
    part of one reveals nothing the display-name clause does not already reveal,
    and refusing to would protect a value that is already public.

    **`%` and `_` in the query are escaped before the `ILIKE`.** `_` is both a
    LIKE single-character wildcard and one of the three characters a handle may
    contain, so an unescaped `tom_b` would match `tomXb` — a search for the
    handle you were handed finding somebody else's. It was harmless while only
    `display_name` was matched this way and stopped being so here.

    **A leading `@` is stripped from the query.** Someone handed `@tom_b` will
    paste it exactly as they were given it. The strip is here rather than in the
    router because `MIN_QUERY_LENGTH` is checked against what the user typed,
    and moving it would silently turn `@ab` into a two-character query.

    **An exact handle match sorts first**, which is the point at the moment of
    decision: you were given `@tom_b` precisely because three people are called
    Tom, and a result list that buries the exact match alphabetically has
    answered the wrong question.

    Unverified users are excluded unconditionally (NEU-1161 §3.2): being
    discoverable by strangers is one of the two things a verified mailbox buys,
    and the exclusion is blanket, including for people the caller is already
    connected to. Disabled users are excluded beside them (NEU-1162 §4) — people
    discovery is where a new target is found, so it is the one surface where
    leaving a disabled abuser visible would actively help them. All of these
    predicates live here rather than in the router so `limit` still returns a
    full page.

    No index. At this userbase a sequential scan is the plan Postgres would pick
    anyway; if it ever matters the answer is a trigram index matching the
    `ix_*_folded_trgm` pattern `catalog` already uses, and it is a measurement
    rather than a guess.
    """
    term = query.removeprefix("@")
    pattern = f"%{_escape_like(term)}%"
    stmt = (
        select(User)
        .where(
            User.display_name.ilike(pattern, escape="\\")
            | User.handle.ilike(pattern, escape="\\")
            | (User.email == query)
        )
        .where(User.email_verified_at.is_not(None))
        .where(User.disabled_at.is_(None))
    )
    if exclude_ids:
        stmt = stmt.where(User.id.notin_(exclude_ids))
    stmt = stmt.order_by(desc(User.handle == term), User.display_name).limit(limit)
    return list((await db.execute(stmt)).scalars().all())
